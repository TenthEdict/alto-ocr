"""Tests for the headless review/remediation logic (remediate.py).

Covers: mm10→pixel word geometry, edit semantics (CONTENT + WC=1.00 +
attribute order), whitespace handling, deletion with SP cleanup and
container cascade (stopping at PrintSpace), undo via revert_word, save
round-trip (tab serialization, PC recompute, review provenance with a
cumulative count, Text/ regeneration), the canonical ALTO→text rendering,
stub detection, and the source-image index.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_remediate
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.pipeline import ALTO_FAILURE_STUB  # noqa: E402
from src.alto_ocr.remediate import (  # noqa: E402
    ReviewPage,
    StubPageError,
    alto_to_text,
    index_source_images,
)

# Institution-style ALTO v2 in mm10: Page 2159x2794 (letter), image at
# 300 DPI is 2550x3300 px → sx = 2550/2159, sy = 3300/2794.
_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="0.74">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="200">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Board" WC="0.96"/>
\t\t\t\t\t\t<SP ID="P1_SP00001" HPOS="300" VPOS="100" WIDTH="20"/>
\t\t\t\t\t\t<String ID="P1_ST00002" HPOS="320" VPOS="100" WIDTH="150" HEIGHT="60" CONTENT="cf" WC="0.40"/>
\t\t\t\t\t</TextLine>
\t\t\t\t\t<TextLine ID="P1_TL00002" HPOS="100" VPOS="200" WIDTH="400" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00003" HPOS="100" VPOS="200" WIDTH="180" HEIGHT="60" CONTENT="1927"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t\t<TextBlock ID="P1_TB00002" HPOS="100" VPOS="500" WIDTH="400" HEIGHT="100">
\t\t\t\t\t<TextLine ID="P1_TL00003" HPOS="100" VPOS="500" WIDTH="400" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00004" HPOS="100" VPOS="500" WIDTH="200" HEIGHT="60" CONTENT="Lone" WC="0.85"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

_IMG_SIZE = (2550, 3300)
_SX = 2550 / 2159


class _PageFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.obj = Path(self._tmp.name) / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir()
        self.alto = self.obj / "ALTO" / "page001.xml"
        self.alto.write_text(_ALTO, encoding="utf-8")
        (self.obj / "Text" / "page001.txt").write_text("stale", encoding="utf-8")
        self.page = ReviewPage(self.alto, _IMG_SIZE)


class LoadTests(_PageFixture):
    def test_words_and_pixel_geometry(self) -> None:
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "Lone"])
        x, y, w, h = self.page.words[0].box_px
        self.assertAlmostEqual(x, 100 * _SX, places=3)
        self.assertAlmostEqual(w, 200 * _SX, places=3)

    def test_flagged_includes_low_and_unscored(self) -> None:
        flagged = self.page.flagged(0.50)
        self.assertEqual([w.content for w in flagged], ["cf", "1927"])

    def test_word_at_picks_smallest_hit(self) -> None:
        hit = self.page.word_at(110 * _SX, 110 * (3300 / 2794))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.content, "Board")
        self.assertIsNone(self.page.word_at(5.0, 5.0))

    def test_stub_page_raises(self) -> None:
        stub = self.obj / "ALTO" / "stub.xml"
        stub.write_text(ALTO_FAILURE_STUB.format(when="t", reason="x"), encoding="utf-8")
        with self.assertRaises(StubPageError):
            ReviewPage(stub, _IMG_SIZE)


class EditTests(_PageFixture):
    def test_edit_sets_content_wc_and_order(self) -> None:
        word = self.page.words[1]  # "cf" @ 0.40
        self.page.edit_word(word, "  of \n")
        self.assertEqual(word.content, "of")
        self.assertEqual(word.elem.get("WC"), "1.00")
        self.assertTrue(self.page.dirty)
        self.assertEqual(self.page.session_edits, 1)
        # Reference attribute order: WC right after CONTENT.
        keys = list(word.elem.attrib)
        self.assertLess(keys.index("CONTENT"), keys.index("WC"))

    def test_empty_edit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.page.edit_word(self.page.words[0], "   ")

    def test_revert_restores_exact_state(self) -> None:
        word = self.page.words[2]  # "1927", no WC
        self.page.edit_word(word, "1928")
        self.page.revert_word(word, "1927", None)
        self.assertEqual(word.content, "1927")
        self.assertIsNone(word.elem.get("WC"))
        self.assertEqual(self.page.session_edits, 0)

    def test_delete_removes_sp_and_cascades_but_keeps_printspace(self) -> None:
        by_content = {w.content: w for w in self.page.words}
        self.page.delete_word(by_content["cf"])   # leaves "Board" + SP cleanup
        self.page.delete_word(by_content["Lone"])  # empties line, block cascades

        xml = etree.tostring(self.page.root).decode()
        self.assertNotIn("P1_SP00001", xml)
        self.assertNotIn("P1_TB00002", xml)  # emptied block removed
        self.assertIn("P1_PS00001", xml)     # PrintSpace survives
        self.assertIn("P1_TB00001", xml)     # block with remaining words survives
        self.assertEqual([w.content for w in self.page.words], ["Board", "1927"])


class SaveTests(_PageFixture):
    def test_save_recomputes_pc_stamps_provenance_and_syncs_txt(self) -> None:
        by_content = {w.content: w for w in self.page.words}
        self.page.edit_word(by_content["cf"], "of")
        txt_path = self.page.save()

        self.assertFalse(self.page.dirty)
        body = self.alto.read_text(encoding="utf-8")
        self.assertTrue(body.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIn('CONTENT="of" WC="1.00"', body)
        self.assertIn("\t\t\t<TextBlock", body)  # tab indentation preserved
        # PC = mean(0.96, 1.00, 0.85) over scored words = 0.9366… → 0.94.
        self.assertIn('PC="0.94"', body)
        self.assertIn('<Processing ID="review">', body)
        self.assertIn("human review: 1 word(s) corrected", body)

        self.assertEqual(txt_path, self.obj / "Text" / "page001.txt")
        self.assertEqual(txt_path.read_text(encoding="utf-8"),
                         "Board of\n1927\n\nLone\n")

    def test_second_save_accumulates_review_count(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.save()
        reopened = ReviewPage(self.alto, _IMG_SIZE)
        reopened.edit_word(reopened.words[0], "Board,")
        reopened.edit_word(reopened.words[2], "1928")
        reopened.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 3 word(s) corrected", body)
        self.assertEqual(body.count('<Processing ID="review">'), 1)

    def test_save_without_text_dir_returns_none(self) -> None:
        obj2 = Path(self._tmp.name) / "RefB"
        (obj2 / "ALTO").mkdir(parents=True)
        alto2 = obj2 / "ALTO" / "p.xml"
        alto2.write_text(_ALTO, encoding="utf-8")
        page = ReviewPage(alto2, _IMG_SIZE)
        page.edit_word(page.words[0], "X")
        self.assertIsNone(page.save())


class AltoToTextTests(unittest.TestCase):
    def test_rendering_shape(self) -> None:
        root = etree.fromstring(_ALTO.encode())
        self.assertEqual(alto_to_text(root, root.nsmap.get(None)),
                         "Board cf\n1927\n\nLone\n")

    def test_empty_layout_renders_empty(self) -> None:
        stub = ALTO_FAILURE_STUB.format(when="t", reason="x")
        root = etree.fromstring(stub.encode())
        self.assertEqual(alto_to_text(root, root.nsmap.get(None)), "")


class SourceIndexTests(unittest.TestCase):
    def test_recursive_index_with_junk_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            (root / "page001.TIF").write_bytes(b"x")
            (root / "sub" / "page002.jpg").write_bytes(b"x")
            (root / "sub" / "page001.tif").write_bytes(b"x")  # duplicate stem
            (root / "Thumbs.db").write_bytes(b"x")
            (root / "notes.txt").write_bytes(b"x")
            idx = index_source_images(root)
        self.assertEqual(set(idx), {"page001", "page002"})
        self.assertEqual(idx["page001"].name, "page001.TIF")  # first hit kept


if __name__ == "__main__":
    unittest.main()
