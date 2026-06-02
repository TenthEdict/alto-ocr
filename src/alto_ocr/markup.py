"""Flatten glm-ocr's rich-text output to plain, searchable text.

glm-ocr (the whole family — ``glm-ocr:q8_0`` and ``glm-ocr:latest`` alike,
verified byte-identical) does not return flat prose for structured page
content: it renders tabular data as **HTML** ``<table>`` markup and
mathematics as **LaTeX** (``$...$``). That collides with two parts of the
pipeline's contract:

  * the ``Text/`` output is a *plain-text* transcript — HTML tags and LaTeX
    delimiters don't belong in it; and
  * the ALTO is split into per-word ``<String>`` boxes and the public
    archive UI highlights individual ``CONTENT`` matches, so an entire HTML
    table collapsed into one ``<String>`` (one giant box) defeats word-level
    highlighting, and ``$``/``\\frac`` glued onto a token makes it unsearchable.

``normalize_markup`` runs on each block's transcription *before* the engine
assembles the ``.txt`` and splits it into words, so both consumers see plain
text. It is deliberately lossy and pragmatic — HTML tables become rows of
space-separated cell values (one row per line), LaTeX becomes its readable
content with delimiters/wrappers stripped and common symbols mapped to
Unicode. Perfect structural or mathematical fidelity is a non-goal; readable,
searchable plain text is. On input that contains no markup (ordinary prose,
the common case) it is a near no-op — only redundant whitespace is collapsed.

Pure stdlib (``re`` + ``html``), matching the module's no-extra-deps stance.
"""

from __future__ import annotations

import html
import re

# --- HTML tables -----------------------------------------------------------
# Model tables are simple and well-formed:
#   <table ...><thead><tr><th>..</th></tr></thead><tbody><tr><td>..</td>..</tr>..</tbody></table>
# Extract rows -> cells; join cells with a space, rows with a newline. thead/
# tbody wrappers are simply not collected (only <tr>/<td>/<th> are). Anything
# that slips through is removed by the generic tag strip afterwards.
_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    # Replace with a space, not "", so a tag wedged between words (``a<br>b``)
    # doesn't glue them; the global whitespace collapse tidies up afterwards.
    return _TAG_RE.sub(" ", s)


def _table_to_text(match: "re.Match[str]") -> str:
    rows: list[str] = []
    for row_body in _ROW_RE.findall(match.group(1)):
        cells = [_strip_tags(c).strip() for c in _CELL_RE.findall(row_body)]
        if any(cells):
            rows.append(" ".join(cells).strip())
    return "\n".join(rows)


# --- Markdown tables -------------------------------------------------------
# glm-ocr also emits pipe-delimited Markdown tables (a leading-pipe row plus a
# ``| :--- | :--- |`` divider). Flatten data rows to space-joined cells and
# drop divider rows. Scoped to lines that *start* with ``|`` so an inline math
# bar (``∂u/∂y|_w``) — which never starts a line with ``|`` — is left untouched.
_MD_DIVIDER_CHARS = set("|-: ")


def _flatten_markdown_tables(text: str) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("|"):
            if set(s) <= _MD_DIVIDER_CHARS:
                continue  # ``| :--- | :--- |`` divider row -> drop
            cells = [c.strip() for c in s.strip("|").split("|")]
            out.append(" ".join(c for c in cells if c))
        else:
            out.append(line)
    return "\n".join(out)


# --- Markdown code fences --------------------------------------------------
# glm-ocr sometimes wraps a whole page (or a stretch of it) in a Markdown code
# fence — ``` ```text `` ... `` ``` `` — and the bare fence delimiters then leak
# into the plain-text transcript. Strip any line that is *only* a fence
# delimiter (3+ backticks or tildes, optional language tag), keeping the content
# between the fences so it can still be normalized as prose/table/math. Matches
# whole lines only, so an inline backtick in body text is left untouched.
_CODE_FENCE_RE = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,})[ \t]*[\w+.#-]*[ \t]*$")


# --- LaTeX -----------------------------------------------------------------
# Common symbol commands -> Unicode. Matched with a trailing non-letter
# lookahead so e.g. ``\pi`` does not fire inside ``\partial``.
_LATEX_SYMBOLS = {
    "partial": "∂", "nabla": "∇", "infty": "∞",
    "int": "∫", "iint": "∬", "oint": "∮",
    "sum": "∑", "prod": "∏",
    "times": "×", "cdot": "·", "div": "÷",
    "pm": "±", "mp": "∓", "ast": "*", "star": "*",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
    "propto": "∝", "sim": "~", "cong": "≅",
    "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}
# Longest-first so the alternation never settles for a prefix match.
_SYMBOL_RE = re.compile(
    r"\\(" + "|".join(sorted(_LATEX_SYMBOLS, key=len, reverse=True)) + r")(?![a-zA-Z])"
)

# Sizing / layout hints that should vanish entirely.
_LATEX_DROP = (
    r"\left", r"\right", r"\bigl", r"\bigr", r"\Bigl", r"\Bigr",
    r"\big", r"\Big", r"\displaystyle", r"\textstyle", r"\limits", r"\nolimits",
)
# Spacing commands -> a single space (so e.g. ``a\,b`` becomes ``a b``, not ``ab``).
_LATEX_SPACING = (r"\,", r"\;", r"\:", r"\!", r"\quad", r"\qquad")
# LaTeX environments (\begin{cases}...\end{cases}, align, array, ...): the
# wrapper goes, the content stays.
_ENV_RE = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
# Escaped literals: ``\$`` is a real dollar sign, etc. Restore them so a
# currency amount survives the math-delimiter strip.
_LATEX_ESCAPES = {r"\$": "$", r"\%": "%", r"\&": "&", r"\#": "#", r"\_": "_"}

_WRAP_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb|operatorname)\s*\{([^{}]*)\}"
)
_FRAC_RE = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_SUBSUP_RE = re.compile(r"([_^])\{([^{}]*)\}")
_MATH_DELIM_RE = re.compile(r"(?<!\\)\$+|\\[()\[\]]")
_GENERIC_CMD_RE = re.compile(r"\\([a-zA-Z]+)")


def _strip_latex(text: str) -> str:
    # Escaped literals first, via placeholders so the math-delimiter pass and
    # the generic-command pass don't clobber them.
    placeholders = {f"\x00{i}\x00": lit for i, lit in enumerate(_LATEX_ESCAPES.values())}
    for (esc, _lit), ph in zip(_LATEX_ESCAPES.items(), placeholders):
        text = text.replace(esc, ph)

    text = _MATH_DELIM_RE.sub("", text)            # $...$, \(, \), \[, \]
    text = text.replace("\\\\", "\n")              # LaTeX row break -> newline
    text = _ENV_RE.sub("", text)                   # \begin{cases}/\end{cases} -> gone
    for cmd in _LATEX_DROP:
        text = text.replace(cmd, "")
    for cmd in _LATEX_SPACING:
        text = text.replace(cmd, " ")
    # Resolve the brace-introducing inner constructs first (wrappers,
    # sub/superscripts, sqrt) so a \frac whose arguments contain them is no
    # longer blocked by their braces when the frac regex runs.
    text = _WRAP_RE.sub(r"\1", text)               # \text{x}/\mathbf{x} -> x
    text = _SUBSUP_RE.sub(r"\1\2", text)           # _{0} -> _0, ^{2} -> ^2
    text = _SQRT_RE.sub(r"√(\1)", text)       # \sqrt{x} -> √(x)
    # \frac twice to resolve one level of frac-in-frac nesting.
    text = _FRAC_RE.sub(r"\1/\2", text)
    text = _FRAC_RE.sub(r"\1/\2", text)
    text = _SYMBOL_RE.sub(lambda m: _LATEX_SYMBOLS[m.group(1)], text)
    text = _GENERIC_CMD_RE.sub(r"\1", text)        # unknown \cmd -> cmd (searchable)
    text = text.replace("{", "").replace("}", "")

    for ph, lit in placeholders.items():
        text = text.replace(ph, lit)
    return text


# --- whitespace cleanup ----------------------------------------------------
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANKLINES_RE = re.compile(r"\n{3,}")


def normalize_markup(text: str) -> str:
    """Return ``text`` with HTML-table and LaTeX markup flattened to plain text.

    A near no-op on ordinary prose (which carries no markup) — only collapses
    runs of spaces/blank lines. On structured content: HTML tables become
    newline-separated rows of space-separated cell values, and LaTeX becomes
    its readable content (delimiters/wrappers removed, ``\\frac{a}{b}`` -> ``a/b``,
    common symbols mapped to Unicode). Intended to run on each block's
    transcription before the engine writes the ``.txt`` and splits words.
    """
    if not text:
        return text
    text = html.unescape(text)
    if "```" in text or "~~~" in text:   # drop code-fence delimiter lines, keep content
        text = _CODE_FENCE_RE.sub("", text)
    text = _TABLE_RE.sub(_table_to_text, text)
    text = _strip_tags(text)              # any stray non-table tags (<b>, <br>, ...)
    if "|" in text:                       # cheap guard; skip on the common no-pipe case
        text = _flatten_markdown_tables(text)
    if "\\" in text or "$" in text:       # cheap guard; skip LaTeX work on plain prose
        text = _strip_latex(text)
    text = _MULTISPACE_RE.sub(" ", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()
