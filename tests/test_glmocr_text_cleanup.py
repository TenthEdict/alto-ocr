"""Regression tests for glm-ocr post-OCR text cleanup (commentary strip + loop collapse).

These lock in the fixes for a code-review pass that found the cleanup passes were
silently deleting REAL transcript content (the passes mutate an *accepted*
transcript, so a false positive loses content unrecoverably across all
collections). Each test names the finding it guards.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_glmocr_text_cleanup
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.glmocr import (  # noqa: E402
    _CHAT_COMMENTARY_RE,
    _collapse_enumerated,
    _collapse_inline,
    _collapse_repetition_loops,
    _collapse_tandem,
    _strip_chat_commentary,
)


class ChatCommentaryRegexTests(unittest.TestCase):
    """_CHAT_COMMENTARY_RE must strip model self-talk WITHOUT eating real prose."""

    # Findings #1-#3: real archival prose that previously matched (and was
    # silently deleted) must now be preserved.
    REAL_PROSE_PRESERVED = [
        # #2: "I will provide/use ..." — ordinary first-person letter/minutes prose
        "I will provide a full accounting at the next meeting.",
        "I will use my best judgment in this matter.",
        # #3: document opener, not a model lead-in
        "Here is the text of the resolution adopted by the Board.",
        "Here is the transcription of the minutes of June 1927.",
        # #1: inscription / caption / bylaws prose
        "The text on the monument was barely legible.",
        "The text states that all members shall be entitled to vote.",
        "The text reads, in part, as quoted by the chairman.",
        # MSA00005 smoke test: a REAL caption that must survive — it looks just
        # like the "the bottom photo depicts…" leak, so we deliberately keep all
        # "the <position> photo …" lines rather than risk eating real captions.
        "The photo at the right shows the sequence of events during powered flight.",
        "The text is a binding contract between the parties.",
        # 33-Ref-ID batch: precision guards for the NEW self-summary arms — each
        # looks superficially like a leak but is real prose and must survive.
        "The document is about to be signed by all parties present.",        # "is about to" idiom, not "about a/an/the"
        "The text describes the southern boundary of the lot as follows.",   # "describes THE" (deed prose), not "a/an"
        "The text outlines the procedure the committee shall follow.",       # "outlines THE", not "a/an"
        "The text is a faithful copy of the original deed.",                 # "is a <noun>" without "document that"
    ]

    # Genuine glm-ocr commentary that must STILL be stripped (no regression in
    # the feature's actual job).
    MODEL_COMMENTARY_STRIPPED = [
        "The image shows a title page.",
        "The text in the image reads: Dear Sir",
        "The text provided does not contain any readable text.",
        "The text reads: Dear Sir,",
        "The text reads as follows:",
        "The text states: All members in good standing",
        "The text on the image is faint.",
        "I'll just output the text.",
        "I will transcribe the page now.",
        "Here is the text:",
        "Here is the transcription:",
        "Here is the text of the image:",
        "I'm unable to read this page.",
        "Wait, let me re-read the page.",
        "Let me re-examine the image.",
        # MSA00005 smoke test: two new leak phrasings the stripper now catches.
        "The text is centered on a white background with black lettering.",
        'Wait, the word "BASE OPERATIONS" is at the bottom of the second photo.',
        # 33-Ref-ID batch (MSA00005 NASA technical manuals): the model transcribes
        # the page correctly, then appends a third-person self-summary (often
        # looped 3-4x). These are the new phrasing families the stripper catches.
        "The text appears to be a page from a technical document or manual, likely related to spacecraft operations.",
        "The text is partially cut off at the bottom. It seems to continue with the heading.",
        "The text is cut off at the bottom. It seems to continue.",
        "The text is a technical document that outlines the procedures for retrofire operations.",
        "The text provides detailed information about an UHF voice receiver-transmitter system.",
        "The text describes an UHF voice receiver-transmitter system designed as a small unit.",
        "The text discusses a system of three antenna units serving different functions.",
        "The image provided is too blurry to accurately recognize any text or details.",
        "The image does not contain any textual content other than the title and the figure number.",
        "The document is about the results of a second United States manned orbital space flight.",
    ]

    def test_real_prose_is_not_matched(self):
        for line in self.REAL_PROSE_PRESERVED:
            with self.subTest(line=line):
                self.assertIsNone(
                    _CHAT_COMMENTARY_RE.match(line),
                    f"real prose wrongly matched (would be deleted): {line!r}",
                )

    def test_model_commentary_is_matched(self):
        for line in self.MODEL_COMMENTARY_STRIPPED:
            with self.subTest(line=line):
                self.assertIsNotNone(
                    _CHAT_COMMENTARY_RE.match(line),
                    f"model commentary not matched (would leak): {line!r}",
                )

    def test_strip_keeps_real_lines_around_commentary(self):
        page = ("Dear Sir,\n"
                "The image shows the letterhead.\n"
                "I remain your obedient servant.")
        self.assertEqual(
            _strip_chat_commentary(page),
            "Dear Sir,\nI remain your obedient servant.",
        )


class LoopCollapseTests(unittest.TestCase):
    """_collapse_repetition_loops must collapse only >= 3x repeats, never doubled content."""

    SUB_BLOCK = (
        "Notice of the annual stockholders meeting of the company.\n"
        "All are invited to attend in person.\n"
        "Refreshments will be served afterward."
    )

    # Finding #4 / #5 / #6: content that appears only ~2x (or a running head
    # bracketing one body line) must be left byte-identical.
    def test_two_line_block_doubled_is_preserved(self):
        blk = "John Q. Public, Esq.\n123 Main Street, Springfield, IL 62704"
        doc = blk + "\n" + blk
        self.assertEqual(_collapse_repetition_loops(doc), doc)

    def test_long_single_line_doubled_is_preserved(self):
        line = "Resolved that the treasurer shall render a full account."
        doc = line + "\n" + line
        self.assertEqual(_collapse_repetition_loops(doc), doc)

    def test_substantial_block_doubled_is_preserved(self):
        doc = self.SUB_BLOCK + "\n" + self.SUB_BLOCK
        self.assertEqual(_collapse_repetition_loops(doc), doc)

    def test_running_header_bracketing_one_body_line_is_preserved(self):
        # [X, body, X]: only X recurs (a stamp/running head top & bottom).
        aba = ("MINUTES OF THE BOARD OF TRUSTEES OF PURDUE UNIVERSITY\n"
               "The board convened at ten oclock and approved the minutes.\n"
               "MINUTES OF THE BOARD OF TRUSTEES OF PURDUE UNIVERSITY")
        self.assertEqual(_collapse_repetition_loops(aba), aba)

    def test_carbon_copy_second_real_copy_survives_strip_then_collapse(self):
        # #5: stripping the interleaved commentary line must NOT make the two
        # real halves collapse to one.
        page = ("Received of John Smith the sum of ten dollars.\n"
                "Signed, the Treasurer.\n"
                "The text reads: (continued)\n"
                "Received of John Smith the sum of ten dollars.\n"
                "Signed, the Treasurer.")
        final = _collapse_repetition_loops(_strip_chat_commentary(page))
        self.assertEqual(final.count("Received of John Smith"), 2)

    # Genuine generation loops must STILL collapse (feature still works).
    def test_single_line_loop_collapses(self):
        self.assertEqual(
            _collapse_repetition_loops("\n".join(["Transfer Van"] * 5)),
            "Transfer Van",
        )

    def test_substantial_block_tripled_collapses(self):
        self.assertEqual(
            _collapse_repetition_loops("\n".join([self.SUB_BLOCK] * 3)),
            self.SUB_BLOCK,
        )

    def test_truncated_triple_collapses(self):
        # 2 full copies + first line of a 3rd (token-cap cut).
        trunc = (self.SUB_BLOCK + "\n" + self.SUB_BLOCK + "\n"
                 + "Notice of the annual stockholders meeting of the company.")
        self.assertEqual(_collapse_repetition_loops(trunc), self.SUB_BLOCK)


class CollapseTandemTests(unittest.TestCase):
    """#14: the strip-once refactor must not change _collapse_tandem behavior."""

    def test_substantial_block_back_to_back_collapses_to_one(self):
        line = "Transfer Van Department Ledger"  # 30 chars -> "substantial"
        out = _collapse_tandem([line] * 5 + ["Grand total carried forward"])
        self.assertEqual(out, [line, "Grand total carried forward"])

    def test_short_token_rows_are_preserved(self):
        # short tokens repeated a modest number of times are NOT a loop signature
        lines = ["0 0 0"] * 4 + ["1 2 3"]
        self.assertEqual(_collapse_tandem(lines), lines)

    def test_blank_only_unit_does_not_anchor_a_run(self):
        lines = ["", "", "", "", "real line"]
        self.assertEqual(_collapse_tandem(lines), lines)


class InlineLoopCollapseTests(unittest.TestCase):
    """_collapse_inline: a loop confined to ONE line, which every line-based pass
    is blind to. Found on MSF00327 (glm-ocr:latest) — dense tabular pages and
    run-on table headers loop without ever emitting a newline.

    The preserve/collapse split here is not invented: each PRESERVE case is real
    content measured in the 4,844-page MSA00005 + MSF00327 corpus, and each
    COLLAPSE case is a real loop from the same corpus.
    """

    PHRASE = "Gas per Indicated Horse Power Supplied per Hour Absorbed by Jacket"

    # --- real content that must survive byte-identical --------------------
    def test_short_token_table_row_is_preserved(self):
        # A table row of identical cells is ordinary content, not a loop.
        line = "104 758.5 86 3.50 1.02 2.48 1.410 1.536 6 6 6 6 6"
        self.assertEqual(_collapse_inline(line), line)

    def test_legit_short_token_run_below_extreme_is_preserved(self):
        # "40.7" x7 and "x" x9 are the corpus maxima for legitimate short-token
        # runs; the extreme floor (40) sits far above them.
        for tok, reps in (("40.7", 7), ("x", 9), ("T", 19)):
            line = "col " + " ".join([tok] * reps) + " end"
            self.assertEqual(_collapse_inline(line), line, f"{tok} x{reps}")

    def test_substantial_phrase_tripled_is_preserved(self):
        # "Friendship Seven," x3 — the radio-comms page the module note cites as
        # the reason not to use repeat_penalty. 3x is real doubled speech.
        line = "Friendship Seven, " * 3 + "do you read"
        self.assertEqual(_collapse_inline(line), line)

    def test_healthy_line_keeps_original_spacing(self):
        # Non-collapsing lines are emitted verbatim, never re-joined.
        line = "The  quick   brown fox   jumped over the lazy dog and ran away"
        self.assertEqual(_collapse_inline(line), line)

    # --- genuine loops that must collapse ---------------------------------
    def test_extreme_single_token_run_collapses(self):
        # MSF00327 page 00012: "0" x553 inside one line.
        self.assertEqual(
            _collapse_inline("ENGINE " + " ".join(["0"] * 553) + " END"),
            "ENGINE 0 END",
        )

    def test_extreme_two_token_period_collapses(self):
        # MSF00327 page 00022: "1.67 67" x216 inside one line.
        self.assertEqual(
            _collapse_inline("Test No. " + " ".join(["1.67", "67"] * 216)),
            "Test No. 1.67 67",
        )

    def test_substantial_phrase_at_min_reps_collapses(self):
        # MSF00327 page 00019: a column header repeated x43 in one 1,475-word line.
        line = " ".join([self.PHRASE] * 6)
        self.assertEqual(_collapse_inline(line), self.PHRASE)

    def test_single_line_page_is_reached(self):
        """Regression: a text with NO newline used to return early from
        _collapse_repetition_loops untouched, so page 00022 — one 450-word line —
        was never even examined."""
        text = "Test No. " + " ".join(["1.67", "67"] * 216)
        self.assertNotIn("\n", text)
        self.assertEqual(_collapse_repetition_loops(text), "Test No. 1.67 67")

    def test_collapse_keeps_the_first_copy_in_place(self):
        # Order-preserving: surrounding tokens keep their positions.
        line = "alpha " + " ".join(["9"] * 60) + " omega"
        self.assertEqual(_collapse_inline(line), "alpha 9 omega")


class EnumeratedLoopCollapseTests(unittest.TestCase):
    """_collapse_enumerated: a loop the model NUMBERS as it goes, so no two lines
    are byte-identical and every exact-match pass misses it. MSF00327 page 00079
    emitted one invented sentence 71x this way (2,877 words on a ~460-word page).
    """

    BODY = ("**Skyline Capital Management** - A leading global investment firm "
            "specializing in asset management and corporate finance.")

    def _numbered(self, n, body=None, start=1):
        return [f"{i}. {body or self.BODY}" for i in range(start, start + n)]

    # --- genuine loops -----------------------------------------------------
    def test_numbered_loop_collapses_to_first_item(self):
        lines = _collapse_enumerated(self._numbered(71))
        self.assertEqual(lines, [f"1. {self.BODY}"])

    def test_blank_lines_between_items_are_dropped_with_the_run(self):
        # glm-ocr emits the items blank-separated; the run must still be seen,
        # and the blanks BETWEEN items go with it. The separator trailing the
        # final item is ordinary formatting and is left alone.
        spaced = []
        for ln in self._numbered(10):
            spaced += [ln, ""]
        self.assertEqual(_collapse_enumerated(spaced), [f"1. {self.BODY}", ""])

    def test_truncated_final_item_is_taken_with_the_run(self):
        # The token cap cuts the last item mid-way ("71. **Skyline"), leaving a
        # body that is a proper prefix of the looped one.
        lines = self._numbered(70) + ["71. **Skyline"]
        self.assertEqual(_collapse_enumerated(lines), [f"1. {self.BODY}"])

    def test_bullet_loop_collapses(self):
        lines = [f"- {self.BODY}"] * 5
        self.assertEqual(_collapse_enumerated(lines), [f"- {self.BODY}"])

    # --- real content that must survive ------------------------------------
    def test_run_below_min_reps_is_preserved(self):
        lines = self._numbered(3)
        self.assertEqual(_collapse_enumerated(lines), lines)

    def test_distinct_items_are_preserved(self):
        lines = [f"{i}. Observation number {i} was recorded by the engineer."
                 for i in range(1, 9)]
        self.assertEqual(_collapse_enumerated(lines), lines)

    def test_short_bodied_numbered_form_is_preserved(self):
        # A blank ruled form / index: short bodies, however many times repeated.
        lines = [f"{i}. Name" for i in range(1, 21)]
        self.assertEqual(_collapse_enumerated(lines), lines)

    def test_dot_leader_index_is_preserved(self):
        # Long enough, but almost no letters -> not looped prose.
        lines = [f"{i}. ......................................... 14"
                 for i in range(1, 21)]
        self.assertEqual(_collapse_enumerated(lines), lines)

    def test_unenumerated_lines_are_untouched(self):
        lines = [self.BODY] * 6  # no markers: this is _collapse_tandem's job
        self.assertEqual(_collapse_enumerated(lines), lines)

    def test_prose_starting_with_an_abbreviation_is_not_a_marker(self):
        # "Mr." must not read as a list marker.
        lines = ["Mr. Smith attended the meeting of the board of trustees."] * 6
        self.assertEqual(_collapse_enumerated(lines), lines)


class DependencyFreeImportTests(unittest.TestCase):
    """#11: the canary regex lives in _util so the QA script imports it without
    pulling in the engine's cv2/numpy. Verified in a CLEAN interpreter (the suite
    itself imports glmocr, so cv2 is already loaded in-process)."""

    def test_util_import_does_not_pull_cv2_or_numpy(self):
        repo_root = Path(__file__).resolve().parent.parent
        code = (
            "import sys, src.alto_ocr._util as u;"
            "assert u._CHAT_COMMENTARY_RE is not None;"
            "assert 'cv2' not in sys.modules, 'cv2 leaked';"
            "assert 'numpy' not in sys.modules, 'numpy leaked';"
            "print('ok')"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=repo_root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
