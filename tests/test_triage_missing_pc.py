"""A scored page with no Page/@PC must still reach the review queue.

Tesseract's ALTO renderer emits per-word ``WC`` natively; our confidence merge
re-derives those and adds the page-level ``PC``. If the merge's write fails
(see tests/test_atomic_write_retry.py) the file keeps its words' WC but never
gains PC — and ``review_pages`` filters on ``p.pc is not None``, so the page
silently disappeared from the correction queue. Observed on three pages of
MSA00005_0653bfcb... on 2026-07-30.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_triage_missing_pc
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.triage import scan_alto_file, scan_object  # noqa: E402

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v3#"


def _alto(strings: list[tuple[str, str | None]], pc: str | None = None) -> str:
    """One-line ALTO with the given (CONTENT, WC) words; PC optional."""
    page_attrs = 'ID="page_0" PHYSICAL_IMG_NR="1" WIDTH="1000" HEIGHT="1000"'
    if pc is not None:
        page_attrs += f' PC="{pc}"'
    words = "".join(
        f'<String ID="s{i}" HPOS="{10 * i}" VPOS="10" WIDTH="9" HEIGHT="9"'
        + (f' WC="{wc}"' if wc is not None else "")
        + f' CONTENT="{c}"/>'
        for i, (c, wc) in enumerate(strings)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<alto xmlns="{ALTO_NS}"><Layout><Page {page_attrs}><PrintSpace>'
        f"<TextBlock ID=\"b0\"><TextLine ID=\"l0\">{words}</TextLine></TextBlock>"
        f"</PrintSpace></Page></Layout></alto>"
    )


class TestMissingPageConfidence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, xml: str) -> Path:
        p = self.dir / name
        p.write_text(xml, encoding="utf-8")
        return p

    def test_pc_is_derived_from_word_confidences(self):
        p = self._write("a.xml", _alto([("one", "0.20"), ("two", "0.40")]))
        page = scan_alto_file(p)
        self.assertEqual(page.status, "scored")
        self.assertIsNotNone(page.pc, "page dropped its confidence entirely")
        self.assertAlmostEqual(page.pc, 0.30, places=6)

    def test_stored_pc_still_wins(self):
        """Derivation is a fallback, never an override of what the merge wrote."""
        p = self._write("a.xml", _alto([("one", "0.20"), ("two", "0.40")], pc="0.91"))
        self.assertAlmostEqual(scan_alto_file(p).pc, 0.91, places=6)

    def test_unscored_page_keeps_no_pc(self):
        """GLMOCR output has no WC at all — there is nothing to derive from."""
        p = self._write("a.xml", _alto([("one", None), ("two", None)]))
        page = scan_alto_file(p)
        self.assertEqual(page.status, "unscored")
        self.assertIsNone(page.pc)

    def test_empty_page_keeps_no_pc(self):
        p = self._write("a.xml", _alto([]))
        page = scan_alto_file(p)
        self.assertEqual(page.status, "empty")
        self.assertIsNone(page.pc)

    def test_page_reaches_the_review_queue(self):
        """The whole point: a low-confidence page with no PC must be flagged."""
        alto = self.dir / "ALTO"
        alto.mkdir()
        (alto / "good.xml").write_text(
            _alto([("a", "0.95"), ("b", "0.95")], pc="0.95"), encoding="utf-8")
        (alto / "lost_pc.xml").write_text(
            _alto([("a", "0.20"), ("b", "0.30")]), encoding="utf-8")

        obj = scan_object(self.dir)
        flagged = {p.stem for p in obj.review_pages}
        self.assertIn("lost_pc", flagged,
                      "page with words but no PC never reaches a corrector")
        self.assertNotIn("good", flagged)


if __name__ == "__main__":
    unittest.main()
