"""The symbol keyboard's ALTO-XML contract (``symbols.py``).

Every key a corrector can click must land in an ALTO ``String/@CONTENT``
exactly as shown, and mean the same thing to every tool downstream: one
code point, drawable by Tk 8.6, a character XML can carry, not whitespace
(ALTO separates words with ``<SP>``), not invisible or combining, stable
under NFC normalization, and never a discouraged look-alike of another key.
These tests lock the layout to that contract, prove the rules actually
reject what they claim to, and push every key through a real
edit → save → re-parse round trip.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_symbols
"""

from __future__ import annotations

import json
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.remediate import ReviewPage  # noqa: E402
from src.alto_ocr.symbols import (  # noqa: E402
    DISCOURAGED,
    SYMBOL_KEYBOARD,
    SymbolCategory,
    keyboard_payload,
    keyboard_problems,
    symbol_label,
    symbol_problem,
)

_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="0.40">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="80">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="x" WC="0.40"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

# What the review tab's palette offered before the keyboard existed.
_LEGACY_PALETTE = ("π", "Σ", "Δ", "θ", "Ω", "μ", "√", "∫", "∞", "±", "×", "÷",
                   "≈", "≠", "≤", "≥", "°", "·", "²", "³", "½", "¼", "¾", "—")


def _all_keys() -> list[str]:
    keys: list[str] = []
    for category in SYMBOL_KEYBOARD:
        keys.extend(ch for ch in category.symbols if ch not in keys)
    return keys


class LayoutContractTests(unittest.TestCase):
    def test_every_key_satisfies_the_contract(self) -> None:
        self.assertEqual(keyboard_problems(), [])

    def test_common_tab_is_the_palette_correctors_already_know(self) -> None:
        self.assertEqual(SYMBOL_KEYBOARD[0].name, "Common")
        self.assertEqual(SYMBOL_KEYBOARD[0].symbols, _LEGACY_PALETTE)

    def test_keys_are_single_canonical_bmp_characters(self) -> None:
        # Spelled out beside keyboard_problems() so a regression in the
        # rule function itself cannot quietly wave a bad key through.
        for ch in _all_keys():
            with self.subTest(key=symbol_label(ch)):
                self.assertEqual(len(ch), 1)
                self.assertLessEqual(ord(ch), 0xFFFF)
                self.assertFalse(ch.isspace())
                self.assertEqual(unicodedata.normalize("NFC", ch), ch)
                self.assertNotIn(unicodedata.category(ch)[0], "CMZ")
                self.assertNotIn(ch, DISCOURAGED)

    def test_rows_stay_keyboard_sized(self) -> None:
        for category in SYMBOL_KEYBOARD:
            with self.subTest(category=category.name):
                self.assertLessEqual(len(category.rows), 4)
                self.assertTrue(all(0 < len(row) <= 25 for row in category.rows))


class RuleTests(unittest.TestCase):
    """Each rule rejects what its docstring says it does."""

    def test_rejections(self) -> None:
        cases = {
            "µ": "compatibility",          # MICRO SIGN beside μ
            "Ω": "NFC",                    # OHM SIGN normalizes to Ω
            "Å": "NFC",                    # ANGSTROM SIGN normalizes to Å
            "−": "compatibility",          # MINUS SIGN beside the hyphen
            " ": "whitespace",             # NO-BREAK SPACE
            " ": "whitespace",
            "­": "invisible",              # SOFT HYPHEN (Cf)
            "‍": "invisible",              # ZERO WIDTH JOINER
            "": "private-use",            # private use area
            "́": "combining",              # COMBINING ACUTE ACCENT
            "\U0001d465": "Basic Multilingual",  # MATHEMATICAL ITALIC SMALL X
            "\x07": "control",
            "a": "ASCII",
            "ab": "code points",
        }
        for ch, reason in cases.items():
            with self.subTest(key=repr(ch)):
                problem = symbol_problem(ch)
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)

    def test_layout_level_rules(self) -> None:
        bad = (SymbolCategory("Twice", (("π", "π"),)),
               SymbolCategory("Twice", (("µ",),)),
               SymbolCategory("Empty", ()))
        problems = "\n".join(keyboard_problems(bad))
        self.assertIn("'Twice' appears more than once", problems)
        self.assertIn("appears twice", problems)
        self.assertIn("MICRO SIGN".capitalize(), problems)
        self.assertIn("Empty: has no keys", problems)


class RoundTripTests(unittest.TestCase):
    """Every key, typed into a word and saved, comes back identical from
    the ALTO and appears unchanged in the regenerated transcript."""

    def test_every_key_survives_save_and_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = Path(td) / "RefA"
            (obj / "ALTO").mkdir(parents=True)
            (obj / "Text").mkdir()
            alto = obj / "ALTO" / "p1.xml"
            alto.write_text(_ALTO, encoding="utf-8")

            text = "".join(_all_keys())
            page = ReviewPage(alto, (2550, 3300))
            page.edit_word(page.words[0], text)
            txt_path = page.save()

            string = next(etree.parse(str(alto)).getroot().iter("{*}String"))
            self.assertEqual(string.get("CONTENT"), text)
            self.assertEqual(txt_path.read_text(encoding="utf-8"), text + "\n")
            # Stored as characters, not numeric references a human editor
            # would have to decode.
            self.assertIn(text, alto.read_text(encoding="utf-8"))


class PayloadTests(unittest.TestCase):
    def test_payload_mirrors_the_layout_and_is_json(self) -> None:
        payload = json.loads(json.dumps(keyboard_payload(), ensure_ascii=False))
        self.assertEqual([c["name"] for c in payload["categories"]],
                         [c.name for c in SYMBOL_KEYBOARD])
        for got, want in zip(payload["categories"], SYMBOL_KEYBOARD):
            self.assertEqual([tuple(r) for r in got["rows"]], list(want.rows))
        self.assertEqual(set(payload["labels"]), set(_all_keys()))

    def test_label_names_the_character(self) -> None:
        self.assertEqual(symbol_label("½"), "U+00BD Vulgar fraction one half")
        self.assertEqual(symbol_label("μ"), "U+03BC Greek small letter mu")


if __name__ == "__main__":
    unittest.main()
