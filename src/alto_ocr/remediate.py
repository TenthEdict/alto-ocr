"""Headless remediation logic: load, edit, and save reviewed ALTO pages.

The review GUI (``review.py``) is a thin Tk layer over this module so the
edit/save semantics stay unit-testable without a display. The planned
``repackage`` command will reuse ``alto_to_text`` so every tool renders the
same canonical transcript from a corrected ALTO.

Semantics (agreed 2026-07-30):

* **Editing a word** sets its ``CONTENT`` and stamps ``WC="1.00"`` — after a
  human verifies a word, the confidence is no longer an OCR estimate. This
  also drops the word out of every low-confidence view (GUI stepping,
  triage) so remaining effort concentrates on what nobody has looked at.
* **Deleting a word** (OCR hallucinated text on noise/marginalia) removes
  the ``<String>`` plus its neighboring ``<SP>``, and cascades upward:
  a ``<TextLine>``/``<TextBlock>``/``<ComposedBlock>`` left with no text
  content is removed too, keeping the ALTO schema-shaped.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .ocr import _ATTR_ORDER, _qname, _reorder_attrs, _serialize_with_tabs
from .walker import IMAGE_SUFFIXES

logger = logging.getLogger(__name__)


class StubPageError(ValueError):
    """Raised when a page is a pipeline failure stub (no ``<Page>``) — there
    is nothing to review; the page needs a re-run, not correction."""


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


class ReviewPage:
    """One ALTO file held open for editing, with pixel-space word geometry.

    ``image_size`` is the source image's ``(width, height)``; ALTO boxes
    (mm10 after institution conversion, pixels pre-conversion) are scaled by
    image-size / Page-dimensions exactly like ``alto_parse`` does for the
    QA overlays.
    """

    def __init__(self, alto_path: Path, image_size: tuple[int, int]) -> None:
        self.alto_path = Path(alto_path)
        parser = etree.XMLParser(remove_blank_text=True)
        self.tree = etree.parse(str(self.alto_path), parser)
        self.root = self.tree.getroot()
        self.ns = self.root.nsmap.get(None)

        self.page = self.root.find(f".//{_qname(self.ns, 'Page')}")
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
        self.sx = img_w / page_w
        self.sy = img_h / page_h

        self.words: list[Word] = []
        for el in self.root.iter(_qname(self.ns, "String")):
            try:
                h, v = int(el.get("HPOS", "")), int(el.get("VPOS", ""))
                w, ht = int(el.get("WIDTH", "")), int(el.get("HEIGHT", ""))
            except ValueError:
                continue  # malformed box: not clickable, leave untouched
            self.words.append(Word(el, (h * self.sx, v * self.sy, w * self.sx, ht * self.sy)))

        self.dirty = False
        self.session_edits = 0  # edits+deletions since load, folded into provenance on save

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

    # ---------- edits -----------------------------------------------------

    def edit_word(self, word: Word, new_text: str) -> None:
        """Set a word's CONTENT (whitespace-collapsed) and mark it verified."""
        text = " ".join(new_text.split())
        if not text:
            raise ValueError("Empty text — use delete_word to remove a word.")
        word.elem.set("CONTENT", text)
        word.elem.set("WC", "1.00")
        _reorder_attrs(word.elem, _ATTR_ORDER["String"])
        self.dirty = True
        self.session_edits += 1

    def delete_word(self, word: Word) -> None:
        """Remove a hallucinated word (plus a neighboring SP; cascade empties)."""
        el = word.elem
        parent = el.getparent()
        sp_tag = _qname(self.ns, "SP")
        prev, nxt = el.getprevious(), el.getnext()
        if prev is not None and prev.tag == sp_tag:
            parent.remove(prev)
        elif nxt is not None and nxt.tag == sp_tag:
            parent.remove(nxt)
        parent.remove(el)

        # Cascade: drop text containers left with no String anywhere below.
        # Only TextLine/TextBlock/ComposedBlock — never PrintSpace/Page/margins.
        string_tag = _qname(self.ns, "String")
        removable = {"TextLine", "TextBlock", "ComposedBlock"}
        node = parent
        while node is not None and etree.QName(node).localname in removable:
            up = node.getparent()
            if next(node.iter(string_tag), None) is None:
                up.remove(node)
            node = up

        self.words.remove(word)
        self.dirty = True
        self.session_edits += 1

    def revert_word(self, word: Word, old_content: str, old_wc: float | None) -> None:
        """Undo one ``edit_word`` (GUI undo stack): restore the exact prior
        CONTENT/WC and un-count the edit. ``dirty`` stays as-is — other
        unsaved changes may exist; reloading the page is the full reset."""
        word.elem.set("CONTENT", old_content)
        if old_wc is None:
            word.elem.attrib.pop("WC", None)
        else:
            word.elem.set("WC", f"{old_wc:.2f}")
        _reorder_attrs(word.elem, _ATTR_ORDER["String"])
        self.session_edits = max(0, self.session_edits - 1)

    # ---------- save ------------------------------------------------------

    def save(self) -> Path | None:
        """Write the ALTO back (institution formatting) and sync the txt.

        Returns the path of the regenerated txt, or ``None`` when the object
        has no ``Text/`` folder (``--no-plaintext`` outputs).
        """
        self._recompute_pc()
        self._stamp_provenance()
        _serialize_with_tabs(self.tree, self.alto_path)

        txt_path: Path | None = None
        text_dir = self.alto_path.parent.parent / "Text"
        if text_dir.is_dir():
            txt_path = text_dir / f"{self.alto_path.stem}.txt"
            txt_path.write_text(alto_to_text(self.root, self.ns), encoding="utf-8")

        self.dirty = False
        self.session_edits = 0
        return txt_path

    def _recompute_pc(self) -> None:
        """PC = mean of current WCs (human-verified words count as 1.00), so
        triage re-ranks partially corrected pages below untouched ones.
        Left alone when no word carries WC (unscored GLMOCR pages)."""
        wcs = [w.wc for w in self.words if w.wc is not None]
        if wcs:
            self.page.set("PC", f"{sum(wcs) / len(wcs):.2f}")
            _reorder_attrs(self.page, _ATTR_ORDER["Page"])

    def _stamp_provenance(self) -> None:
        desc = self.root.find(f"./{_qname(self.ns, 'Description')}")
        if desc is None:
            return  # non-institution ALTO; don't invent structure
        proc = None
        for el in desc.findall(_qname(self.ns, "Processing")):
            if el.get("ID") == "review":
                proc = el
                break
        prior = 0
        if proc is None:
            proc = etree.SubElement(desc, _qname(self.ns, "Processing"), {"ID": "review"})
            for tag in ("processingDateTime", "processingAgency", "processingStepDescription"):
                etree.SubElement(proc, _qname(self.ns, tag))
        else:
            step = proc.find(_qname(self.ns, "processingStepDescription"))
            if step is not None and step.text:
                m = re.search(r"(\d+) word", step.text)
                prior = int(m.group(1)) if m else 0

        total = prior + self.session_edits
        proc.find(_qname(self.ns, "processingDateTime")).text = (
            datetime.now(timezone.utc).isoformat()
        )
        proc.find(_qname(self.ns, "processingAgency")).text = "alto_ocr"
        proc.find(_qname(self.ns, "processingStepDescription")).text = (
            f"human review: {total} word(s) corrected"
        )


def alto_to_text(root: etree._Element, ns: str | None) -> str:
    """Canonical transcript rendering of a (corrected) ALTO tree.

    Lines are space-joined String CONTENTs, blocks are blank-line separated —
    the same shape Tesseract's own txt renderer produces for flowing text.
    Used by the review save path and (later) the repackage command, so every
    tool derives the identical Text/ file from a given ALTO.
    """
    blocks: list[str] = []
    for tb in root.iter(_qname(ns, "TextBlock")):
        lines: list[str] = []
        for tl in tb.iter(_qname(ns, "TextLine")):
            words = [s.get("CONTENT", "") for s in tl.iter(_qname(ns, "String"))]
            line = " ".join(w for w in words if w)
            if line:
                lines.append(line)
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


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
