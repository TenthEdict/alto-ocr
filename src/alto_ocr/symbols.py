"""The review tools' symbol keyboard: characters correctors need but cannot
type on a US keyboard, laid out the way a keyboard is — rows of keys under
category tabs.

The collections decide what is on it: engineering notebooks and lab reports
(math, Greek, units, super/subscripts), pharmacy records (apothecary
weights), typescripts (dashes, fractions, typographic quotes), and
correspondence and publications in European languages (accented letters).
"Common" comes first and is the palette the review tab shipped with, so
correctors who learned it find it where it was.

Every key must be storable in an ALTO ``String/@CONTENT`` exactly as
inserted, and must mean the same thing to every tool downstream. That is
stricter than "valid XML" (``symbol_problem`` is the contract; the test
suite asserts ``keyboard_problems()`` is empty):

* **one code point per key**, and **not plain ASCII** (already on the keyboard);
* **Basic Multilingual Plane only**: Tcl/Tk 8.6, which draws the desktop
  app, cannot reliably handle characters beyond U+FFFF;
* **a character XML can carry**, and not a control
  (``alto_format.content_problem``, the gate every human edit passes);
* **not whitespace**: edits collapse whitespace, and ALTO marks the gaps
  between words with ``<SP>`` elements, not with characters;
* **no format characters or lone combining marks**: a soft hyphen or
  zero-width joiner is invisible in the editor, and a combining accent
  attaches to whatever happens to precede the cursor;
* **NFC-stable**: a character that Unicode normalization replaces (U+2126
  OHM SIGN becomes U+03A9 Ω) changes under any search index that
  normalizes, so a transcript must carry the canonical form;
* **no discouraged compatibility twins** of a character already on the
  keyboard (U+00B5 MICRO SIGN beside μ, N-ARY SUMMATION beside Σ, MINUS
  SIGN beside the hyphen). Two keys that look the same but store
  different characters split one word into two search terms.

Headless on purpose: the Tk review tab and the web review client both
render this one layout (the web client receives it through
``/api/config``), so the two UIs cannot drift apart.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .alto_format import content_problem


@dataclass(frozen=True)
class SymbolCategory:
    """One tab of the keyboard: a name and its rows of keys."""

    name: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(ch for row in self.rows for ch in row)


def _rows(*rows: str) -> tuple[tuple[str, ...], ...]:
    # Space-separated in the source so each key is legible there; keys can
    # never be whitespace themselves (see symbol_problem), so split() is safe.
    return tuple(tuple(row.split()) for row in rows)


SYMBOL_KEYBOARD: tuple[SymbolCategory, ...] = (
    SymbolCategory("Common", _rows(
        "π Σ Δ θ Ω μ √ ∫ ∞ ± × ÷ ≈ ≠ ≤ ≥ ° · ² ³ ½ ¼ ¾ —",
    )),
    SymbolCategory("Math", _rows(
        "± ∓ × ÷ √ ∛ ∫ ∬ ∮ ∂ ∇ ∞ ′ ″ ℓ ℏ",
        "≈ ≅ ≡ ≠ ≤ ≥ ≪ ≫ ∝ ∠ ⊥ ∥ △ ∴ ∵",
        "∈ ∉ ⊂ ⊃ ⊆ ⊇ ∪ ∩ ∅ ∀ ∃ ¬ ∧ ∨ ⊕ ⊗",
    )),
    SymbolCategory("Greek", _rows(
        "Α Β Γ Δ Ε Ζ Η Θ Ι Κ Λ Μ Ν Ξ Ο Π Ρ Σ Τ Υ Φ Χ Ψ Ω",
        "α β γ δ ε ζ η θ ι κ λ μ ν ξ ο π ρ σ ς τ υ φ χ ψ ω",
    )),
    SymbolCategory("Numbers", _rows(
        "⁰ ¹ ² ³ ⁴ ⁵ ⁶ ⁷ ⁸ ⁹ ⁺ ⁻ ⁼ ⁽ ⁾ ⁿ",
        "₀ ₁ ₂ ₃ ₄ ₅ ₆ ₇ ₈ ₉ ₊ ₋ ₌ ₍ ₎",
        "½ ⅓ ⅔ ¼ ¾ ⅕ ⅖ ⅗ ⅘ ⅙ ⅚ ⅛ ⅜ ⅝ ⅞ ⁄",
    )),
    SymbolCategory("Units", _rows(
        "° ′ ″ ‰ μ Ω Å ℓ №",
        "¢ £ € ¥ ƒ",
        "℞ ℥ ʒ ℈ ℔",
    )),
    SymbolCategory("Punctuation", _rows(
        "— – ‘ ’ “ ” „ « » ‹ ›",
        "§ ¶ † ‡ • © ® ™ ¡ ¿ ª º",
    )),
    SymbolCategory("Arrows", _rows(
        "← → ↑ ↓ ↔ ↕ ↖ ↗ ↘ ↙",
        "⇐ ⇒ ⇑ ⇓ ⇔ ⇌",
    )),
    SymbolCategory("Accents", _rows(
        "à á â ã ä å æ ç è é ê ë ì í î ï ñ ò ó ô",
        "õ ö ø œ ù ú û ü ý ÿ ß ð þ ł č š ž ő ű ř",
        "À Á Â Ã Ä Å Æ Ç È É Ê Ë Ì Í Î Ï Ñ Ò Ó Ô",
        "Õ Ö Ø Œ Ù Ú Û Ü Ý Ÿ Ð Þ Ł Č Š Ž Ő Ű Ř",
    )),
)

# Compatibility twins Unicode discourages in favour of another character:
# never a key, whatever the category. Value = what to use instead.
DISCOURAGED: dict[str, str] = {
    "µ": "μ",   # MICRO SIGN -> GREEK SMALL LETTER MU (Unicode's preferred micro)
    "Ω": "Ω",   # OHM SIGN -> GREEK CAPITAL LETTER OMEGA
    "Å": "Å",   # ANGSTROM SIGN -> LATIN CAPITAL LETTER A WITH RING ABOVE
    "K": "K",        # KELVIN SIGN -> K
    "∆": "Δ",   # INCREMENT -> GREEK CAPITAL LETTER DELTA
    "∑": "Σ",   # N-ARY SUMMATION -> GREEK CAPITAL LETTER SIGMA
    "∏": "Π",   # N-ARY PRODUCT -> GREEK CAPITAL LETTER PI
    "−": "-",        # MINUS SIGN -> hyphen-minus, as typed and as OCR emits it
    "‐": "-",        # HYPHEN
    "‑": "-",        # NON-BREAKING HYPHEN
    "℃": "°C",  # DEGREE CELSIUS -> degree sign + C
    "℉": "°F",  # DEGREE FAHRENHEIT -> degree sign + F
}

# Tcl/Tk 8.6 stores text as UTF-16 and mishandles anything past the BMP.
_MAX_CODE_POINT = 0xFFFF


def symbol_problem(ch: str) -> str | None:
    """Why ``ch`` cannot be a key on the symbol keyboard, or None."""
    if len(ch) != 1:
        return f"is {len(ch)} code points; a key inserts exactly one"
    cp = ord(ch)
    if cp > _MAX_CODE_POINT:
        return "is outside the Basic Multilingual Plane (Tk 8.6 cannot draw it reliably)"
    category = unicodedata.category(ch)
    if ch.isspace() or category.startswith("Z"):
        return ("is whitespace; edits collapse it, and ALTO separates words "
                "with <SP> elements")
    problem = content_problem(ch)
    if problem is not None:
        return problem
    if cp < 0x7F:
        return "is plain ASCII, already on the keyboard"
    if category.startswith("C"):
        return f"is an invisible, private-use or unassigned character ({category})"
    if category.startswith("M"):
        return "is a combining mark; on its own it attaches to whatever precedes the cursor"
    normalized = unicodedata.normalize("NFC", ch)
    if normalized != ch:
        return (f"is not NFC-stable (normalizes to U+{ord(normalized[0]):04X}); "
                "use the canonical character")
    if ch in DISCOURAGED:
        return f"is a discouraged compatibility character; use {DISCOURAGED[ch]!r}"
    return None


def keyboard_problems(keyboard: tuple[SymbolCategory, ...] = SYMBOL_KEYBOARD) -> list[str]:
    """Every rule the layout breaks, one line each; empty when it is sound."""
    problems: list[str] = []
    names = [c.name for c in keyboard]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"category {name!r} appears more than once")
    for category in keyboard:
        if not category.symbols:
            problems.append(f"{category.name}: has no keys")
        seen: set[str] = set()
        for ch in category.symbols:
            if ch in seen:
                problems.append(f"{category.name}: {symbol_label(ch)} appears twice")
            seen.add(ch)
            reason = symbol_problem(ch)
            if reason is not None:
                problems.append(f"{category.name}: {symbol_label(ch)} {reason}")
    return problems


def symbol_label(ch: str) -> str:
    """The hover hint for a key: its code point and Unicode name, e.g.
    ``U+00BD Vulgar fraction one half`` — how a corrector tells μ (the
    Greek letter, on the keyboard) from look-alikes that are not."""
    if len(ch) != 1:
        return repr(ch)
    name = unicodedata.name(ch, "")
    return f"U+{ord(ch):04X} {name.capitalize()}" if name else f"U+{ord(ch):04X}"


def keyboard_payload(keyboard: tuple[SymbolCategory, ...] = SYMBOL_KEYBOARD) -> dict:
    """The layout as JSON for the web review client (``/api/config``), with
    the hover labels precomputed — the browser has no Unicode name table."""
    return {
        "categories": [{"name": c.name, "rows": [list(row) for row in c.rows]}
                       for c in keyboard],
        "labels": {ch: symbol_label(ch)
                   for c in keyboard for ch in c.symbols},
    }
