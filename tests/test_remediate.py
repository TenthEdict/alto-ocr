"""Tests for the headless review/remediation logic (remediate.py).

Covers: mm10→pixel word geometry, edit semantics (CONTENT + WC=1.00 +
attribute order), confirming a correct-but-flagged word (verify_word:
WC only, its own provenance clause and session counter, undo via
unverify_word), next/previous-word stepping by document position
(step_from/doc_index — a corrected word must not restart the page),
whitespace handling, deletion with SP cleanup and
container cascade (stopping at PrintSpace), exact undo of deletions
(undo_delete; redo_add for a same-session added word), refusal of text
ALTO cannot store (control characters), page clearing (clear_words:
bulk rejection of hallucinated pages, PC drop, empty triage status),
word insertion (add_word: line/block/PrintSpace placement, SP rebuild,
fresh institution IDs, bbox expansion, undo_add), undo via revert_word,
word-level reading-order moves (move_words/nudge_words/undo_move_words:
in-line runs, multi-line runs keeping their line breaks, target-line
splits, re-fitted containers, untouched word boxes, byte-exact undo, the
separate provenance count), transcript_spans, save round-trip (tab
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
    transcript_spans,
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


class LineBlockTests(_PageFixture):
    def test_line_and_block_words(self) -> None:
        by = {w.content: w for w in self.page.words}
        self.assertEqual([w.content for w in self.page.line_words(by["cf"])],
                         ["Board", "cf"])
        self.assertEqual([w.content for w in self.page.block_words(by["1927"])],
                         ["Board", "cf", "1927"])
        self.assertEqual([w.content for w in self.page.block_words(by["Lone"])],
                         ["Lone"])


class DeleteUndoTests(_PageFixture):
    """The review GUI deletes without a confirmation prompt (2026-09-24);
    Ctrl+Z restoring the page *exactly* is what makes that safe."""

    def _snapshot(self) -> tuple[bytes, list]:
        return etree.tostring(self.page.root), list(self.page.words)

    def test_undo_restores_the_document_byte_for_byte(self) -> None:
        before_xml, before_words = self._snapshot()
        word = self.page.words[1]  # "cf": its SP sits before it
        removal = self.page.delete_word(word)
        self.assertNotIn(b"P1_SP00001", etree.tostring(self.page.root))
        self.page.undo_delete(word, removal)
        self.assertEqual(etree.tostring(self.page.root), before_xml)
        # The same Word objects, in the same reading order: undo stacks and
        # canvas items the GUI keyed on them stay valid.
        self.assertEqual(len(self.page.words), len(before_words))
        for got, want in zip(self.page.words, before_words):
            self.assertIs(got, want)
        self.assertEqual(self.page.session_edits, 0)

    def test_undo_brings_back_cascaded_containers_in_place(self) -> None:
        before_xml, _ = self._snapshot()
        by_content = {w.content: w for w in self.page.words}
        # "1927" is alone on TL00002: the line goes, its block stays.
        line_removal = self.page.delete_word(by_content["1927"])
        self.assertNotIn(b"P1_TL00002", etree.tostring(self.page.root))
        # "Lone" is alone in TB00002: line and block both go.
        block_removal = self.page.delete_word(by_content["Lone"])
        self.assertNotIn(b"P1_TB00002", etree.tostring(self.page.root))
        self.page.undo_delete(by_content["Lone"], block_removal)
        self.page.undo_delete(by_content["1927"], line_removal)
        self.assertEqual(etree.tostring(self.page.root), before_xml)
        self.assertEqual(alto_to_text(self.page.root), "Board cf\n1927\n\nLone\n")

    def test_every_word_deleted_then_undone_in_reverse(self) -> None:
        before_xml, _ = self._snapshot()
        undo = [(w, self.page.delete_word(w)) for w in list(self.page.words)]
        self.assertEqual(self.page.words, [])
        self.assertEqual(self.page.session_edits, 4)
        for word, removal in reversed(undo):
            self.page.undo_delete(word, removal)
        self.assertEqual(etree.tostring(self.page.root), before_xml)
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "Lone"])
        self.assertEqual(self.page.session_edits, 0)

    def test_restored_word_is_live_again(self) -> None:
        word = self.page.words[1]
        self.page.undo_delete(word, self.page.delete_word(word))
        self.page.edit_word(word, "of")
        self.page.save()
        self.assertIn('CONTENT="of" WC="1.00"', self.alto.read_text(encoding="utf-8"))

    def test_deleting_an_added_word_nets_out_and_redo_brings_it_back(self) -> None:
        word = self.page.add_word(_px_box(500, 105, 100, 55), "and")
        added_xml = etree.tostring(self.page.root)
        removal = self.page.undo_add(word)  # the GUI's delete of an added word
        self.assertEqual(self.page.session_edits, 0)
        self.page.redo_add(word, removal)
        self.assertEqual(etree.tostring(self.page.root), added_xml)
        self.assertEqual(self.page.session_edits, 1)
        self.assertIn(word, self.page.words)


class SettleTests(_PageFixture):
    """An undone session has nothing to save (settle): no "Save changes?"
    prompt, and no review provenance stamped on a page nobody changed."""

    def test_undone_delete_is_clean(self) -> None:
        word = self.page.words[1]
        self.page.undo_delete(word, self.page.delete_word(word))
        self.assertTrue(self.page.dirty)
        self.assertFalse(self.page.settle())

    def test_undone_edit_and_move_are_clean(self) -> None:
        word = self.page.words[1]  # "cf" @ 0.40
        self.page.edit_word(word, "of")
        self.page.revert_word(word, "cf", 0.40)
        lone = self.page.words[3]
        self.page.undo_move_words(self.page.move_words([lone], None))
        self.assertFalse(self.page.settle())

    def test_partial_undo_stays_dirty(self) -> None:
        first, second = self.page.words[0], self.page.words[1]
        self.page.edit_word(first, "Boarded")
        removal = self.page.delete_word(second)
        self.page.undo_delete(second, removal)
        self.assertTrue(self.page.settle())  # the edit is still there

    def test_exact_undo_add_is_clean(self) -> None:
        # Inside the line's box: the rebuilt SP gets its old ID and geometry
        # back, so removing the word again restores the page byte for byte.
        word = self.page.add_word(_px_box(500, 105, 100, 55), "and")
        self.page.undo_add(word)
        self.assertFalse(self.page.settle())

    def test_inexact_undo_stays_dirty(self) -> None:
        # Past the line's right edge the add grows the line and block boxes,
        # and undo_add does not shrink them back: a real change remains.
        word = self.page.add_word(_px_box(1000, 105, 100, 55), "and")
        self.page.undo_add(word)
        self.assertTrue(self.page.settle())

    def test_clean_state_follows_a_save(self) -> None:
        word = self.page.words[1]
        self.page.edit_word(word, "of")
        self.page.save()
        self.page.edit_word(word, "off")
        self.page.revert_word(word, "of", 1.0)
        self.assertFalse(self.page.settle())  # back to the *saved* page


class ContentGuardTests(_PageFixture):
    """Text a corrector types (or pastes) must be storable in ALTO."""

    def test_control_character_edit_is_refused_untouched(self) -> None:
        word = self.page.words[1]
        with self.assertRaisesRegex(ValueError, "U\\+0001"):
            self.page.edit_word(word, "o\x01f")
        self.assertEqual(word.content, "cf")
        self.assertEqual(word.elem.get("WC"), "0.40")
        self.assertFalse(self.page.dirty)
        self.assertEqual(self.page.session_edits, 0)

    def test_control_character_add_is_refused_untouched(self) -> None:
        before = etree.tostring(self.page.root)
        with self.assertRaises(ValueError):
            self.page.add_word(_px_box(500, 105, 100, 55), "an\x07d")
        self.assertEqual(etree.tostring(self.page.root), before)
        self.assertFalse(self.page.dirty)

    def test_whitespace_controls_are_collapsed_not_refused(self) -> None:
        # VT/FF/the separators/NEL are whitespace to str.split(): collapsed,
        # never stored.
        word = self.page.words[1]
        self.page.edit_word(word, "o\x0bf\x1f\x85")
        self.assertEqual(word.content, "o f")

    def test_content_problem(self) -> None:
        from src.alto_ocr.alto_format import content_problem
        for ok in ("Board", "½°μΩ", "naïve—“quoted”", "&<>\"'"):
            self.assertIsNone(content_problem(ok), ok)
        for bad in ("\x00", "a\x08", "\x7f", "\x9f", "￾", "\ud800"):
            self.assertIsNotNone(content_problem(bad), repr(bad))


class VerifyTests(_PageFixture):
    """Confirming a flagged word the OCR already got right (Enter on an
    unchanged word in the GUI)."""

    def test_confirm_stamps_wc_without_touching_content(self) -> None:
        word = self.page.words[1]  # "cf" @ 0.40
        self.assertTrue(self.page.verify_word(word))
        self.assertEqual(word.content, "cf")
        self.assertEqual(word.elem.get("WC"), "1.00")
        self.assertTrue(self.page.dirty)
        keys = list(word.elem.attrib)
        self.assertLess(keys.index("CONTENT"), keys.index("WC"))

    def test_confirm_counts_apart_from_corrections(self) -> None:
        self.page.verify_word(self.page.words[1])
        self.page.verify_word(self.page.words[2])  # unscored word, no WC
        self.assertEqual(self.page.session_verifies, 2)
        self.assertEqual(self.page.session_edits, 0)

    def test_confirmed_word_leaves_the_review_queue(self) -> None:
        word = self.page.words[1]
        self.page.verify_word(word)
        self.assertNotIn(word, self.page.flagged(0.50))

    def test_already_verified_word_records_nothing(self) -> None:
        word = self.page.words[1]
        self.page.verify_word(word)
        self.assertFalse(self.page.verify_word(word))
        self.assertEqual(self.page.session_verifies, 1)

    def test_unverify_restores_engine_confidence(self) -> None:
        scored, unscored = self.page.words[1], self.page.words[2]
        self.page.verify_word(scored)
        self.page.verify_word(unscored)
        self.page.unverify_word(scored, 0.40)
        self.page.unverify_word(unscored, None)
        self.assertEqual(scored.elem.get("WC"), "0.40")
        self.assertIsNone(unscored.elem.get("WC"))
        self.assertEqual(self.page.session_verifies, 0)

    def test_save_records_confirmations_in_their_own_clause(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.verify_word(self.page.words[3])  # "Lone" @ 0.85, correct
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; 1 word(s) confirmed",
                      body)
        # Confirmed words count as 1.00 in the page score, like edits:
        # mean(0.96, 1.00, —, 1.00) over scored words = 0.9866… → 0.99.
        self.assertIn('PC="0.99"', body)

    def test_confirmations_accumulate_across_sessions(self) -> None:
        self.page.verify_word(self.page.words[1])
        self.page.save()
        reopened = ReviewPage(self.alto, _IMG_SIZE)
        reopened.verify_word(reopened.words[2])
        reopened.edit_word(reopened.words[0], "Boards")
        reopened.move_words([reopened.words[3]], None)
        reopened.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; 2 word(s) confirmed; "
                      "1 reading-order change(s)", body)
        self.assertEqual(body.count('<Processing ID="review">'), 1)

    def test_confirm_only_page_is_protected_from_re_ocr(self) -> None:
        # Confirming is human work too — a re-run must not overwrite it.
        self.page.verify_word(self.page.words[1])
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 0 word(s) corrected; 1 word(s) confirmed",
                      body)
        self.assertTrue(has_review_provenance(self.alto))

    def test_no_confirmations_leaves_the_clause_out(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected", body)
        self.assertNotIn("confirmed", body)


class NetCountTests(_PageFixture):
    """Provenance counts each word's net effect, not each action
    (2026-10-15): the archive's record of how many words a human changed
    must not grow because a corrector changed their mind."""

    def _saved_step(self) -> str:
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        start = body.index("human review:")
        return body[start:body.index("<", start)]

    def test_word_edited_twice_counts_once(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.edit_word(cf, "or")
        self.assertEqual(self.page.session_edits, 1)
        self.assertEqual(self._saved_step(), "human review: 1 word(s) corrected")

    def test_edited_then_deleted_counts_once(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.delete_word(cf)
        self.assertEqual(self.page.session_edits, 1)

    def test_edit_back_to_the_original_text_is_a_confirmation(self) -> None:
        # The words are unchanged, but a human vouched for them: WC 1.00.
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.edit_word(cf, "cf")
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (0, 1))

    def test_verified_then_edited_is_one_correction(self) -> None:
        cf = self.page.words[1]
        self.page.verify_word(cf)
        self.page.edit_word(cf, "of")
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (1, 0))

    def test_verified_then_deleted_is_one_correction(self) -> None:
        cf = self.page.words[1]
        self.page.verify_word(cf)
        self.page.delete_word(cf)
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (1, 0))

    def test_added_then_edited_counts_once(self) -> None:
        word = self.page.add_word(_px_box(500, 105, 100, 55), "and")
        self.page.edit_word(word, "an")
        self.assertEqual(self.page.session_edits, 1)

    def test_added_then_deleted_counts_nothing(self) -> None:
        # Deleted the ordinary way rather than through undo_add.
        word = self.page.add_word(_px_box(500, 105, 100, 55), "and")
        self.page.delete_word(word)
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (0, 0))

    def test_multi_word_delete_counts_each_word_once(self) -> None:
        board, cf = self.page.words[0], self.page.words[1]
        self.page.edit_word(cf, "of")
        for word in (board, cf):
            self.page.delete_word(word)
        self.assertEqual(self.page.session_edits, 2)

    def test_clear_page_after_edits_counts_each_word_once(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.verify_word(self.page.words[3])
        self.assertEqual(self.page.clear_words(), 4)
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (4, 0))

    def test_clear_page_nets_out_words_added_this_session(self) -> None:
        self.page.add_word(_px_box(500, 105, 100, 55), "and")
        self.assertEqual(self.page.clear_words(), 5)
        self.assertEqual(self.page.session_edits, 4)  # the 4 that were on the file

    def test_undo_mixed_with_redo_nets_out(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.revert_word(cf, "cf", 0.40)
        removal = self.page.delete_word(cf)
        self.page.undo_delete(cf, removal)
        self.assertEqual((self.page.session_edits, self.page.session_verifies), (0, 0))

    def test_engine_wc_spelled_differently_survives_an_undo(self) -> None:
        # "0.4" in the file, rewritten as "0.40" by unverify: not a change.
        cf = self.page.words[1]
        cf.elem.set("WC", "0.4")
        self.page.verify_word(cf)
        self.page.unverify_word(cf, 0.4)
        self.assertEqual(self.page.session_verifies, 0)

    def test_undo_past_a_save_counts_against_the_saved_file(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.save()
        self.page.revert_word(cf, "cf", 0.40)  # the GUI's undo stack outlives a save
        self.assertEqual(self.page.session_edits, 1)
        self.assertEqual(self._saved_step(), "human review: 2 word(s) corrected")

    def test_save_starts_a_fresh_baseline(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        self.page.save()
        self.assertEqual(self.page.session_edits, 0)
        self.page.edit_word(cf, "or")
        self.assertEqual(self._saved_step(), "human review: 2 word(s) corrected")

    def test_reading_order_moves_still_count_per_move(self) -> None:
        lone = self.page.words[3]
        self.page.move_words([lone], None)
        self.page.move_words([lone], self.page.words[-1])
        self.page.move_words([lone], None)
        self.assertEqual(self.page.session_moves, 3)
        self.assertEqual(self.page.session_edits, 0)


class StepFromTests(_PageFixture):
    """Next/previous-word stepping (the GUI's Enter / n / p). Threshold 0.99
    flags all four fixture words, so a corrected word in the *middle* of the
    page is distinguishable from the page's first flagged word."""

    def _flagged(self) -> list:
        return self.page.flagged(0.99)

    def test_step_after_edit_goes_to_the_next_word_not_the_page_top(self) -> None:
        # The reported bug: correcting a word stamps WC=1.00, dropping it out
        # of the flagged list — stepping used to restart at the page's first
        # flagged word ("Board") instead of continuing down the page.
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        flagged = self._flagged()
        self.assertNotIn(cf, flagged)
        nxt = self.page.step_from(cf, flagged, +1)
        self.assertEqual(nxt.content, "1927")

    def test_step_backward_from_a_corrected_word(self) -> None:
        cf = self.page.words[1]
        self.page.edit_word(cf, "of")
        prev = self.page.step_from(cf, self._flagged(), -1)
        self.assertEqual(prev.content, "Board")

    def test_step_walks_the_page_front_to_back(self) -> None:
        # Correct every word in turn: each step must land on the next word
        # down the page, then wrap once at the end.
        seen = []
        word = self.page.words[0]
        for _ in range(3):
            self.page.edit_word(word, word.content.upper())
            word = self.page.step_from(word, self._flagged(), +1)
            seen.append(word.content)
        self.assertEqual(seen, ["cf", "1927", "Lone"])

    def test_wrap_at_the_page_ends(self) -> None:
        flagged = self._flagged()
        self.assertEqual(
            self.page.step_from(self.page.words[-1], flagged, +1).content, "Board")
        self.assertEqual(
            self.page.step_from(self.page.words[0], flagged, -1).content, "Lone")

    def test_sole_remaining_word_has_nowhere_to_go(self) -> None:
        word = self.page.words[1]
        self.assertIsNone(self.page.step_from(word, [word], +1))
        self.assertIsNone(self.page.step_from(word, [], +1))

    def test_candidate_order_does_not_matter(self) -> None:
        # The GUI filters the flagged list; stepping must rely on document
        # order, not on the order it happens to be handed.
        shuffled = list(reversed(self._flagged()))
        nxt = self.page.step_from(self.page.words[1], shuffled, +1)
        self.assertEqual(nxt.content, "1927")

    def test_deleted_word_anchors_at_the_page_start(self) -> None:
        word = self.page.words[1]
        self.page.delete_word(word)
        self.assertIsNone(self.page.doc_index(word))
        self.assertEqual(self.page.step_from(word, self._flagged(), +1).content,
                         "Board")

    def test_doc_index_follows_reading_order(self) -> None:
        self.assertEqual([self.page.doc_index(w) for w in self.page.words],
                         [0, 1, 2, 3])


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


def _string_geometry(root: etree._Element) -> list[tuple]:
    """(ID, HPOS, VPOS, WIDTH, HEIGHT, CONTENT, WC) of every String — a
    reading-order move must leave every word's box and text untouched."""
    return sorted(
        (el.get("ID"), el.get("HPOS"), el.get("VPOS"), el.get("WIDTH"),
         el.get("HEIGHT"), el.get("CONTENT"), el.get("WC"))
        for el in root.iter("{*}String")
    )


def _box(el: etree._Element) -> tuple[str, str, str, str]:
    return (el.get("HPOS"), el.get("VPOS"), el.get("WIDTH"), el.get("HEIGHT"))


class MoveWordsTests(_PageFixture):
    """Word-level reading order (move_words / nudge_words). The fixture is
    TB1[TL1: Board cf | TL2: 1927]  TB2[TL3: Lone]."""

    def _w(self, content: str):
        return next(w for w in self.page.words if w.content == content)

    def _text(self) -> str:
        return alto_to_text(self.page.root)

    def test_single_word_goes_in_line_after_target(self) -> None:
        strings = _string_geometry(self.page.root)
        move = self.page.move_words([self._w("Lone")], self._w("Board"))
        self.assertIsNotNone(move)
        self.assertEqual(self._text(), "Board Lone cf\n1927\n")
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "Lone", "cf", "1927"])
        # Emptied TextLine and TextBlock cascade away; PrintSpace stays.
        self.assertIsNone(self.page.root.find(".//{*}TextBlock[@ID='P1_TB00002']"))
        self.assertIsNotNone(self.page.root.find(".//{*}PrintSpace"))
        # The line that gained a word is re-fitted around all three; the
        # line whose words did not change keeps its box; the block covers both.
        tl1 = self.page.root.find(".//{*}TextLine[@ID='P1_TL00001']")
        self.assertEqual(_box(tl1), ("100", "100", "370", "460"))
        tl2 = self.page.root.find(".//{*}TextLine[@ID='P1_TL00002']")
        self.assertEqual(_box(tl2), ("100", "200", "400", "80"))
        tb1 = self.page.root.find(".//{*}TextBlock[@ID='P1_TB00001']")
        self.assertEqual(_box(tb1), ("100", "100", "400", "460"))
        self.assertEqual(len(tl1.findall("{*}SP")), 2)
        self.assertEqual(_string_geometry(self.page.root), strings)
        self.assertTrue(self.page.dirty)
        self.assertEqual(self.page.session_moves, 1)
        self.assertEqual(self.page.session_edits, 0)

    def test_page_start_reorders_within_a_line_without_refitting(self) -> None:
        tl1 = self.page.root.find(".//{*}TextLine[@ID='P1_TL00001']")
        box = _box(tl1)
        self.assertIsNotNone(self.page.move_words([self._w("cf")], None))
        self.assertEqual(self._text(), "cf Board\n1927\n\nLone\n")
        self.assertEqual(_box(tl1), box)  # same words, only their order changed
        sp = tl1.find("{*}SP")
        self.assertEqual(sp.getprevious().get("CONTENT"), "cf")

    def test_multi_line_run_keeps_its_line_breaks(self) -> None:
        self.page.move_words([self._w("cf"), self._w("1927")], self._w("Lone"))
        self.assertEqual(self._text(), "Board\n\nLone\ncf\n1927\n")
        tb2 = self.page.root.find(".//{*}TextBlock[@ID='P1_TB00002']")
        ids = [tl.get("ID") for tl in tb2.findall("{*}TextLine")]
        # "1927" was a whole line: it moved as that line, ID and all; "cf"
        # was half a line and gets a fresh one.
        self.assertEqual(ids, ["P1_TL00003", "P1_TL00004", "P1_TL00002"])
        tl1 = self.page.root.find(".//{*}TextLine[@ID='P1_TL00001']")
        self.assertEqual(_box(tl1), ("100", "100", "200", "60"))  # Board alone
        self.assertEqual(tl1.findall("{*}SP"), [])
        tb1 = self.page.root.find(".//{*}TextBlock[@ID='P1_TB00001']")
        self.assertEqual(_box(tb1), ("100", "100", "200", "60"))

    def test_multi_line_run_mid_line_splits_the_target_line(self) -> None:
        self.page.move_words([self._w("1927"), self._w("Lone")], self._w("Board"))
        self.assertEqual(self._text(), "Board\n1927\nLone\ncf\n")
        tb1 = self.page.root.find(".//{*}TextBlock[@ID='P1_TB00001']")
        self.assertEqual([tl.get("ID") for tl in tb1.findall("{*}TextLine")],
                         ["P1_TL00001", "P1_TL00002", "P1_TL00003", "P1_TL00004"])
        self.assertIsNone(self.page.root.find(".//{*}TextBlock[@ID='P1_TB00002']"))

    def test_non_contiguous_run_moves_as_lines(self) -> None:
        # A column: words that are not next to each other in reading order
        # because Tesseract's lines run across both columns.
        self.page.move_words([self._w("cf"), self._w("Lone")], self._w("1927"))
        self.assertEqual(self._text(), "Board\n1927\ncf\nLone\n")
        tb1 = self.page.root.find(".//{*}TextBlock[@ID='P1_TB00001']")
        self.assertEqual([tl.get("ID") for tl in tb1.findall("{*}TextLine")],
                         ["P1_TL00001", "P1_TL00002", "P1_TL00004", "P1_TL00003"])

    def test_nudge_steps_one_word(self) -> None:
        self.assertIsNotNone(self.page.nudge_words([self._w("1927")], -1))
        self.assertEqual(self._text(), "Board 1927 cf\n\nLone\n")
        self.assertIsNotNone(self.page.nudge_words([self._w("1927")], +1))
        self.assertEqual(self._text(), "Board cf 1927\n\nLone\n")

    def test_nudge_to_the_page_start(self) -> None:
        self.assertIsNotNone(self.page.nudge_words([self._w("cf")], -1))
        self.assertEqual(self._text(), "cf Board\n1927\n\nLone\n")

    def test_nudge_at_page_edges_is_noop(self) -> None:
        before = etree.tostring(self.page.root)
        self.assertIsNone(self.page.nudge_words([self._w("Board")], -1))
        self.assertIsNone(self.page.nudge_words([self._w("Lone")], +1))
        self.assertIsNone(self.page.nudge_words([self._w("cf")], 0))
        self.assertEqual(etree.tostring(self.page.root), before)
        self.assertFalse(self.page.dirty)

    def test_already_in_place_is_noop(self) -> None:
        before = etree.tostring(self.page.root)
        self.assertIsNone(self.page.move_words([self._w("cf")], self._w("Board")))
        self.assertIsNone(self.page.move_words([self._w("Board")], None))
        self.assertIsNone(self.page.move_words([], self._w("Board")))
        self.assertEqual(etree.tostring(self.page.root), before)
        self.assertFalse(self.page.dirty)
        self.assertEqual(self.page.session_moves, 0)

    def test_target_inside_the_run_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.page.move_words([self._w("Board"), self._w("cf")], self._w("cf"))

    def test_undo_is_byte_exact_in_reverse_order(self) -> None:
        original = etree.tostring(self.page.root)
        moves = [
            self.page.move_words([self._w("Lone")], self._w("Board")),
            self.page.move_words([self._w("cf"), self._w("1927")], None),
            self.page.nudge_words([self._w("Board")], +1),
        ]
        self.assertEqual(self.page.session_moves, 3)
        for move in reversed(moves):
            self.page.undo_move_words(move)
        self.assertEqual(etree.tostring(self.page.root), original)
        self.assertEqual([w.content for w in self.page.words],
                         ["Board", "cf", "1927", "Lone"])
        self.assertEqual(self.page.session_moves, 0)
        self.assertFalse(self.page.settle())

    def test_words_stay_usable_across_move_and_undo(self) -> None:
        cf = self._w("cf")
        move = self.page.move_words([cf], self._w("Lone"))
        self.page.edit_word(cf, "of")
        self.assertEqual(self._text(), "Board\n1927\n\nLone of\n")
        self.page.revert_word(cf, "cf", 0.40)
        self.page.undo_move_words(move)
        self.assertEqual(self._text(), "Board cf\n1927\n\nLone\n")

    def test_save_records_moves_and_writes_new_order(self) -> None:
        self.page.edit_word(self._w("cf"), "of")
        self.page.move_words([self._w("Lone")], None)
        txt_path = self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; "
                      "1 reading-order change(s)", body)
        self.assertEqual(txt_path.read_text(encoding="utf-8"),
                         "Lone Board of\n1927\n")
        # A later session accumulates the count — the "(\d+) word" parse
        # survives the appended clause.
        reopened = ReviewPage(self.alto, _IMG_SIZE)
        reopened.nudge_words([reopened.words[0]], +1)
        reopened.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected; "
                      "2 reading-order change(s)", body)
        self.assertEqual(body.count('<Processing ID="review">'), 1)

    def test_moves_only_session_gets_provenance(self) -> None:
        self.page.move_words([self._w("Lone")], None)
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 0 word(s) corrected; "
                      "1 reading-order change(s)", body)
        # A reordered page is protected from re-OCR like any reviewed page.
        self.assertTrue(has_review_provenance(self.alto))
        # PC is order-independent: mean(0.96, 0.40, 0.85) -> 0.74 as loaded.
        self.assertIn('PC="0.74"', body)

    def test_no_moves_keeps_legacy_wording(self) -> None:
        self.page.edit_word(self.page.words[1], "of")
        self.page.save()
        body = self.alto.read_text(encoding="utf-8")
        self.assertIn("human review: 1 word(s) corrected", body)
        self.assertNotIn("reading-order", body)


class MoveComposedBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        obj = Path(self._tmp.name) / "RefCB"
        (obj / "ALTO").mkdir(parents=True)
        self.alto = obj / "ALTO" / "page001.xml"
        self.alto.write_text(_ALTO_CB, encoding="utf-8")
        self.page = ReviewPage(self.alto, _IMG_SIZE)

    def test_emptied_block_inside_composedblock_cascades_and_refits(self) -> None:
        original = etree.tostring(self.page.root)
        beta = next(w for w in self.page.words if w.content == "Beta")
        gamma = next(w for w in self.page.words if w.content == "Gamma")
        move = self.page.move_words([beta], gamma)
        self.assertEqual(alto_to_text(self.page.root), "Alpha\n\nGamma Beta\n")
        cb = self.page.root.find(".//{*}ComposedBlock")
        self.assertEqual([tb.get("ID") for tb in cb.findall("{*}TextBlock")],
                         ["P1_TB00001"])
        self.assertEqual(_box(cb), ("100", "100", "800", "100"))
        self.page.undo_move_words(move)
        self.assertEqual(etree.tostring(self.page.root), original)


class TranscriptSpansTests(unittest.TestCase):
    def test_spans_index_the_rendered_text(self) -> None:
        root = etree.fromstring(_ALTO.encode())
        text, spans = transcript_spans(root)
        self.assertEqual(text, alto_to_text(root))
        self.assertEqual([(s.get("CONTENT"), text[a:b]) for s, a, b in spans],
                         [("Board", "Board"), ("cf", "cf"),
                          ("1927", "1927"), ("Lone", "Lone")])


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

    def test_tif_beats_its_jpeg_export_in_either_walk_order(self) -> None:
        # Capture One writes sibling JPEG/ + TIFF/ exports; os.walk order
        # decides which folder is seen first, so both orders must give the TIF.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for sub in ("JPEG", "TIFF"):
                (root / sub).mkdir()
            (root / "JPEG" / "p1.jpg").write_bytes(b"x")
            (root / "JPEG" / "p2.jpg").write_bytes(b"x")
            (root / "JPEG" / "p3.jpg").write_bytes(b"x")
            (root / "TIFF" / "p1.tif").write_bytes(b"x")
            (root / "TIFF" / "p2.tif").write_bytes(b"x")
            with self.assertNoLogs("src.alto_ocr.remediate", "WARNING"):
                idx = index_source_images(root)
        self.assertEqual({k: v.name for k, v in idx.items()},
                         {"p1": "p1.tif", "p2": "p2.tif", "p3": "p3.jpg"})


if __name__ == "__main__":
    unittest.main()
