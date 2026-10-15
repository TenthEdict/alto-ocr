"""Headless remediation logic: load, edit, and save reviewed ALTO pages.

The review GUI (``review.py``) is a thin Tk layer over this module so the
edit/save semantics stay unit-testable without a display. The ``repackage``
command's logic (``repackage_object``) lives here too and shares
``alto_to_text`` with the review save path, so every tool renders the
identical canonical transcript from a corrected ALTO.

Semantics (agreed 2026-07-30):

* **Editing a word** sets its ``CONTENT`` and stamps ``WC="1.00"`` — after a
  human verifies a word, the confidence is no longer an OCR estimate. This
  also drops the word out of every low-confidence view (GUI stepping,
  triage) so remaining effort concentrates on what nobody has looked at.
* **Confirming a word** (2026-08-14; the OCR was right, the confidence was
  just low) stamps the same ``WC="1.00"`` without touching ``CONTENT`` —
  a human read it against the scan, so it is no longer an estimate and the
  word drops out of the review queue. Correct-but-flagged words are common
  enough (faint type, tight kerning) that without this a corrector can walk
  a page forever and never empty it. Counted *separately* from corrections
  in the provenance ("N word(s) confirmed"), so the record of how many
  words were actually changed stays honest.
* **Deleting a word** (OCR hallucinated text on noise/marginalia) removes
  the ``<String>`` plus its neighboring ``<SP>``, and cascades upward:
  a ``<TextLine>``/``<TextBlock>``/``<ComposedBlock>`` left with no text
  content is removed too, keeping the ALTO schema-shaped. The returned
  ``Removal`` records every detached node with its parent and position, so
  ``undo_delete`` puts the document back exactly as it was (2026-09-24:
  the review GUI deletes without a confirmation prompt, and Ctrl+Z is
  what makes that safe).
* **Text a human types** is checked before it touches the tree
  (``alto_format.content_problem``): a control character pasted in from
  elsewhere is refused with a message, never half-applied.
* **Provenance counts net effect per word** (2026-10-15; per action
  before, so a word edited twice, or edited and then deleted, counted
  twice). The page snapshots each ``<String>`` before its first change
  since load/save, and save compares: added, deleted or re-worded is one
  correction, the same words with a new ``WC`` is one confirmation, and
  back where it started is nothing. Undo therefore needs no counting of
  its own. Reading-order moves still count per move, deliberately.
* **Clearing a page** (2026-07-31; the whole page is hallucinated OCR —
  covers/photographs where texture was read as thousands of garbage
  words) bulk-deletes every word in one operation, leaving the
  Page/PrintSpace skeleton like an engine-produced blank page. The stale
  ``PC`` is dropped (no words → no page confidence), so triage
  re-classifies the page as ``empty`` instead of ranking its garbage
  score; each removed word counts toward provenance like a deletion.
* **Adding a word** (2026-07-31; OCR missed it entirely) takes a box the
  corrector drew on the page image, maps it back into ALTO units via the
  same image-px/Page-dims ratio the display uses, and inserts a
  ``<String>`` at the right spot: into the TextLine whose vertical band
  best overlaps the box, else a new TextLine in the best-overlapping
  TextBlock, else a new TextBlock in PrintSpace. Like an edit, the added
  word is human-verified (``WC="1.00"``) and counts toward provenance.
* **Reordering** (OCR read the page in the wrong order — wrong column
  sequence, captions/margin notes interleaved into the body) works on
  **words** (2026-10-01; it moved Tesseract's blocks and lines before,
  and those units rarely matched what was actually out of order).
  ``move_words`` takes a run of words and places it directly after another
  word (or at the page start); ``nudge_words`` shifts a run one word
  earlier or later. Reading order *is* document order (``alto_to_text``
  ignores coordinates and ``<SP>``), so a move relocates ``<String>``
  elements: a run from one line goes in-line beside its target word, a
  run spanning several lines keeps its line breaks as lines of their own
  (splitting the target's line when the target is mid-line). Word boxes
  never change; containers whose membership changed are re-fitted to their
  contents, SP runs rebuilt, emptied ones removed. Undo restores a
  snapshot of the layout, so it is byte-exact. Reorders are counted
  separately from word corrections in the review provenance
  ("… N reading-order change(s)").
* **Saving** re-serializes with the institution conventions (tab indent,
  reference attribute order — reusing ``ocr.py``'s helpers), recomputes
  ``Page/@PC`` as the mean of current ``WC`` values (so partially-corrected
  pages sink in the triage ranking), refreshes a ``<Processing
  ID="review">`` provenance element with a cumulative corrected-word count,
  and regenerates the sibling ``Text/<stem>.txt`` when a Text folder
  exists — ALTO and transcript never drift apart.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from ._util import atomic_write_text
from .alto_format import (
    ATTR_ORDER,
    alto_namespace,
    content_problem,
    hardened_parser,
    qname,
    reorder_attrs,
    serialize_with_tabs,
)
from .mets import METS_NS, XLINK_NS, PageEntry, _checksum_md5, build_mets, write_mets
from .walker import IMAGE_SUFFIXES, natural_chunks, natural_key

logger = logging.getLogger(__name__)


class StubPageError(ValueError):
    """Raised when a page is a pipeline failure stub (no ``<Page>``) — there
    is nothing to review; the page needs a re-run, not correction."""


class ConcurrentEditError(RuntimeError):
    """Raised by ``ReviewPage.save`` when the ALTO changed on disk after this
    page was loaded (a second review session, an external editor, or a forced
    re-OCR). Saving anyway would silently destroy the other party's work;
    the caller decides — reload, or ``save(force=True)`` deliberately."""


def has_review_provenance(alto_path: Path) -> bool:
    """True when the ALTO carries the review-session provenance stamped by
    ``ReviewPage.save`` (``<Processing ID="review">``), i.e. a human has
    corrected words in this file.

    Namespace-agnostic (``{*}``) so files round-tripped through external
    editors that emit a prefixed ALTO namespace still count as reviewed.
    Unreadable/unparseable files return False — a corrupt file's cure is
    exactly a re-run, so it must not trip the overwrite guard.
    """
    try:
        root = etree.parse(str(alto_path), hardened_parser()).getroot()
    except (OSError, etree.XMLSyntaxError):
        return False
    return any(p.get("ID") == "review" for p in root.iter("{*}Processing"))


@dataclass
class Word:
    """One ALTO ``<String>`` with its box mapped into image-pixel space."""

    elem: etree._Element
    box_px: tuple[float, float, float, float]  # x, y, w, h on the source image

    @property
    def content(self) -> str:
        return self.elem.get("CONTENT", "")

    @property
    def wc(self) -> float | None:
        try:
            return float(self.elem.get("WC"))
        except (TypeError, ValueError):
            return None

    def needs_review(self, threshold: float) -> bool:
        """Below-threshold OR unscored (no WC — e.g. GLMOCR output). A word
        no engine attached confidence to needs eyes just as much."""
        wc = self.wc
        return wc is None or wc < threshold


@dataclass(frozen=True)
class Removal:
    """Everything one word removal detached from the tree, in detach order:
    ``(parent, index, node)`` for the neighboring ``<SP>``, the ``<String>``
    itself, and each container the cascade emptied. Replaying it backwards
    re-inserts each node at its recorded index, which is exact because every
    index was taken at the moment its node left. Opaque to callers — hand
    it back to ``undo_delete``/``redo_add``."""

    steps: tuple[tuple[etree._Element, int, etree._Element], ...]


@dataclass(frozen=True)
class WordMove:
    """The page layout as it was before one ``move_words``: each container's
    children in order, and the attributes of every element a move may
    rewrite. Opaque to callers — hand it back to ``undo_move_words``.

    A snapshot rather than a list of steps, because a move touches several
    containers at once (lines split, re-fitted, rebuilt SP runs, emptied
    ones dropped) and replaying the snapshot is exact by construction. The
    ``<String>`` elements themselves are never copied, so every ``Word`` the
    GUI holds stays valid across move and undo."""

    children: tuple[tuple[etree._Element, tuple[etree._Element, ...]], ...]
    attrs: tuple[tuple[etree._Element, tuple[tuple[str, str], ...]], ...]


def _checked_content(text: str) -> str:
    """Whitespace-collapsed text, refused (ValueError) when ALTO cannot
    store it. The one gate for every word a human types into a page."""
    content = " ".join(text.split())
    problem = content_problem(content)
    if problem is not None:
        raise ValueError(f"{content!r} {problem}.")
    return content


class ReviewPage:
    """One ALTO file held open for editing, with pixel-space word geometry.

    ``image_size`` is the source image's ``(width, height)``; ALTO boxes
    (mm10 after institution conversion, pixels pre-conversion) are scaled by
    image-size / Page-dimensions exactly like ``alto_parse`` does for the
    QA overlays.
    """

    def __init__(self, alto_path: Path, image_size: tuple[int, int]) -> None:
        self.alto_path = Path(alto_path)
        parser = hardened_parser(remove_blank_text=True)
        try:
            self.tree = etree.parse(str(self.alto_path), parser)
        except etree.XMLSyntaxError as e:
            raise StubPageError(
                f"{self.alto_path.name} is not well-formed XML ({e}); "
                "re-run OCR for this page instead of reviewing it."
            ) from e
        self.root = self.tree.getroot()
        # Namespace of the document's ALTO elements — the shared idiom
        # (alto_format.alto_namespace): prefixed ALTO from external editors
        # works the same as default-namespace ALTO.
        self.ns = alto_namespace(self.root)

        self.page = self.root.find(f".//{qname(self.ns, 'Page')}")
        if self.page is None:
            raise StubPageError(
                f"{self.alto_path.name} has no <Page> — it is a failure stub; "
                "re-run OCR for this page instead of reviewing it."
            )
        try:
            page_w = int(self.page.get("WIDTH", "0"))
            page_h = int(self.page.get("HEIGHT", "0"))
        except ValueError:
            page_w = page_h = 0
        if page_w <= 0 or page_h <= 0:
            raise StubPageError(f"{self.alto_path.name}: <Page> has no usable dimensions.")

        img_w, img_h = image_size
        # Kept, not just consumed: every box this class hands out is in
        # image-pixel space, so a consumer that renders them needs the frame
        # those boxes belong to. The Tk GUI has the PIL image at hand; the
        # web review server serves geometry as JSON and has nothing to ask.
        self.image_size = (img_w, img_h)
        self.sx = img_w / page_w
        self.sy = img_h / page_h

        self.words: list[Word] = []
        for el in self.root.iter(qname(self.ns, "String")):
            try:
                h, v = int(el.get("HPOS", "")), int(el.get("VPOS", ""))
                w, ht = int(el.get("WIDTH", "")), int(el.get("HEIGHT", ""))
            except ValueError:
                continue  # malformed box: not clickable, leave untouched
            self.words.append(Word(el, (h * self.sx, v * self.sy, w * self.sx, ht * self.sy)))

        # Concurrent-edit guard baseline: save() re-stats and refuses (unless
        # forced) when the file changed on disk after this load. mtime ticks
        # are coarse on some network shares — best-effort, not a lock.
        self._loaded_mtime_ns = self.alto_path.stat().st_mtime_ns
        # The tree as last loaded or saved, for settle(): a session whose
        # changes have all been undone has nothing to save.
        self._clean_state = etree.tostring(self.root)

        self.dirty = False
        # Each <String> touched since load/save -> its state before the first
        # touch (None: not on the page then, i.e. added this session). save()
        # counts provenance from these by net effect per word.
        self._before: dict[etree._Element, tuple[str, float | None] | None] = {}
        self.session_moves = 0  # reading-order moves since load, folded into provenance on save

    # ---------- provenance counting ---------------------------------------

    def _state(self, el: etree._Element) -> tuple[str, float | None] | None:
        """A String's countable state: ``None`` when it is not on the page,
        else its CONTENT and WC. WC is compared as a number, rounded the way
        it is written, so an undo that rewrites ``0.4`` as ``0.40`` is not
        mistaken for a change."""
        if not self._attached(el):
            return None
        try:
            wc: float | None = round(float(el.get("WC")), 2)
        except (TypeError, ValueError):
            wc = None
        return el.get("CONTENT", ""), wc

    def _touch(self, el: etree._Element) -> None:
        """Record a String's state before its first change since load/save.
        Every mutator calls this *before* mutating, undo paths included: an
        undo reaching back past a save is itself a change to the saved file."""
        if el not in self._before:
            self._before[el] = self._state(el)

    def _net_counts(self) -> tuple[int, int]:
        """``(corrected, confirmed)`` by net effect per word, since load/save.

        A word counts once however many actions touched it: added, deleted,
        or with different CONTENT is one correction; the same CONTENT with a
        different WC (a human vouching for it) is one confirmation; back
        where it started is nothing. So a word edited twice, or edited and
        then deleted, is one correction, and a word added and then deleted
        again is none. Undo needs no bookkeeping of its own: it puts the
        state back, and the comparison sees that.
        """
        corrected = confirmed = 0
        for el, before in self._before.items():
            after = self._state(el)
            if before == after:
                continue
            if before is None or after is None or before[0] != after[0]:
                corrected += 1
            else:
                confirmed += 1
        return corrected, confirmed

    @property
    def session_edits(self) -> int:
        """Words corrected (edited, added or deleted) since load/save, net."""
        return self._net_counts()[0]

    @property
    def session_verifies(self) -> int:
        """Words confirmed as correct since load/save, net."""
        return self._net_counts()[1]

    # ---------- queries ---------------------------------------------------

    def flagged(self, threshold: float) -> list[Word]:
        """Words needing review, in document (reading) order."""
        return [w for w in self.words if w.needs_review(threshold)]

    def word_at(self, x: float, y: float) -> Word | None:
        """Smallest word whose box contains the image-pixel point, if any."""
        hits = [
            w for w in self.words
            if w.box_px[0] <= x <= w.box_px[0] + w.box_px[2]
            and w.box_px[1] <= y <= w.box_px[1] + w.box_px[3]
        ]
        return min(hits, key=lambda w: w.box_px[2] * w.box_px[3]) if hits else None

    def doc_index(self, word: Word) -> int | None:
        """Position of a word in document (reading) order; None once it has
        been removed from the page."""
        for i, w in enumerate(self.words):
            if w is word:
                return i
        return None

    def step_from(self, current: Word, candidates: list[Word],
                  delta: int) -> Word | None:
        """The nearest word from *candidates* lying after (``delta`` > 0) or
        before (``delta`` < 0) *current* in document order, wrapping at the
        page ends. *current* itself is never returned; ``None`` means there
        is nowhere else to go (no other candidate on the page).

        Stepping is by **position, not list membership**: correcting a word
        stamps ``WC="1.00"``, which drops it out of the flagged set the
        instant it is edited, so "where is the corrector on the page" cannot
        be answered by looking *current* up in *candidates*. Answering it
        that way sent the corrector back to the page's first flagged word
        after every single correction — forward progress through a page was
        impossible (fixed 2026-08-14). *candidates* need not be sorted; a
        word missing from the page (just deleted) anchors at the page start.
        """
        pos = {id(w.elem): i for i, w in enumerate(self.words)}
        end = len(self.words)
        pool = sorted((w for w in candidates if w is not current),
                      key=lambda w: pos.get(id(w.elem), end))
        if not pool:
            return None
        here = pos.get(id(current.elem))
        if here is None:  # current is gone from the page (deleted)
            return pool[0] if delta > 0 else pool[-1]
        if delta > 0:
            later = [w for w in pool if pos.get(id(w.elem), -1) > here]
            return later[0] if later else pool[0]
        earlier = [w for w in pool if pos.get(id(w.elem), end) < here]
        return earlier[-1] if earlier else pool[-1]

    # ---------- edits -----------------------------------------------------

    def edit_word(self, word: Word, new_text: str) -> None:
        """Set a word's CONTENT (whitespace-collapsed) and mark it verified.
        Raises ValueError, changing nothing, for empty text or text ALTO
        cannot store."""
        text = _checked_content(new_text)
        if not text:
            raise ValueError("Empty text — use delete_word to remove a word.")
        self._touch(word.elem)
        word.elem.set("CONTENT", text)
        word.elem.set("WC", "1.00")
        reorder_attrs(word.elem, ATTR_ORDER["String"])
        self.dirty = True

    def verify_word(self, word: Word) -> bool:
        """Confirm a low-confidence word the OCR actually got right: stamp
        ``WC="1.00"``, leaving ``CONTENT`` alone.

        The claim is the same one an edit makes — a human read this word
        against the scan and it is correct — so the word leaves every
        low-confidence view (GUI stepping, triage) and lifts the recomputed
        page ``PC``. Without it a correct-but-flagged word is re-served on
        every pass through the page and no amount of reviewing ever empties
        the queue.

        Counted apart from corrections (``session_verifies``, its own
        provenance clause): the archive's record of how many words a human
        *changed* has to stay honest. Returns False, recording nothing, for
        a word already at 1.00.
        """
        wc = word.wc
        if wc is not None and wc >= 1.0:
            return False
        self._touch(word.elem)
        word.elem.set("WC", "1.00")
        reorder_attrs(word.elem, ATTR_ORDER["String"])
        self.dirty = True
        return True

    def delete_word(self, word: Word) -> Removal:
        """Remove a hallucinated word (plus a neighboring SP; cascade empties).
        The returned ``Removal`` lets ``undo_delete`` restore it exactly."""
        removal = self._remove_string(word)
        self.dirty = True
        return removal

    def undo_delete(self, word: Word, removal: Removal) -> None:
        """Undo one ``delete_word`` (GUI undo stack): put the String, its SP
        and every container the cascade removed back where they were (the
        deletion then nets out of provenance). ``dirty`` stays as-is, like ``revert_word`` —
        other unsaved changes may exist; reloading is the full reset.

        Only exact while the tree is as the deletion left it, which a strict
        undo stack guarantees: every later change has been undone first."""
        self._restore(word, removal)

    def line_words(self, word: Word) -> list[Word]:
        """Every word on ``word``'s TextLine, in reading order — a whole
        garbage line is selected (and deleted) in one gesture."""
        line = word.elem.getparent()
        return [w for w in self.words if w.elem.getparent() is line]

    def block_words(self, word: Word) -> list[Word]:
        """Every word in ``word``'s TextBlock, in reading order (its line
        alone when the word sits in no TextBlock)."""
        block = next((a for a in word.elem.iterancestors()
                      if etree.QName(a).localname == "TextBlock"), None)
        if block is None:
            return self.line_words(word)
        return [w for w in self.words if any(a is block for a in w.elem.iterancestors())]

    def _remove_string(self, word: Word) -> Removal:
        """Detach a word's ``<String>`` plus a neighboring ``<SP>``, cascading
        away emptied containers. Shared by ``delete_word`` and ``undo_add``."""
        self._touch(word.elem)
        steps: list[tuple[etree._Element, int, etree._Element]] = []

        def detach(node: etree._Element) -> None:
            parent_ = node.getparent()
            steps.append((parent_, parent_.index(node), node))
            parent_.remove(node)

        el = word.elem
        parent = el.getparent()
        sp_tag = qname(self.ns, "SP")
        prev, nxt = el.getprevious(), el.getnext()
        if prev is not None and prev.tag == sp_tag:
            detach(prev)
        elif nxt is not None and nxt.tag == sp_tag:
            detach(nxt)
        detach(el)

        # Cascade: drop text containers left with no String anywhere below.
        # Only TextLine/TextBlock/ComposedBlock — never PrintSpace/Page/margins.
        string_tag = qname(self.ns, "String")
        removable = {"TextLine", "TextBlock", "ComposedBlock"}
        node = parent
        while node is not None and etree.QName(node).localname in removable:
            up = node.getparent()
            if next(node.iter(string_tag), None) is None:
                detach(node)
            node = up

        self.words.remove(word)
        return Removal(tuple(steps))

    def _restore(self, word: Word, removal: Removal) -> None:
        """Replay a ``Removal`` backwards and put the word back into
        ``self.words`` at its reading-order position."""
        self._touch(word.elem)
        for parent, index, node in reversed(removal.steps):
            parent.insert(index, node)
        self._insert_in_reading_order(word)

    def _insert_in_reading_order(self, word: Word) -> None:
        """Place an attached word in ``self.words`` by document order —
        ``flagged()`` and the GUI's next-word stepping rely on that order."""
        order = {id(s): i
                 for i, s in enumerate(self.root.iter(qname(self.ns, "String")))}
        pos = sum(1 for existing in self.words
                  if order.get(id(existing.elem), -1) < order[id(word.elem)])
        self.words.insert(pos, word)

    def clear_words(self) -> int:
        """Remove every word on the page — bulk rejection for pages whose
        OCR is wholly hallucinated (book covers, photographs, blank pages
        where paper or fabric texture was read as text; the observed case
        was a cloth cover producing ~1,900 garbage words nobody should
        delete one at a time). Equivalent to deleting each word
        individually — every removed word counts toward the review
        provenance total like ``delete_word`` — but in one operation.

        The Page/PrintSpace skeleton survives, matching an engine-produced
        blank page, and the stale ``PC`` is dropped (no words → no page
        confidence), so the page re-classifies as ``empty`` in triage
        instead of ranking by its garbage score. Returns the number of
        ``<String>`` elements removed (this can exceed ``len(words)`` when
        the file held malformed-box Strings the loader skipped — a cleared
        page rejects those too).
        """
        strings = list(self.root.iter(qname(self.ns, "String")))
        if not strings:
            return 0
        for el in strings:
            self._touch(el)
        removable = {"TextLine", "TextBlock", "ComposedBlock"}
        for el in strings:
            top = el
            node = el.getparent()
            while node is not None and etree.QName(node).localname in removable:
                top, node = node, node.getparent()
            parent = top.getparent()
            if parent is not None:  # a sibling String already took this container out
                parent.remove(top)
        self.words.clear()
        self.page.attrib.pop("PC", None)
        self.dirty = True
        return len(strings)

    # ---------- word insertion (missed-word corrections) ------------------

    _ID_CODES = {"ComposedBlock": "CB", "TextBlock": "TB",
                 "TextLine": "TL", "String": "ST", "SP": "SP"}

    def add_word(self, box_px: tuple[float, float, float, float], text: str) -> Word:
        """Insert a word the OCR missed, boxed by the corrector in image px.

        ``box_px`` is (x, y, w, h) on the source image — the box the
        corrector drew. It converts to ALTO units by the inverse of the
        display scaling, so the stored coordinates land exactly where the
        box was drawn on the original scan. Placement: the String joins the
        TextLine whose vertical band best overlaps the box (left-to-right
        order, the line's ``<SP>`` run rebuilt); failing that, a new
        TextLine in the TextBlock with the largest box overlap; failing
        that, a new TextBlock in PrintSpace at its top-to-bottom position.
        Ancestor bboxes grow to cover the new word. A human put the word
        there, so it carries ``WC="1.00"`` and counts toward the review
        provenance total like any other correction. Returns the new Word,
        placed in reading order in ``self.words``.
        """
        content = _checked_content(text)
        if not content:
            raise ValueError("Empty text — nothing to add.")
        x, y, w, h = box_px
        hpos = max(0, int(round(x / self.sx)))
        vpos = max(0, int(round(y / self.sy)))
        width = max(1, int(round(w / self.sx)))
        height = max(1, int(round(h / self.sy)))

        el = etree.Element(qname(self.ns, "String"))
        self._touch(el)  # not on the page yet: counts as added
        el.set("ID", self._next_id("String"))
        for attr, val in (("HPOS", hpos), ("VPOS", vpos),
                          ("WIDTH", width), ("HEIGHT", height)):
            el.set(attr, str(val))
        el.set("CONTENT", content)
        el.set("WC", "1.00")
        reorder_attrs(el, ATTR_ORDER["String"])

        line = self._best_line(hpos, vpos, width, height)
        if line is not None:
            self._insert_into_line(line, el)
        else:
            self._new_line_for(el, hpos, vpos, width, height)
        self._expand_ancestors(el, hpos, vpos, width, height)

        word = Word(el, (hpos * self.sx, vpos * self.sy,
                         width * self.sx, height * self.sy))
        self._insert_in_reading_order(word)

        self.dirty = True
        return word

    def undo_add(self, word: Word) -> Removal:
        """Undo one ``add_word`` (GUI undo stack): detach the String again
        (the add then nets out of provenance). ``dirty`` stays as-is, like
        ``revert_word`` — other unsaved changes may exist; reloading is the
        full reset.

        Also how the GUI deletes a word added in the same session; the returned ``Removal`` lets ``redo_add`` bring it back
        when that deletion is itself undone."""
        return self._remove_string(word)

    def redo_add(self, word: Word, removal: Removal) -> None:
        """Reverse ``undo_add``: re-attach the added word exactly where it
        was (it counts as added again)."""
        self._restore(word, removal)
        self.dirty = True

    @staticmethod
    def _box_of(el: etree._Element) -> tuple[int, int, int, int] | None:
        try:
            return (int(el.get("HPOS", "")), int(el.get("VPOS", "")),
                    int(el.get("WIDTH", "")), int(el.get("HEIGHT", "")))
        except ValueError:
            return None

    def _next_id(self, local: str, reserved: set[str] | None = None) -> str:
        """Fresh institution-style ID (e.g. ``P1_ST00005``): continue past
        the highest existing number for the element type; never reuse an ID
        (deleted words leave gaps, which is fine — nothing renumbers).
        ``reserved`` adds IDs held by elements momentarily out of the tree
        (a line a move is relocating) and receives the new one."""
        prefix = f"{self.page.get('ID', 'P1')}_{self._ID_CODES[local]}"
        used = {e.get("ID") for e in self.root.iter() if e.get("ID") is not None}
        if reserved:
            used |= reserved
        pat = re.compile(re.escape(prefix) + r"(\d+)$")
        n = 0
        for uid in used:
            m = pat.match(uid)
            if m:
                n = max(n, int(m.group(1)))
        n += 1
        while f"{prefix}{n:05d}" in used:
            n += 1
        if reserved is not None:
            reserved.add(f"{prefix}{n:05d}")
        return f"{prefix}{n:05d}"

    def _best_line(self, hpos: int, vpos: int, width: int, height: int
                   ) -> etree._Element | None:
        """TextLine whose vertical band overlaps at least half the shorter
        of (line, word) heights; best = most overlap, then nearest
        horizontally. Handwritten baselines wobble, so the criterion is a
        band test, not containment."""
        best = None
        best_key: tuple[float, float] | None = None
        for line in self.root.iter(qname(self.ns, "TextLine")):
            b = self._box_of(line)
            if b is None:
                continue
            lx, ly, lw, lh = b
            overlap = min(ly + lh, vpos + height) - max(ly, vpos)
            if overlap < 0.5 * min(max(lh, 1), max(height, 1)):
                continue
            h_gap = max(lx - (hpos + width), hpos - (lx + lw), 0)
            key = (-overlap, float(h_gap))
            if best_key is None or key < best_key:
                best, best_key = line, key
        return best

    def _insert_into_line(self, line: etree._Element, el: etree._Element) -> None:
        hpos = int(el.get("HPOS"))
        anchor = None
        for s in line.findall(qname(self.ns, "String")):
            try:
                if int(s.get("HPOS", "")) > hpos:
                    anchor = s
                    break
            except ValueError:
                continue
        if anchor is not None:
            anchor.addprevious(el)
        else:
            line.append(el)
        self._rebuild_line_sps(line)

    def _rebuild_line_sps(self, line: etree._Element) -> None:
        """Regenerate the line's ``<SP>`` run from its current Strings —
        simpler and safer than splicing around one insertion point. SP
        geometry is derived (the gap between neighbors) and nothing
        references SP IDs, so rebuilding is loss-free."""
        sp_tag = qname(self.ns, "SP")
        for sp in line.findall(sp_tag):
            line.remove(sp)
        strings = line.findall(qname(self.ns, "String"))
        for left, right in zip(strings, strings[1:]):
            lb, rb = self._box_of(left), self._box_of(right)
            if lb is None or rb is None:
                continue
            sp = etree.Element(sp_tag)
            sp.set("ID", self._next_id("SP"))
            sp.set("HPOS", str(lb[0] + lb[2]))
            sp.set("VPOS", str(lb[1]))
            sp.set("WIDTH", str(max(0, rb[0] - (lb[0] + lb[2]))))
            left.addnext(sp)

    def _new_line_for(self, el: etree._Element,
                      hpos: int, vpos: int, width: int, height: int) -> None:
        """House ``el`` in a new TextLine boxed to the word — inside the
        best-overlapping TextBlock, or a new TextBlock in PrintSpace when
        nothing overlaps (marginalia, annotations on blank areas)."""
        line = etree.Element(qname(self.ns, "TextLine"))
        line.set("ID", self._next_id("TextLine"))
        for attr, val in (("HPOS", hpos), ("VPOS", vpos),
                          ("WIDTH", width), ("HEIGHT", height)):
            line.set(attr, str(val))
        line.append(el)

        block = self._best_block(hpos, vpos, width, height)
        if block is None:
            block = etree.Element(qname(self.ns, "TextBlock"))
            block.set("ID", self._next_id("TextBlock"))
            for attr, val in (("HPOS", hpos), ("VPOS", vpos),
                              ("WIDTH", width), ("HEIGHT", height)):
                block.set(attr, str(val))
            ps = self.page.find(qname(self.ns, "PrintSpace"))
            if ps is None:
                # Degenerate ALTO (no PrintSpace at all — some blank-page
                # variants). Text must live somewhere; create one page-sized.
                ps = etree.SubElement(self.page, qname(self.ns, "PrintSpace"))
                ps.set("ID", f"{self.page.get('ID', 'P1')}_PS00001")
                for attr, val in (("HPOS", "0"), ("VPOS", "0"),
                                  ("WIDTH", self.page.get("WIDTH", "0")),
                                  ("HEIGHT", self.page.get("HEIGHT", "0"))):
                    ps.set(attr, val)
            self._insert_by_vpos(ps, block, vpos, ("TextBlock", "ComposedBlock"))
        self._insert_by_vpos(block, line, vpos, ("TextLine",))

    def _best_block(self, hpos: int, vpos: int, width: int, height: int
                    ) -> etree._Element | None:
        """TextBlock with the largest intersection area, or None."""
        best, best_area = None, 0
        for block in self.root.iter(qname(self.ns, "TextBlock")):
            b = self._box_of(block)
            if b is None:
                continue
            bx, by, bw, bh = b
            ix = min(bx + bw, hpos + width) - max(bx, hpos)
            iy = min(by + bh, vpos + height) - max(by, vpos)
            area = ix * iy if (ix > 0 and iy > 0) else 0
            if area > best_area:
                best, best_area = block, area
        return best

    @staticmethod
    def _insert_by_vpos(parent: etree._Element, el: etree._Element,
                        vpos: int, localnames: tuple[str, ...]) -> None:
        """Insert ``el`` before the first ``localnames`` child whose VPOS is
        below the new element's — keeping top-to-bottom reading order."""
        for child in parent:
            if etree.QName(child).localname not in localnames:
                continue
            try:
                if int(child.get("VPOS", "")) > vpos:
                    child.addprevious(el)
                    return
            except ValueError:
                continue
        parent.append(el)

    def _expand_ancestors(self, el: etree._Element,
                          hpos: int, vpos: int, width: int, height: int) -> None:
        """Grow TextLine/TextBlock/ComposedBlock bboxes to cover the new
        word. PrintSpace and margins stay untouched — the institution
        converter owns those, and consumers only read Page dims + String
        boxes."""
        node = el.getparent()
        grow = {"TextLine", "TextBlock", "ComposedBlock"}
        while node is not None and etree.QName(node).localname in grow:
            b = self._box_of(node)
            if b is not None:
                nx, ny = min(b[0], hpos), min(b[1], vpos)
                nx2 = max(b[0] + b[2], hpos + width)
                ny2 = max(b[1] + b[3], vpos + height)
                node.set("HPOS", str(nx))
                node.set("VPOS", str(ny))
                node.set("WIDTH", str(nx2 - nx))
                node.set("HEIGHT", str(ny2 - ny))
            node = node.getparent()

    def revert_word(self, word: Word, old_content: str, old_wc: float | None) -> None:
        """Undo one ``edit_word`` (GUI undo stack): restore the exact prior
        CONTENT/WC (the edit then nets out of provenance). ``dirty`` stays as-is — other
        unsaved changes may exist; reloading the page is the full reset."""
        self._touch(word.elem)
        word.elem.set("CONTENT", old_content)
        if old_wc is None:
            word.elem.attrib.pop("WC", None)
        else:
            word.elem.set("WC", f"{old_wc:.2f}")
        reorder_attrs(word.elem, ATTR_ORDER["String"])

    def unverify_word(self, word: Word, old_wc: float | None) -> None:
        """Undo one ``verify_word`` (GUI undo stack): restore the engine's
        WC (the confirmation then nets out of provenance). ``dirty`` stays as-is, like
        ``revert_word`` — reloading the page is the full reset."""
        self._touch(word.elem)
        if old_wc is None:
            word.elem.attrib.pop("WC", None)
        else:
            word.elem.set("WC", f"{old_wc:.2f}")
        reorder_attrs(word.elem, ATTR_ORDER["String"])

    # ---------- reading order ---------------------------------------------

    # Containers that hold words; a move re-fits and may remove these.
    _WORD_CONTAINERS = frozenset({"TextLine", "TextBlock", "ComposedBlock"})

    def move_words(self, words: list[Word], after: Word | None) -> WordMove | None:
        """Move ``words`` so they are read directly after ``after`` — or
        first on the page when ``after`` is None. Returns the ``WordMove``
        that ``undo_move_words`` takes, or None (nothing changed) when the
        words are already read there.

        The words travel in their current reading order. Where they land:

        * a run from **one line** goes in-line, straight after ``after``
          (before the page's first word for ``None``) — the case of a word
          or phrase read out of place;
        * a run spanning **several lines** keeps its line breaks: each
          source line's share becomes a line of its own after ``after``'s
          line (before the first line for ``None``). When ``after`` is
          mid-line, its line is split so the moved lines sit between its
          two halves. A source line that moved whole is reused, ID and all.

        Word boxes never change — they still sit on the same pixels. Lines
        and blocks that gained or lost words are re-fitted to what they now
        contain, their ``<SP>`` runs rebuilt, and emptied ones removed like a
        deletion's cascade. Counts once toward the provenance reading-order
        total. Raises ValueError when ``after`` is one of ``words``.
        """
        pos = {id(w.elem): i for i, w in enumerate(self.words)}
        unique = {id(w.elem): w for w in words if id(w.elem) in pos}
        run = sorted(unique.values(), key=lambda w: pos[id(w.elem)])
        if not run:
            return None
        if after is not None:
            if id(after.elem) in unique:
                raise ValueError("Words cannot be placed after one of themselves.")
            if id(after.elem) not in pos:
                raise ValueError("The target word is no longer on this page.")
        start = pos[id(after.elem)] + 1 if after is not None else 0
        if [pos[id(w.elem)] for w in run] == list(range(start, start + len(run))):
            return None  # already read exactly there

        snapshot = self._layout_snapshot()
        string_tag = qname(self.ns, "String")
        # Lines are relocated by detaching them, which hides their IDs from
        # _next_id: reserve every ID on the page before anything moves.
        ids = {e.get("ID") for e in self.root.iter() if e.get("ID") is not None}

        # Each touched line's String membership before the move, and the
        # containers it sat in: re-fitting is for containers whose contents
        # changed, never for a line whose words were only reordered.
        before: dict[int, tuple[etree._Element, frozenset[int]]] = {}
        blocks: dict[int, etree._Element] = {}

        def touch(line: etree._Element) -> None:
            if id(line) not in before:
                before[id(line)] = (line, frozenset(
                    id(s) for s in line.findall(string_tag)))

        # The word the run goes beside: ``after``, or (page start) the first
        # String that is not itself moving. Found before anything detaches,
        # so the target line's membership is recorded as it was.
        if after is not None:
            anchor = after.elem
        else:
            anchor = next((s for s in self.page.iter(string_tag)
                           if id(s) not in unique), None)
            if anchor is None:
                return None
        target = anchor.getparent()
        touch(target)

        # Segments: consecutive run words sharing a source line.
        segments: list[tuple[etree._Element, list[etree._Element]]] = []
        for w in run:
            line = w.elem.getparent()
            if segments and segments[-1][0] is line:
                segments[-1][1].append(w.elem)
            else:
                segments.append((line, [w.elem]))
        for line, elems in segments:
            touch(line)
            for el in elems:
                line.remove(el)

        if len(segments) == 1:
            elems = segments[0][1]
            if after is not None:
                for el in reversed(elems):
                    anchor.addnext(el)
            else:
                for el in elems:
                    anchor.addprevious(el)
        else:
            new_lines = []
            for source, elems in segments:
                if source is not target and next(source.iter(string_tag), None) is None:
                    # The whole line moved: reuse it (its ID survives).
                    old_parent = source.getparent()
                    if old_parent is not None:
                        self._collect_blocks(source, blocks)
                        old_parent.remove(source)
                        self._drop_if_empty(old_parent, blocks)
                    line = source
                else:
                    line = self._new_line(ids)
                for el in elems:
                    line.append(el)
                touch(line)
                new_lines.append(line)
            tail = None
            if after is not None:
                rest = list(anchor.itersiblings(string_tag))
                if rest:  # target mid-line: split it around the moved lines
                    tail = self._new_line(ids)
                    touch(tail)
                    for el in rest:
                        tail.append(el)
            if after is None:
                for line in new_lines:
                    target.addprevious(line)
            else:
                prev = target
                for line in new_lines + ([tail] if tail is not None else []):
                    prev.addnext(line)
                    prev = line
            # The target's block gained lines (a reused one unchanged inside).
            self._collect_blocks(target, blocks)

        # Tidy every touched line: drop it when emptied, else rebuild its SP
        # run (word order changed) and re-fit it when its words changed.
        for line, members in before.values():
            if not self._attached(line):
                continue
            strings = line.findall(string_tag)
            if not strings:
                self._collect_blocks(line, blocks)
                self._drop_if_empty(line, blocks)
                continue
            self._rebuild_line_sps(line)
            if frozenset(id(s) for s in strings) != members:
                self._fit_box(line, strings)
                self._collect_blocks(line, blocks)
        # Innermost first, so a ComposedBlock fits its re-fitted blocks.
        for block in sorted(blocks.values(), key=self._depth, reverse=True):
            if self._attached(block):
                self._fit_box(block, [
                    c for c in block if isinstance(c.tag, str)
                    and etree.QName(c).localname in self._WORD_CONTAINERS])

        self._resort_words()
        self.dirty = True
        self.session_moves += 1
        return snapshot

    def nudge_words(self, words: list[Word], delta: int) -> WordMove | None:
        """Move ``words`` one word earlier (``delta`` < 0) or later in reading
        order — ``move_words`` with the neighboring word as the target. None
        at the page's start/end (nothing to step past)."""
        pos = {id(w.elem): i for i, w in enumerate(self.words)}
        chosen = {id(w.elem) for w in words if id(w.elem) in pos}
        if not chosen or delta == 0:
            return None
        idx = sorted(pos[i] for i in chosen)
        if delta < 0:
            earlier = [w for w in self.words[:idx[0]] if id(w.elem) not in chosen]
            if not earlier:
                return None
            after = earlier[-2] if len(earlier) >= 2 else None
        else:
            later = [w for w in self.words[idx[-1] + 1:] if id(w.elem) not in chosen]
            if not later:
                return None
            after = later[0]
        return self.move_words(words, after)

    def undo_move_words(self, move: WordMove) -> None:
        """Undo one ``move_words``/``nudge_words`` (GUI undo stack): put the
        layout back exactly and un-count the move. ``dirty`` stays as-is,
        like ``revert_word`` — reloading is the full reset.

        Only exact while the tree is as the move left it, which a strict
        undo stack guarantees: every later change has been undone first."""
        self._restore_layout(move)
        self.session_moves = max(0, self.session_moves - 1)

    def _layout_snapshot(self) -> WordMove:
        children = []
        attrs = []
        rewritable = self._WORD_CONTAINERS | {"SP"}
        for el in self.page.iter():
            if not isinstance(el.tag, str):
                continue
            if len(el):
                children.append((el, tuple(el)))
            if etree.QName(el).localname in rewritable:
                attrs.append((el, tuple(el.attrib.items())))
        return WordMove(tuple(children), tuple(attrs))

    def _restore_layout(self, move: WordMove) -> None:
        for parent, kids in move.children:
            parent[:] = list(kids)
        for el, items in move.attrs:
            el.attrib.clear()
            for key, value in items:
                el.set(key, value)
        self._resort_words()

    def _resort_words(self) -> None:
        """Keep ``self.words`` in document (reading) order — ``flagged()``
        and the GUI's next-word stepping rely on it."""
        order = {id(s): i
                 for i, s in enumerate(self.root.iter(qname(self.ns, "String")))}
        self.words.sort(key=lambda w: order.get(id(w.elem), 1 << 30))

    def _new_line(self, reserved: set[str]) -> etree._Element:
        line = etree.Element(qname(self.ns, "TextLine"))
        line.set("ID", self._next_id("TextLine", reserved))
        return line

    def _collect_blocks(self, el: etree._Element,
                        into: dict[int, etree._Element]) -> None:
        """Queue every text container above ``el`` for re-fitting."""
        node = el.getparent()
        while node is not None and etree.QName(node).localname in self._WORD_CONTAINERS:
            into[id(node)] = node
            node = node.getparent()

    def _drop_if_empty(self, node: etree._Element,
                       blocks: dict[int, etree._Element]) -> None:
        """Remove ``node`` and each ancestor container left with no String —
        the deletion cascade, never PrintSpace or above. The first surviving
        container is queued for re-fitting."""
        string_tag = qname(self.ns, "String")
        while node is not None and etree.QName(node).localname in self._WORD_CONTAINERS:
            parent = node.getparent()
            if parent is None:
                return
            if next(node.iter(string_tag), None) is not None:
                blocks[id(node)] = node
                return
            parent.remove(node)
            node = parent

    def _fit_box(self, el: etree._Element, children: list[etree._Element]) -> None:
        """Set ``el``'s box (ALTO units) to the union of its children's."""
        boxes = [b for b in (self._box_of(c) for c in children) if b is not None]
        if not boxes:
            return
        x = min(b[0] for b in boxes)
        y = min(b[1] for b in boxes)
        x2 = max(b[0] + b[2] for b in boxes)
        y2 = max(b[1] + b[3] for b in boxes)
        for attr, val in (("HPOS", x), ("VPOS", y),
                          ("WIDTH", x2 - x), ("HEIGHT", y2 - y)):
            el.set(attr, str(val))

    def _attached(self, el: etree._Element) -> bool:
        return any(a is self.page for a in el.iterancestors())

    @staticmethod
    def _depth(el: etree._Element) -> int:
        return sum(1 for _ in el.iterancestors())

    def settle(self) -> bool:
        """After an undo: clear ``dirty`` when the tree is byte-identical to
        its last loaded or saved state — every change has been undone, so
        nothing is left to save. Without this an undone session still asks
        to be saved, and saving it stamps review provenance (and with it the
        re-OCR guard) on a page nobody changed. Conservative by design: an
        undo that is not byte-exact (``undo_add`` does not shrink the line
        and block boxes an added word grew) keeps the page dirty. Returns
        ``dirty``."""
        if self.dirty and etree.tostring(self.root) == self._clean_state:
            self.dirty = False
        return self.dirty

    # ---------- save ------------------------------------------------------

    def save(self, *, force: bool = False) -> Path | None:
        """Write the ALTO back (institution formatting) and sync the txt.

        Returns the path of the regenerated txt, or ``None`` when the object
        has no ``Text/`` folder (``--no-plaintext`` outputs).

        Raises ``ConcurrentEditError`` when the file on disk changed after
        this page was loaded (another session's save would be silently lost
        otherwise). ``force=True`` skips the check for a deliberate
        overwrite; a successful save re-baselines the check.
        """
        if not force:
            try:
                now_ns = self.alto_path.stat().st_mtime_ns
            except OSError:
                now_ns = None  # file vanished — let the write recreate it
            if now_ns is not None and now_ns != self._loaded_mtime_ns:
                raise ConcurrentEditError(
                    f"{self.alto_path.name} changed on disk after this page was "
                    "opened (another review session, or an external tool?). "
                    "Reload the page to pick up the newer version, or force-save "
                    "to overwrite it with your edits."
                )
        self._recompute_pc()
        self._stamp_provenance()
        # The session's edits are folded into the tree's cumulative count
        # now — zero the counters BEFORE the writes, so a failed txt write
        # followed by a retried save can't fold the same edits in twice
        # (_stamp_provenance re-reads the tree's totals as `prior`).
        self._before.clear()
        self.session_moves = 0
        serialize_with_tabs(self.tree, self.alto_path)
        self._loaded_mtime_ns = self.alto_path.stat().st_mtime_ns
        self._clean_state = etree.tostring(self.root)

        txt_path: Path | None = None
        text_dir = self.alto_path.parent.parent / "Text"
        if text_dir.is_dir():
            txt_path = text_dir / f"{self.alto_path.stem}.txt"
            atomic_write_text(txt_path, alto_to_text(self.root))

        self.dirty = False
        return txt_path

    def _recompute_pc(self) -> None:
        """PC = mean of current WCs (human-verified words count as 1.00), so
        triage re-ranks partially corrected pages below untouched ones.
        Left alone when no word carries WC (unscored GLMOCR pages)."""
        wcs = [w.wc for w in self.words if w.wc is not None]
        if wcs:
            self.page.set("PC", f"{sum(wcs) / len(wcs):.2f}")
            reorder_attrs(self.page, ATTR_ORDER["Page"])

    def _stamp_provenance(self) -> None:
        desc = self.root.find(f"./{qname(self.ns, 'Description')}")
        if desc is None:
            return  # non-institution ALTO; don't invent structure
        proc = None
        for el in desc.findall(qname(self.ns, "Processing")):
            if el.get("ID") == "review":
                proc = el
                break
        prior = 0
        prior_moves = 0
        prior_verifies = 0
        if proc is None:
            proc = etree.SubElement(desc, qname(self.ns, "Processing"), {"ID": "review"})
            for tag in ("processingDateTime", "processingAgency", "processingStepDescription"):
                etree.SubElement(proc, qname(self.ns, tag))
        else:
            step = proc.find(qname(self.ns, "processingStepDescription"))
            if step is not None and step.text:
                # Each clause is parsed by its own verb: the corrected count
                # stays the leading "(\d+) word" for anything reading these
                # files loosely, but "N word(s) confirmed" can't be mistaken
                # for it here.
                m = re.search(r"(\d+) word\(s\) corrected", step.text) or \
                    re.search(r"(\d+) word", step.text)
                prior = int(m.group(1)) if m else 0
                m = re.search(r"(\d+) word\(s\) confirmed", step.text)
                prior_verifies = int(m.group(1)) if m else 0
                m = re.search(r"(\d+) reading-order", step.text)
                prior_moves = int(m.group(1)) if m else 0

        corrected, confirmed = self._net_counts()
        total = prior + corrected
        total_moves = prior_moves + self.session_moves
        total_verifies = prior_verifies + confirmed
        # Corrected leads, so the loose "(\d+) word" parse above still finds
        # it; the moves clause never contains "word" at all.
        step_text = f"human review: {total} word(s) corrected"
        if total_verifies:
            step_text += f"; {total_verifies} word(s) confirmed"
        if total_moves:
            step_text += f"; {total_moves} reading-order change(s)"
        proc.find(qname(self.ns, "processingDateTime")).text = (
            datetime.now(timezone.utc).isoformat()
        )
        proc.find(qname(self.ns, "processingAgency")).text = "alto_ocr"
        proc.find(qname(self.ns, "processingStepDescription")).text = step_text


def alto_to_text(root: etree._Element) -> str:
    """Canonical transcript rendering of a (corrected) ALTO tree.

    Lines are space-joined String CONTENTs, blocks are blank-line separated —
    the same shape Tesseract's own txt renderer produces for flowing text.
    Used by the review save path and the repackage command, so every tool
    derives the identical Text/ file from a given ALTO.

    Namespace-agnostic (``{*}`` matches any namespace and none): default-ns,
    prefixed (external editors), and namespace-less ALTO all render alike —
    a prefixed file must never silently render as an empty transcript.
    """
    return transcript_spans(root)[0]


def transcript_spans(
    root: etree._Element,
) -> tuple[str, list[tuple[etree._Element, int, int]]]:
    """``alto_to_text``'s transcript plus where each word sits in it:
    ``(String, start, end)`` character offsets, in reading order. The Review
    tab's reading-order panel shows the transcript and maps clicks in it
    back to words; rendering both from one function keeps the panel showing
    exactly the text a save will write."""
    blocks: list[list[list[etree._Element]]] = []
    for tb in root.iter("{*}TextBlock"):
        lines = []
        for tl in tb.iter("{*}TextLine"):
            words = [s for s in tl.iter("{*}String") if s.get("CONTENT", "")]
            if words:
                lines.append(words)
        if lines:
            blocks.append(lines)
    out: list[str] = []
    spans: list[tuple[etree._Element, int, int]] = []
    at = 0
    for bi, lines in enumerate(blocks):
        for li, words in enumerate(lines):
            for wi, s in enumerate(words):
                sep = " " if wi else "\n" if li else "\n\n" if bi else ""
                content = s.get("CONTENT")
                out.append(sep + content)
                at += len(sep)
                spans.append((s, at, at + len(content)))
                at += len(content)
    return ("".join(out) + "\n" if blocks else ""), spans


@dataclass
class RepackageResult:
    """Outcome of repackaging one object from its (corrected) ALTO."""

    ref_id: str
    object_dir: Path
    pages: int
    txt_written: int          # transcripts created or updated (identical files untouched)
    mets_written: bool
    problems: list[str] = field(default_factory=list)
    # Stems found changed outside this tool and stamped as reviewed, so the
    # re-OCR guard keeps protecting them after the METS is refreshed.
    externally_edited: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def iter_object_dirs(output_root: Path) -> list[Path]:
    """Object directories under an output root, per the output contract:
    ``<root>/<RefID>/ALTO/`` — or ``root`` itself when pointed at one object."""
    if (output_root / "ALTO").is_dir():
        return [output_root]
    return [d for d in sorted(output_root.iterdir())
            if d.is_dir() and (d / "ALTO").is_dir()]


def mets_alto_checksums(mets_path: Path) -> dict[str, str]:
    """``{stem: MD5}`` for the ALTO files an existing METS records.

    The METS is the pipeline's own record of the bytes it last packaged, so
    it doubles as a fingerprint store — no extra state to keep in sync.
    Empty when the METS is unreadable, absent, or was written with
    ``--no-checksums`` (nothing to compare against).
    """
    try:
        root = etree.parse(str(mets_path), hardened_parser()).getroot()
    except (OSError, etree.XMLSyntaxError) as e:
        logger.debug("Could not read METS %s for checksums (%s)", mets_path, e)
        return {}
    out: dict[str, str] = {}
    for file_el in root.iter(f"{{{METS_NS}}}file"):
        cksum = file_el.get("CHECKSUM")
        if not cksum:
            continue
        for flocat in file_el.iter(f"{{{METS_NS}}}FLocat"):
            href = flocat.get(f"{{{XLINK_NS}}}href", "")
            if href.startswith("ALTO/") and href.endswith(".xml"):
                out[Path(href).stem] = cksum.lower()
    return out


def pages_edited_since_mets(object_dir: Path) -> list[str]:
    """Stems whose ALTO on disk no longer matches the METS's recorded MD5.

    These are pages some *other* tool corrected — Goobi workflow's built-in
    ALTO editor, eScriptorium, a hand-edit. Such edits carry no
    ``<Processing ID="review">`` (Goobi's ``AltoSaver`` writes ``CONTENT``
    and nothing else), so provenance alone cannot see them and a re-run
    would destroy human work silently.

    Returns ``[]`` when there is no METS or it carries no checksums — the
    detection is only as good as the fingerprint, and a run that opted out
    of METS or checksums has none. Provenance remains the primary signal;
    this is the safety net under it.
    """
    object_dir = Path(object_dir)
    recorded = mets_alto_checksums(object_dir / f"{object_dir.name}.mets.xml")
    if not recorded:
        return []
    alto_dir = object_dir / "ALTO"
    edited = []
    for stem, want in recorded.items():
        current = _checksum_md5(alto_dir / f"{stem}.xml")
        if current is not None and current.lower() != want:
            edited.append(stem)
    return sorted(edited, key=natural_chunks)


def stamp_external_review(alto_path: Path) -> bool:
    """Record that *something outside this tool* corrected ``alto_path``.

    Called by ``repackage_object`` for pages whose bytes drifted from the
    METS. Without this the protection would be temporary: repackage
    refreshes the METS checksums, the drift disappears, and the next
    re-run overwrites the edit. Stamping converts a transient
    byte-difference into the durable signal the guard already understands.

    Returns True when a stamp was written. Pages that already carry review
    provenance are left alone — our own GUI's record is more specific than
    anything inferrable here.
    """
    try:
        tree = etree.parse(str(alto_path), hardened_parser(remove_blank_text=True))
    except (OSError, etree.XMLSyntaxError):
        return False
    root = tree.getroot()
    ns = alto_namespace(root)
    desc = root.find(f"./{qname(ns, 'Description')}")
    if desc is None:
        return False  # non-institution ALTO; don't invent structure
    for el in desc.findall(qname(ns, "Processing")):
        if el.get("ID") == "review":
            return False
    proc = etree.SubElement(desc, qname(ns, "Processing"), {"ID": "review"})
    etree.SubElement(proc, qname(ns, "processingDateTime")).text = (
        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    etree.SubElement(proc, qname(ns, "processingAgency")).text = "alto_ocr"
    etree.SubElement(proc, qname(ns, "processingStepDescription")).text = (
        "external edit: ALTO changed outside this tool (detected by METS "
        "checksum drift); word count unknown")
    serialize_with_tabs(tree, alto_path)
    return True


def _mets_page_order(mets_path: Path) -> list[str] | None:
    """Page stems in the existing METS structMap's order (LABEL attrs sorted
    by ORDER). ``None`` when the METS is unreadable or lists no pages —
    callers fall back to natural stem order."""
    try:
        root = etree.parse(str(mets_path), hardened_parser()).getroot()
    except (OSError, etree.XMLSyntaxError) as e:
        logger.warning("Could not read existing METS %s (%s); page order will "
                       "fall back to natural stem sort", mets_path, e)
        return None
    divs = [el for el in root.iter(f"{{{METS_NS}}}div") if el.get("TYPE") == "page"]

    def order_key(el: etree._Element) -> int:
        try:
            return int(el.get("ORDER", ""))
        except ValueError:
            return 1 << 30  # unordered divs sink to the end, document order kept

    stems = [el.get("LABEL") for el in sorted(divs, key=order_key)]
    stems = [s for s in stems if s]
    return stems or None


def _mets_createdate(mets_path: Path) -> str | None:
    """CREATEDATE from the existing METS header, so a rebuild can preserve
    it. Rebuilding must not lie about provenance: the file was *created*
    once; every repackage after that is a modification (``LASTMODDATE``)."""
    try:
        root = etree.parse(str(mets_path), hardened_parser()).getroot()
    except (OSError, etree.XMLSyntaxError):
        return None
    hdr = root.find(f"{{{METS_NS}}}metsHdr")
    return hdr.get("CREATEDATE") if hdr is not None else None


def repackage_object(
    object_dir: Path,
    *,
    with_checksums: bool = True,
    write_mets_file: bool = True,
) -> RepackageResult:
    """Regenerate everything derived from an object's (corrected) ALTO.

    After review — in the GUI or any external editor — the ALTO is the
    source of truth: transcripts are re-rendered through ``alto_to_text``
    (write skipped when the file is already byte-identical, so re-running
    is free) and the METS is rebuilt so its sizes/checksums match the
    edited files. The existing METS's structMap page order is preserved
    (stem sorting can't reconstruct the original nested-folder order);
    pages new on disk are appended in natural order, and METS entries with
    no ALTO on disk are dropped with a note. Objects without a METS
    (``--no-mets`` runs) get transcripts only — repackage doesn't invent
    files the original run opted out of; the same applies to ``Text/``.
    """
    object_dir = Path(object_dir)
    alto_dir = object_dir / "ALTO"
    result = RepackageResult(
        ref_id=object_dir.name, object_dir=object_dir,
        pages=0, txt_written=0, mets_written=False,
    )

    stems = [p.stem for p in sorted(alto_dir.glob("*.xml"), key=natural_key)]
    result.pages = len(stems)
    if not stems:
        result.problems.append("no ALTO pages found")
        return result

    # Detect drift BEFORE the METS is rebuilt: refreshing its checksums
    # erases the only evidence an external editor leaves, so the transient
    # byte-difference has to become a durable stamp here or protection is
    # lost the moment this function finishes.
    for stem in pages_edited_since_mets(object_dir):
        if stamp_external_review(alto_dir / f"{stem}.xml"):
            result.externally_edited.append(stem)
            logger.info("[%s] %s changed outside this tool — stamped as reviewed",
                        result.ref_id, stem)

    text_dir = object_dir / "Text"
    has_text = text_dir.is_dir()
    for stem in stems:
        alto_path = alto_dir / f"{stem}.xml"
        try:
            root = etree.parse(str(alto_path), hardened_parser()).getroot()
        except (OSError, etree.XMLSyntaxError) as e:
            result.problems.append(f"{stem}: unreadable ALTO ({e})")
            continue
        if has_text:
            txt = alto_to_text(root)
            txt_path = text_dir / f"{stem}.txt"
            try:
                current = txt_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if current != txt:
                atomic_write_text(txt_path, txt)
                result.txt_written += 1

    if write_mets_file:
        mets_path = object_dir / f"{object_dir.name}.mets.xml"
        if mets_path.is_file():
            created = _mets_createdate(mets_path)
            on_disk = set(stems)
            old_order = _mets_page_order(mets_path) or []
            missing = [s for s in old_order if s not in on_disk]
            if missing:
                result.problems.append(
                    "METS listed page(s) with no ALTO on disk, dropped: "
                    + ", ".join(missing)
                )
            ordered = [s for s in old_order if s in on_disk]
            ordered += [s for s in stems if s not in set(ordered)]
            pages = [PageEntry(stem=s, order=i) for i, s in enumerate(ordered, start=1)]
            tree = build_mets(
                object_dir.name, object_dir, pages,
                with_checksums=with_checksums,
                with_plaintext=has_text,
            )
            hdr = tree.getroot().find(f"{{{METS_NS}}}metsHdr")
            if hdr is not None:
                if created:
                    hdr.set("CREATEDATE", created)
                hdr.set("LASTMODDATE", datetime.now(timezone.utc)
                        .replace(microsecond=0).isoformat())
            write_mets(tree, mets_path)
            result.mets_written = True
        else:
            logger.info("[%s] no existing METS — skipping METS regeneration "
                        "(original run used --no-mets?)", object_dir.name)

    return result


def index_source_images(source_dir: Path) -> dict[str, Path]:
    """Recursive stem → image-path index of a source (preservation) folder.

    Accepts the walker's image suffixes case-insensitively and skips Windows
    junk. Duplicate stems keep the first hit with a warning — the object
    passed the walker's hard collision check at OCR time, so a duplicate
    here usually means the chosen folder is broader than one object.
    """
    index: dict[str, Path] = {}
    for dirpath, _dirnames, filenames in os.walk(source_dir):
        for name in sorted(filenames):
            if name.lower().startswith("thumbs.db"):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if p.stem in index:
                logger.warning("Duplicate image stem %r under %s — keeping %s",
                               p.stem, source_dir, index[p.stem])
                continue
            index[p.stem] = p
    return index
