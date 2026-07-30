"""Tests for ``markup.normalize_markup`` — glm-ocr's rich-text flattener.

The transforms are structural, not character-gated: they act only on real
markup regions (well-formed HTML tags, genuine math spans, pipe-table rows)
and must be a no-op on ordinary archival prose — currency, inequalities,
placeholders, braces, stray backslashes. That contract had regressed once
before (a lone ``$`` used to route whole blocks through the LaTeX pipeline)
and had no tests; locked in here, including the 2026-07-30 fix that stops
DOTALL from pairing two unrelated ``$`` signs across *different lines* into
one "math span" and eating them.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_markup
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.markup import normalize_markup  # noqa: E402


class HtmlTableTests(unittest.TestCase):
    def test_table_flattens_to_rows_of_cells(self) -> None:
        html = ("<table><thead><tr><th>Name</th><th>Year</th></tr></thead>"
                "<tbody><tr><td>Zeff</td><td>1927</td></tr></tbody></table>")
        self.assertEqual(normalize_markup(html), "Name Year\nZeff 1927")

    def test_stray_inline_tags_removed_without_gluing_words(self) -> None:
        self.assertEqual(normalize_markup("alpha<br>beta and <b>bold</b>"),
                         "alpha beta and bold")

    def test_angle_bracket_prose_survives(self) -> None:
        for text in ("a < b holds", "voltage <45 deg> range", "the <placeholder> here"):
            self.assertEqual(normalize_markup(text), text)


class MarkdownTableTests(unittest.TestCase):
    def test_divider_row_dropped_data_rows_joined(self) -> None:
        md = "| Name | Year |\n| :--- | ---: |\n| Zeff | 1927 |"
        self.assertEqual(normalize_markup(md), "Name Year\nZeff 1927")

    def test_single_dash_cells_are_data_not_divider(self) -> None:
        md = "| a | b |\n| - | - |"
        self.assertEqual(normalize_markup(md), "a b\n- -")


class LatexTests(unittest.TestCase):
    def test_frac_and_symbols_flatten(self) -> None:
        self.assertEqual(normalize_markup(r"rate $\frac{a}{b}$ per day"),
                         "rate a/b per day")
        self.assertEqual(normalize_markup(r"angle $\alpha$ rises"), "angle α rises")

    def test_inline_math_without_backslash_still_flattens(self) -> None:
        self.assertEqual(normalize_markup("$x^2$ equals four"), "x^2 equals four")

    def test_currency_range_preserved(self) -> None:
        text = "salaries of $2,000 to $4,500 per annum"
        self.assertEqual(normalize_markup(text), text)

    def test_lone_currency_preserved(self) -> None:
        text = "a deposit of $1,250 was made"
        self.assertEqual(normalize_markup(text), text)

    def test_dollars_on_different_lines_not_paired(self) -> None:
        # Regression (2026-07-30): DOTALL used to let these pair into one
        # "math span" and both $ signs were silently eaten.
        text = "the fee was $5 flat\nsee the note about the $ sign"
        self.assertEqual(normalize_markup(text), text)

    def test_stray_backslash_and_braces_survive(self) -> None:
        for text in (r"path \network\share noted", "the set {1, 2} applies"):
            self.assertEqual(normalize_markup(text), text)


class FenceAndWhitespaceTests(unittest.TestCase):
    def test_code_fence_lines_dropped_content_kept(self) -> None:
        self.assertEqual(normalize_markup("```text\nreal line\n```"), "real line")

    def test_prose_is_a_noop_beyond_whitespace(self) -> None:
        self.assertEqual(normalize_markup("Board  of   Trustees"), "Board of Trustees")


if __name__ == "__main__":
    unittest.main()
