"""Regression tests for glm-ocr post-OCR text cleanup (commentary strip + loop collapse).

These lock in the fixes for a code-review pass that found the cleanup passes were
silently deleting REAL transcript content (the passes mutate an *accepted*
transcript, so a false positive loses content unrecoverably across all
collections). Each test names the finding it guards.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_glmocr_text_cleanup
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.glmocr import (  # noqa: E402
    _CHAT_COMMENTARY_RE,
    _collapse_repetition_loops,
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


if __name__ == "__main__":
    unittest.main()
