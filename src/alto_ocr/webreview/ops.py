"""Apply a batch of corrections to one page and save it.

The browser holds no server session. A corrector opens a page, works
through it, and the client posts the accumulated corrections as one batch;
the server loads the ALTO, applies them, and saves once. That shape is
chosen, not incidental:

* **No server-side page state**, so a restart, a second browser tab, or a
  corrector who closes the laptop lid loses nothing that was already saved
  and corrupts nothing that was not.
* **One write per save**, so the provenance count and the regenerated
  transcript move together, and an SMB share sees one file rewrite rather
  than one per keystroke.
* **The batch is atomic for free**: operations mutate an in-memory lxml
  tree, and nothing reaches disk until every one of them has succeeded. A
  batch that fails half way leaves the file on disk exactly as it was.

Words are addressed by their **index in document order at load time**
(``ReviewPage.words``), which is stable for the life of one loaded page and
needs no IDs -- ALTO from an external editor cannot be relied on to carry
unique ``ID`` attributes, and inventing them would rewrite files a corrector
never touched. Every index is resolved to its ``Word`` *before* any
operation runs, so a deletion earlier in the batch cannot silently shift
what a later operation points at.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..remediate import ConcurrentEditError, ReviewPage, Word
from ..triage import DEFAULT_LOW_WC

logger = logging.getLogger("alto_ocr.webreview.ops")

EDIT = "edit"
VERIFY = "verify"
DELETE = "delete"
CLEAR = "clear"
ADD = "add"
_WORD_OPS = (EDIT, VERIFY, DELETE)
_ALL_OPS = (EDIT, VERIFY, DELETE, CLEAR, ADD)


class OperationError(ValueError):
    """A batch the server refuses to apply. Reported to the client as 400."""


@dataclass(frozen=True)
class Operation:
    """One correction. ``index`` addresses ``ReviewPage.words`` at load time."""

    op: str
    index: int | None = None
    text: str | None = None
    box: tuple[float, float, float, float] | None = None


@dataclass
class ApplyReport:
    """What a batch actually did -- the client turns this into a status line."""

    edited: int = 0
    verified: int = 0
    deleted: int = 0
    added: int = 0
    cleared: int = 0
    already_verified: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.edited or self.verified or self.deleted
                    or self.added or self.cleared)

    def summary(self) -> str:
        """One sentence, in the vocabulary the provenance element uses."""
        parts: list[str] = []
        if self.edited:
            parts.append(f"{self.edited} corrected")
        if self.verified:
            parts.append(f"{self.verified} confirmed")
        if self.deleted:
            parts.append(f"{self.deleted} deleted")
        if self.added:
            parts.append(f"{self.added} added")
        if self.cleared:
            parts.append(f"page cleared ({self.cleared} words removed)")
        if not parts:
            return "No changes to save."
        return "Saved: " + ", ".join(parts) + "."

    def to_json(self) -> dict:
        return {
            "edited": self.edited, "verified": self.verified,
            "deleted": self.deleted, "added": self.added,
            "cleared": self.cleared, "alreadyVerified": self.already_verified,
            "notes": self.notes, "summary": self.summary(),
        }


def parse_operations(payload: object) -> list[Operation]:
    """Validate a client's op list. Raises ``OperationError`` on anything odd.

    Strict on purpose. This is the only untrusted input that reaches the
    archive's files, and a malformed op that is *guessed at* rather than
    rejected would write a correction nobody made.
    """
    if not isinstance(payload, list):
        raise OperationError("Expected a list of operations.")
    ops: list[Operation] = []
    seen_word_indexes: set[int] = set()
    for i, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise OperationError(f"Operation {i} is not an object.")
        name = raw.get("op")
        if name not in _ALL_OPS:
            raise OperationError(
                f"Operation {i}: unknown op {name!r} (expected one of {', '.join(_ALL_OPS)}).")

        if name in _WORD_OPS:
            index = raw.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise OperationError(f"Operation {i}: {name} needs a non-negative integer index.")
            if index in seen_word_indexes:
                # Two operations on one word would each be counted in the
                # provenance total, overstating how many words a human
                # changed. The client coalesces; the server does not guess.
                raise OperationError(
                    f"Operation {i}: word {index} already has an operation in this batch.")
            seen_word_indexes.add(index)
            text = raw.get("text")
            if name == EDIT:
                if not isinstance(text, str) or not text.strip():
                    raise OperationError(
                        f"Operation {i}: edit needs non-empty text (use delete to remove a word).")
                ops.append(Operation(EDIT, index=index, text=text))
            else:
                ops.append(Operation(name, index=index))
            continue

        if name == ADD:
            text = raw.get("text")
            if not isinstance(text, str) or not text.strip():
                raise OperationError(f"Operation {i}: add needs non-empty text.")
            box = raw.get("box")
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               for v in box)):
                raise OperationError(
                    f"Operation {i}: add needs box [x, y, width, height] in source pixels.")
            if box[2] <= 0 or box[3] <= 0:
                raise OperationError(f"Operation {i}: add box must have positive width and height.")
            ops.append(Operation(ADD, text=text, box=(float(box[0]), float(box[1]),
                                                      float(box[2]), float(box[3]))))
            continue

        ops.append(Operation(CLEAR))

    clears = sum(1 for o in ops if o.op == CLEAR)
    if clears > 1:
        raise OperationError("A batch may clear the page at most once.")
    if clears and any(o.op in _WORD_OPS for o in ops):
        # Clearing removes every word, so a per-word operation in the same
        # batch either targets a word that is about to vanish or one that
        # already has -- neither is a correction anyone meant to make.
        # Clear + add is allowed: that is how a corrector rejects a page of
        # hallucinated OCR and restores the handful of words that were real.
        raise OperationError(
            "Clear cannot be combined with per-word operations. Save the "
            "cleared page first, then add the words that were real.")
    return ops


def resolve_targets(page: ReviewPage, ops: list[Operation]) -> dict[int, Word]:
    """Map every word index in a batch to its ``Word``, before anything runs.

    Resolving up front is what makes indices safe to use as addresses: lxml
    element references stay valid across the deletion of *other* elements,
    so a batch that deletes word 4 and edits word 9 edits the word the
    corrector was looking at, not the one that shifted into slot 9.
    """
    targets: dict[int, Word] = {}
    for op in ops:
        if op.index is None:
            continue
        if op.index >= len(page.words):
            raise OperationError(
                f"Word {op.index} is not on this page any more (it has "
                f"{len(page.words)} words). Reload the page and re-apply.")
        targets[op.index] = page.words[op.index]
    return targets


def apply_operations(page: ReviewPage, ops: list[Operation]) -> ApplyReport:
    """Run a validated batch against a loaded page. Nothing is written here."""
    targets = resolve_targets(page, ops)
    report = ApplyReport()
    for op in ops:
        if op.op == EDIT:
            page.edit_word(targets[op.index], op.text)
            report.edited += 1
        elif op.op == VERIFY:
            if page.verify_word(targets[op.index]):
                report.verified += 1
            else:
                # Already at WC 1.00 -- a corrector confirming a word another
                # session had confirmed. Not an error, and deliberately not
                # counted: the provenance total must stay a count of words a
                # human actually acted on for the first time.
                report.already_verified += 1
        elif op.op == DELETE:
            page.delete_word(targets[op.index])
            report.deleted += 1
        elif op.op == ADD:
            page.add_word(op.box, op.text)
            report.added += 1
        elif op.op == CLEAR:
            report.cleared += page.clear_words()
    return report


def load_token(alto_path: Path) -> str:
    """The page's concurrency token: a fingerprint of its current bytes.

    Handed to the client on load and posted back with the batch, so the
    server can tell whether the corrections were made against the version
    still on disk.

    Content-based rather than mtime-based, and that is deliberate.
    ``ReviewPage.save`` guards on ``st_mtime_ns``, which is only as fine as
    the filesystem's timestamp granularity -- measured at ~4 ms on the
    development host, and coarser on the SMB shares this runs over, so two
    saves in quick succession can carry an identical mtime and the second
    would silently overwrite the first. A digest cannot collide that way. It
    also means a token stays valid across a copy that preserves content but
    not mtime, which is the correct answer: the bytes the corrector reviewed
    are still the bytes on disk.

    BLAKE2b at 16 bytes: ALTO pages run tens of KB to a few MB, so hashing
    one page per request is well under a millisecond, and the digest is only
    ever compared for equality.
    """
    try:
        data = alto_path.read_bytes()
    except OSError:
        return "0"
    return f"{len(data)}-{hashlib.blake2b(data, digest_size=16).hexdigest()}"


def check_token(alto_path: Path, token: str | None) -> None:
    """Refuse a batch aimed at a version of the page that is no longer on disk.

    Checked here as well as inside ``ReviewPage.save`` because the two guard
    different windows: this one covers *load to post* (another corrector
    saved while this one was working), ``save``'s covers *load to write*.
    Without this check the client's stale view would overwrite the other
    session's corrections and the save-time guard would never fire, because
    by then this request's own load is the baseline.
    """
    if token is None:
        raise OperationError(
            "Missing page token. Reload the page before saving -- the token "
            "identifies which version of the ALTO these corrections were made against.")
    current = load_token(alto_path)
    if token != current:
        raise ConcurrentEditError(
            f"{alto_path.name} changed on disk after you opened it (another "
            "review session, the Tk review GUI, or an external editor). "
            "Reload to pick up the newer version, or force-save to overwrite "
            "it with your corrections.")


def page_payload(page: ReviewPage, *, stem: str, alto_path: Path,
                 low_wc: float = DEFAULT_LOW_WC) -> dict:
    """Everything the browser needs to render and edit one page.

    Word boxes go out in **source-image pixels** -- ``Word.box_px``, which
    ``ReviewPage`` already scaled from ALTO units by image-size /
    Page-dimensions. That scaling is why this UI is immune to the
    ``MeasurementUnit="mm10"`` problem that misplaces every box in Goobi's
    own ALTO editor: the ratio is computed from the file's own Page
    dimensions rather than assuming ALTO units are pixels.
    """
    words = [
        {
            "i": i,
            "text": w.content,
            "wc": w.wc,
            "box": [round(v, 2) for v in w.box_px],
            "flagged": w.needs_review(low_wc),
        }
        for i, w in enumerate(page.words)
    ]
    return {
        "stem": stem,
        "token": load_token(alto_path),
        "imageWidth": page.image_size[0],
        "imageHeight": page.image_size[1],
        "words": words,
        "flagged": [w["i"] for w in words if w["flagged"]],
        "pc": page.page.get("PC"),
        "lowWc": low_wc,
    }
