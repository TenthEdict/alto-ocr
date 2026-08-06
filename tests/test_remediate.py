"""Tests for the headless review/remediation logic (remediate.py).

Covers: mm10→pixel word geometry, edit semantics (CONTENT + WC=1.00 +
attribute order), whitespace handling, deletion with SP cleanup and
container cascade (stopping at PrintSpace), page clearing (clear_words:
bulk rejection of hallucinated pages, PC drop, empty triage status),
word insertion (add_word: line/block/PrintSpace placement, SP rebuild,
fresh institution IDs, bbox expansion, undo_add), undo via revert_word,
reading-order moves (move_element/undo_move/flow_children: pure sibling
relocation with geometry/IDs/SPs untouched, ComposedBlocks as atomic
units, the separate provenance count), save round-trip (tab
serialization, PC recompute, review provenance with cumulative counts,
Text/ regeneration), the canonical ALTO→text rendering, stub detection,
and the source-image index.

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
    has_review_provenance,
    index_source_images,
)
from src.alto_ocr.triage import scan_alto_file  # noqa: E402

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
_SY = 3300 / 2794


def _px_box(hpos: float, vpos: float, w: float, h: float) -> tuple:
    """An image-pixel box that maps back to the given ALTO (mm10) box —
    what the GUI hands add_word after the corrector draws on the scan."""
    return (hpos * _SX, vpos * _SY, w * _SX, h * _SY)


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


class ClearPageTests(_PageFixture):
    def test_clear_removes_all_words_keeps_skeleton(self) -> None:
        removed = self.page.clear_words()
        self.assertEqual(removed, 4)
        self.assertEqual(self.page.words, [])
        self.assertTrue(self.page.dirty)
        self.assertEqual(self.page.session_edits, 4)
        xml = etree.tostring(self.page.root).decode()
        self.assertNotIn("String", xml)
        self.assertNotIn("TextBlock", xml)
        self.assertIn("P1_PS00001", xml)             # PrintSpace survives
        self.assertIsNone(self.page.page.get("PC"))  # stale score dropped

    def test_clear_save_renders_empty_txt_and_triage_empty(self) -> None:
        self.page.clear_words()
        txt_path = self.page.save()
        self.assertEqual(txt_path.read_text(encoding="utf-8"), "")
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 4 word(s) corrected", body)
        self.assertNotIn('PC="', body)
        info = scan_alto_file(self.alto)
        self.assertEqual(info.status, "empty")
        self.assertEqual(info.words, 0)

    def test_clear_on_empty_page_is_a_noop(self) -> None:
        self.page.clear_words()
        self.page.save()
        reopened = ReviewPage(self.alto, _IMG_SIZE)
        self.assertEqual(reopened.clear_words(), 0)
        self.assertFalse(reopened.dirty)

    def test_add_word_after_clear_builds_block_in_printspace(self) -> None:
        # Cover-page workflow: wipe the garbage, then hand-add the few
        # words the page really shows (spine labels, stamps).
        self.page.clear_words()
        word = self.page.add_word(_px_box(500, 400, 300, 60), "SPECIFICATIONS")
        self.assertEqual([w.content for w in self.page.words], ["SPECIFICATIONS"])
        line = word.elem.getparent()
        self.assertEqual(etree.QName(line).localname, "TextLine")
        block = line.getparent()
        self.assertEqual(etree.QName(block).localname, "TextBlock")
        self.assertEqual(etree.QName(block.getparent()).localname, "PrintSpace")


class AddWordTests(_PageFixture):
    def test_add_into_existing_line(self) -> None:
        # Box to the right of "cf" inside TL00001's vertical band.
        word = self.page.add_word(_px_box(500, 105, 100, 55), " and ")
        self.assertEqual(word.content, "and")
        self.assertEqual(word.elem.get("WC"), "1.00")
        self.assertEqual(word.elem.get("ID"), "P1_ST00005")  # past max ST
        self.assertTrue(self.page.dirty)
        self.assertEqual(self.page.session_edits, 1)
        # Reading order maintained in self.words (line 1 gains the word).
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "and", "1927", "Lone"])
        # Landed in TL00001, after "cf", with the SP run rebuilt from the
        # current gaps: Board..cf gap 20, cf..and gap 30.
        line = word.elem.getparent()
        self.assertEqual(line.get("ID"), "P1_TL00001")
        kinds = [(etree.QName(k).localname, k.get("CONTENT")) for k in line]
        self.assertEqual(kinds, [("String", "Board"), ("SP", None),
                                 ("String", "cf"), ("SP", None),
                                 ("String", "and")])
        sps = line.findall("{*}SP")
        self.assertEqual([sp.get("WIDTH") for sp in sps], ["20", "30"])
        # Verified words aren't flagged.
        self.assertNotIn(word, self.page.flagged(0.99))

    def test_add_makes_new_line_in_overlapping_block(self) -> None:
        # Below both of TB00001's lines but overlapping its bbox (y 100-300).
        word = self.page.add_word(_px_box(150, 285, 200, 20), "added")
        line = word.elem.getparent()
        block = line.getparent()
        self.assertEqual(etree.QName(line).localname, "TextLine")
        self.assertEqual(line.get("ID"), "P1_TL00004")
        self.assertEqual(block.get("ID"), "P1_TB00001")
        # Appended after the block's existing lines (VPOS order)…
        self.assertEqual([tl.get("ID") for tl in block.findall("{*}TextLine")],
                         ["P1_TL00001", "P1_TL00002", "P1_TL00004"])
        # …and the block bbox grew to cover the word (y2 300 -> 305).
        self.assertEqual(block.get("HEIGHT"), "205")
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "added", "Lone"])

    def test_add_far_from_everything_makes_new_block(self) -> None:
        word = self.page.add_word(_px_box(1200, 1000, 300, 60), "margin")
        line = word.elem.getparent()
        block = line.getparent()
        self.assertEqual(block.get("ID"), "P1_TB00003")
        self.assertEqual(etree.QName(block.getparent()).localname, "PrintSpace")
        # After TB00002 (VPOS 500 < 1000) in reading order.
        ps = block.getparent()
        self.assertEqual([tb.get("ID") for tb in ps.findall("{*}TextBlock")],
                         ["P1_TB00001", "P1_TB00002", "P1_TB00003"])
        self.assertEqual(self.page.words[-1].content, "margin")
        self.assertEqual(alto_to_text(self.page.root),
                         "Board cf\n1927\n\nLone\n\nmargin\n")

    def test_add_empty_text_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.page.add_word(_px_box(500, 105, 100, 55), "   ")
        self.assertFalse(self.page.dirty)

    def test_undo_add_restores_document(self) -> None:
        word = self.page.add_word(_px_box(500, 105, 100, 55), "and")
        self.page.undo_add(word)
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "Lone"])
        self.assertEqual(self.page.session_edits, 0)
        xml = etree.tostring(self.page.root).decode()
        self.assertNotIn('CONTENT="and"', xml)
        # The line keeps exactly one SP between its two surviving words.
        line = self.page.words[0].elem.getparent()
        self.assertEqual(len(line.findall("{*}SP")), 1)

    def test_save_after_add_updates_pc_provenance_and_txt(self) -> None:
        self.page.add_word(_px_box(500, 105, 100, 55), "and")
        txt_path = self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn('CONTENT="and" WC="1.00"', body)
        # PC = mean(0.96, 0.40, 1.00, 0.85) = 0.8025 -> 0.80.
        self.assertIn('PC="0.80"', body)
        self.assertIn("human review: 1 word(s) corrected", body)
        self.assertEqual(txt_path.read_text(encoding="utf-8"),
                         "Board cf and\n1927\n\nLone\n")


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


# ComposedBlock variant: two blocks grouped in a ComposedBlock plus one
# standalone block, for the "CB moves as one atomic unit" semantics.
_ALTO_CB = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="0.90">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<ComposedBlock ID="P1_CB00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="400">
\t\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="100">
\t\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="300" HEIGHT="80">
\t\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Alpha" WC="0.90"/>
\t\t\t\t\t\t</TextLine>
\t\t\t\t\t</TextBlock>
\t\t\t\t\t<TextBlock ID="P1_TB00002" HPOS="100" VPOS="300" WIDTH="800" HEIGHT="100">
\t\t\t\t\t\t<TextLine ID="P1_TL00002" HPOS="100" VPOS="300" WIDTH="300" HEIGHT="80">
\t\t\t\t\t\t\t<String ID="P1_ST00002" HPOS="100" VPOS="300" WIDTH="200" HEIGHT="60" CONTENT="Beta" WC="0.90"/>
\t\t\t\t\t\t</TextLine>
\t\t\t\t\t</TextBlock>
\t\t\t\t</ComposedBlock>
\t\t\t\t<TextBlock ID="P1_TB00003" HPOS="100" VPOS="600" WIDTH="800" HEIGHT="100">
\t\t\t\t\t<TextLine ID="P1_TL00003" HPOS="100" VPOS="600" WIDTH="300" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00003" HPOS="100" VPOS="600" WIDTH="200" HEIGHT="60" CONTENT="Gamma" WC="0.90"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""


def _geometry_snapshot(root: etree._Element) -> list[tuple]:
    """(ID, HPOS, VPOS, WIDTH, HEIGHT) of every ID-carrying element —
    a move must leave all of it byte-identical."""
    return sorted(
        (el.get("ID"), el.get("HPOS"), el.get("VPOS"),
         el.get("WIDTH"), el.get("HEIGHT"))
        for el in root.iter() if el.get("ID") is not None
    )


class MoveTests(_PageFixture):
    def test_move_block_swaps_transcript_order(self) -> None:
        tb2 = self.page.flow_children()[1]
        self.assertEqual(tb2.get("ID"), "P1_TB00002")
        self.assertTrue(self.page.move_element(tb2, -1))
        self.assertEqual(alto_to_text(self.page.root),
                         "Lone\n\nBoard cf\n1927\n")
        # self.words re-sorted into the new document order.
        self.assertEqual([w.content for w in self.page.words],
                         ["Lone", "Board", "cf", "1927"])
        self.assertTrue(self.page.dirty)
        self.assertEqual(self.page.session_moves, 1)
        self.assertEqual(self.page.session_edits, 0)

    def test_move_line_within_block(self) -> None:
        tb1 = self.page.flow_children()[0]
        lines = self.page.flow_children(tb1)
        self.assertTrue(self.page.move_element(lines[1], -1))  # "1927" up
        self.assertEqual(alto_to_text(self.page.root),
                         "1927\nBoard cf\n\nLone\n")
        self.assertEqual([tl.get("ID") for tl in tb1.findall("{*}TextLine")],
                         ["P1_TL00002", "P1_TL00001"])
        self.assertEqual([w.content for w in self.page.words],
                         ["1927", "Board", "cf", "Lone"])

    def test_move_at_boundary_is_noop(self) -> None:
        before = etree.tostring(self.page.root)
        blocks = self.page.flow_children()
        self.assertFalse(self.page.move_element(blocks[0], -1))
        self.assertFalse(self.page.move_element(blocks[-1], +1))
        self.assertFalse(self.page.move_element(blocks[0], 0))
        self.assertFalse(self.page.dirty)
        self.assertEqual(self.page.session_moves, 0)
        self.assertEqual(etree.tostring(self.page.root), before)

    def test_move_preserves_geometry_ids_and_sps(self) -> None:
        before = _geometry_snapshot(self.page.root)
        self.page.move_element(self.page.flow_children()[1], -1)
        self.assertEqual(_geometry_snapshot(self.page.root), before)
        self.assertEqual(len(self.page.root.findall(".//{*}SP")), 1)

    def test_undo_move_restores_order_and_count(self) -> None:
        tb2 = self.page.flow_children()[1]
        self.page.move_element(tb2, -1)
        self.assertTrue(self.page.undo_move(tb2, -1))
        self.assertEqual(alto_to_text(self.page.root),
                         "Board cf\n1927\n\nLone\n")
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "Lone"])
        self.assertEqual(self.page.session_moves, 0)
        self.assertTrue(self.page.dirty)  # like revert_word: reload is the full reset

    def test_flow_children_levels(self) -> None:
        blocks = self.page.flow_children()
        self.assertEqual([b.get("ID") for b in blocks],
                         ["P1_TB00001", "P1_TB00002"])
        lines = self.page.flow_children(blocks[0])
        self.assertEqual([tl.get("ID") for tl in lines],
                         ["P1_TL00001", "P1_TL00002"])
        # No PrintSpace (skeleton edge case) -> no flow, not an error.
        ps = self.page.page.find("{*}PrintSpace")
        self.page.page.remove(ps)
        self.assertEqual(self.page.flow_children(), [])

    def test_box_px_of_and_text_preview(self) -> None:
        tb1 = self.page.flow_children()[0]
        x, y, w, h = self.page.box_px_of(tb1)
        self.assertAlmostEqual(x, 100 * _SX, places=3)
        self.assertAlmostEqual(y, 100 * _SY, places=3)
        self.assertAlmostEqual(w, 800 * _SX, places=3)
        self.assertAlmostEqual(h, 200 * _SY, places=3)
        self.assertEqual(self.page.text_preview(tb1), "Board cf 1927")
        self.assertEqual(self.page.text_preview(tb1, limit=8), "Board cf…")


class MoveComposedBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        obj = Path(self._tmp.name) / "RefCB"
        (obj / "ALTO").mkdir(parents=True)
        self.alto = obj / "ALTO" / "page001.xml"
        self.alto.write_text(_ALTO_CB, encoding="utf-8")
        self.page = ReviewPage(self.alto, _IMG_SIZE)

    def test_composedblock_moves_as_unit(self) -> None:
        flow = self.page.flow_children()
        self.assertEqual([f.get("ID") for f in flow],
                         ["P1_CB00001", "P1_TB00003"])
        self.assertTrue(self.page.move_element(flow[0], +1))
        self.assertEqual(alto_to_text(self.page.root),
                         "Gamma\n\nAlpha\n\nBeta\n")
        self.assertEqual([w.content for w in self.page.words],
                         ["Gamma", "Alpha", "Beta"])

    def test_blocks_reorder_inside_composedblock(self) -> None:
        cb = self.page.flow_children()[0]
        inner = self.page.flow_children(cb)
        self.assertEqual([b.get("ID") for b in inner],
                         ["P1_TB00001", "P1_TB00002"])
        self.assertTrue(self.page.move_element(inner[1], -1))
        self.assertEqual(alto_to_text(self.page.root),
                         "Beta\n\nAlpha\n\nGamma\n")


class MoveSaveTests(_PageFixture):
    def test_save_after_moves_stamps_both_counts(self) -> None:
        by_content = {w.content: w for w in self.page.words}
        self.page.edit_word(by_content["cf"], "of")
        tb2 = self.page.flow_children()[1]
        self.page.move_element(tb2, -1)
        self.page.move_element(tb2, +1)  # two moves, back where it started
        txt_path = self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; "
                      "2 reading-order change(s)", body)
        self.assertEqual(txt_path.read_text(encoding="utf-8"),
                         "Board of\n1927\n\nLone\n")
        # A later session accumulates both counts — the "(\d+) word" parse
        # survives the appended clause.
        reopened = ReviewPage(self.alto, _IMG_SIZE)
        reopened.move_element(reopened.flow_children()[1], -1)
        reopened.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; "
                      "3 reading-order change(s)", body)
        self.assertEqual(body.count('<Processing ID="review">'), 1)

    def test_moves_only_session_gets_provenance(self) -> None:
        self.page.move_element(self.page.flow_children()[1], -1)
        txt_path = self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 0 word(s) corrected; "
                      "1 reading-order change(s)", body)
        # A reordered page is protected from re-OCR like any reviewed page.
        self.assertTrue(has_review_provenance(self.alto))
        # PC is order-independent: mean(0.96, 0.40, 0.85) -> 0.74 as loaded.
        self.assertIn('PC="0.74"', body)
        self.assertEqual(txt_path.read_text(encoding="utf-8"),
                         "Lone\n\nBoard cf\n1927\n")

    def test_no_moves_keeps_legacy_wording(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected", body)
        self.assertNotIn("reading-order", body)


class AltoToTextTests(unittest.TestCase):
    def test_rendering_shape(self) -> None:
        root = etree.fromstring(_ALTO.encode())
        self.assertEqual(alto_to_text(root),
                         "Board cf\n1927\n\nLone\n")

    def test_empty_layout_renders_empty(self) -> None:
        stub = ALTO_FAILURE_STUB.format(when="t", reason="x")
        root = etree.fromstring(stub.encode())
        self.assertEqual(alto_to_text(root), "")


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
