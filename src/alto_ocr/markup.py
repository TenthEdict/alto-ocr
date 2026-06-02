"""Flatten glm-ocr's rich-text output to plain, searchable text.

glm-ocr (the whole family — ``glm-ocr:q8_0`` and ``glm-ocr:latest`` alike,
verified byte-identical) does not return flat prose for structured page
content: it renders tabular data as **HTML** ``<table>`` markup, mathematics as
**LaTeX** (``$...$``), and occasionally wraps output in Markdown code fences.
That collides with two parts of the pipeline's contract:

  * the ``Text/`` output is a *plain-text* transcript — HTML tags and LaTeX
    delimiters don't belong in it; and
  * the ALTO is split into per-word ``<String>`` boxes and the public archive
    UI highlights individual ``CONTENT`` matches, so an entire HTML table
    collapsed into one ``<String>`` (one giant box) defeats word-level
    highlighting, and ``$``/``\\frac`` glued onto a token makes it unsearchable.

``normalize_markup`` runs on each block's transcription *before* the engine
assembles the ``.txt`` and splits it into words, so both consumers see plain
text. It is deliberately lossy and pragmatic — HTML tables become rows of
space-separated cell values, LaTeX becomes its readable content with symbols
mapped to Unicode. Perfect structural or mathematical fidelity is a non-goal;
readable, searchable plain text is.

**Structural, not character-gated (load-bearing).** Each transform acts only on
the markup region it owns — real HTML tags (``</?tag…>``), genuine ``$…$`` /
``\\(…\\)`` / ``\\[…\\]`` math spans, ``|``-delimited table rows — and leaves
everything else verbatim. This matters because the same characters appear in
ordinary archival prose: ``$`` in a dollar amount, ``<``/``>`` in an inequality
or ``<placeholder>``, ``\\`` in a path, ``{}`` in a set. An earlier version
routed an *entire block* through the LaTeX/tag pipeline whenever a single such
character appeared anywhere, silently deleting currency, bracketed phrases and
braces from prose. The rule now: a lone ``$`` (no matching delimiter, or an
interior that isn't actually LaTeX) is left as a literal; a ``<`` not starting a
well-formed tag is left as a literal; etc. On ordinary prose ``normalize_markup``
is a genuine no-op apart from whitespace collapsing.

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

# Only a tag whose name is an actual HTML element glm-ocr emits (table markup
# plus basic inline formatting). This deliberately does NOT match prose like
# ``a < b`` (``<`` then a space), ``<45 deg>`` (``<`` then a digit), or a
# ``<placeholder>`` whose word isn't an HTML tag name — so inequalities and
# angle-bracket prose in the transcript survive. A leaked ``<td>`` / ``<br>`` /
# ``<sup>`` is still removed.
_HTML_TAG_NAMES = (
    "table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col"
    "|b|strong|i|em|u|s|sub|sup|br|span|p|div|code|pre|font|small|big"
    "|h[1-6]|ul|ol|li|hr|blockquote|a"
)
_TAG_RE = re.compile(r"</?(?:" + _HTML_TAG_NAMES + r")\b[^<>]*>", re.IGNORECASE)


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
# glm-ocr also emits pipe-delimited Markdown tables (a header row, a
# ``| :--- | :--- |`` delimiter row, then data rows). Flatten data rows to
# space-joined cells and drop the delimiter row. Two guards keep prose safe:
#   * only lines that *start* with ``|`` are touched (an inline math bar
#     ``∂u/∂y|_w`` never starts a line with ``|``); and
#   * a row is treated as the table delimiter only when it FOLLOWS a table row
#     and every cell is colons/2+ dashes — so a real data row of single dashes
#     or blanks (``| - | - |``, meaning empty/"N/A" cells) is kept as content,
#     not silently dropped.
_MD_DIVIDER_CELL_RE = re.compile(r":?-{2,}:?")


def _flatten_markdown_tables(text: str) -> str:
    out: list[str] = []
    prev_pipe_row = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            nonempty = [c for c in cells if c]
            is_divider = (
                prev_pipe_row
                and bool(nonempty)
                and all(_MD_DIVIDER_CELL_RE.fullmatch(c) for c in nonempty)
            )
            if is_divider:
                continue  # header/body separator row -> drop
            out.append(" ".join(nonempty))
            prev_pipe_row = True
        else:
            out.append(line)
            prev_pipe_row = False
    return "\n".join(out)


# --- Markdown code fences --------------------------------------------------
# glm-ocr sometimes wraps a stretch of output in a Markdown code fence
# (``` ```text `` ... `` ``` ``) and the bare delimiters then leak into the
# transcript. Drop any line that *starts* with a 3+ backtick/tilde fence —
# including one carrying a language tag and/or trailing text (``` ```python foo``)
# — keeping the content between the fences.
_CODE_FENCE_RE = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,}).*$")


# --- LaTeX -----------------------------------------------------------------
# LaTeX is transformed ONLY inside genuine math spans (see _strip_latex); the
# helpers below operate on a span's interior, where delimiters are already gone.
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

# Sizing / layout commands that vanish. Matched with a trailing non-letter
# lookahead so ``\left`` does NOT clobber the prefix of ``\leftarrow`` /
# ``\rightarrow`` / ``\leftrightarrow`` (which then map to Unicode arrows).
_LATEX_DROP_RE = re.compile(
    r"\\(?:displaystyle|textstyle|nolimits|limits|Bigl|Bigr|bigl|bigr"
    r"|Big|big|left|right)(?![a-zA-Z])"
)
# Spacing commands -> a single space (so ``a\,b`` becomes ``a b``, not ``ab``).
_LATEX_SPACING_RE = re.compile(r"\\[,;:!]|\\q?quad(?![a-zA-Z])")
# Environments (\begin{cases}…\end{cases}, align, array, …): wrapper goes,
# content stays.
_ENV_RE = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
# Escaped literals: ``\$`` is a real dollar sign, etc. Protect them through the
# generic-command pass via NUL placeholders, then restore. (Built once at
# module load — the maps are constant.)
_LATEX_ESCAPES = {r"\$": "$", r"\%": "%", r"\&": "&", r"\#": "#", r"\_": "_"}
_ESCAPE_TO_PH = {esc: f"\x00{i}\x00" for i, esc in enumerate(_LATEX_ESCAPES)}
_PH_TO_LITERAL = {ph: _LATEX_ESCAPES[esc] for esc, ph in _ESCAPE_TO_PH.items()}

_WRAP_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb|operatorname)\s*\{([^{}]*)\}"
)
_FRAC_RE = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_SUBSUP_RE = re.compile(r"([_^])\{([^{}]*)\}")
_ROWBREAK_RE = re.compile(r"\\\\(?:\[[^\]]*\])?")  # ``\\`` or ``\\[2pt]`` row break
_GENERIC_CMD_RE = re.compile(r"\\([a-zA-Z]+)")

# A math span: an explicit environment, ``$$…$$``, ``$…$``, ``\[…\]`` or
# ``\(…\)``. Only span interiors are transformed; text outside any span (prose,
# a lone ``$`` currency sign, a stray ``\``) is returned verbatim.
_MATH_SPAN_RE = re.compile(
    r"\\begin\{[^{}]*\}(.*?)\\end\{[^{}]*\}"
    r"|\$\$(.+?)\$\$"
    r"|\$(.+?)\$"
    r"|\\\[(.+?)\\\]"
    r"|\\\((.+?)\\\)",
    re.DOTALL,
)
# Distinguish a genuine ``$…$`` math span from currency. Currency is the only
# ambiguous case (``$$``/``\[``/``\(``/``\begin`` never appear in money). A
# single-``$`` pair is treated as currency — left verbatim — when it has no
# LaTeX marker AND a digit immediately follows the closing ``$`` (so that ``$``
# is really the *next* amount's opening sign, as in ``$2,000 to $4,500``). An
# unpaired ``$1,250`` never matches a span at all. Genuine inline math —
# ``$x^2$`` (marker), ``$k = 1.4$`` / ``$1/e$`` (closing ``$`` not followed by a
# digit) — still flattens.
_LATEX_MARKER_RE = re.compile(r"[\\^_{}]")


def _strip_latex_inner(s: str) -> str:
    """Flatten the interior of one math span to readable plain text."""
    for esc, ph in _ESCAPE_TO_PH.items():
        s = s.replace(esc, ph)
    s = _ROWBREAK_RE.sub("\n", s)        # ``\\`` / ``\\[2pt]`` -> newline (before bracket work)
    s = _ENV_RE.sub("", s)               # stray \begin/\end inside the span
    s = _LATEX_DROP_RE.sub("", s)        # \left \right \displaystyle ...
    s = _LATEX_SPACING_RE.sub(" ", s)    # \, \; \quad ...
    # Resolve the brace-introducing constructs to a fixpoint so nested braces
    # (``\mathbf{p_{i}}``, ``\frac{\sqrt{a}}{b}``) unwind level by level rather
    # than blocking the [^{}]* arguments.
    for _ in range(6):
        prev = s
        s = _SUBSUP_RE.sub(r"\1\2", s)   # _{i} -> _i, ^{2} -> ^2
        s = _SQRT_RE.sub(r"√(\1)", s)    # \sqrt{x} -> √(x)
        s = _WRAP_RE.sub(r"\1", s)       # \mathbf{x} -> x
        s = _FRAC_RE.sub(r"\1/\2", s)    # \frac{a}{b} -> a/b
        if s == prev:
            break
    s = _SYMBOL_RE.sub(lambda m: _LATEX_SYMBOLS[m.group(1)], s)
    s = _GENERIC_CMD_RE.sub(r"\1", s)    # unknown \cmd -> cmd (searchable)
    s = s.replace("{", "").replace("}", "")
    for ph, lit in _PH_TO_LITERAL.items():
        s = s.replace(ph, lit)
    return s


def _strip_latex(text: str) -> str:
    """Replace each genuine math span with its flattened interior.

    Text outside any span is untouched — so a lone ``$`` (currency), a stray
    backslash, or a ``$…$`` pair whose interior isn't actually LaTeX is returned
    exactly as-is.
    """
    def repl(m: "re.Match[str]") -> str:
        groups = m.groups()
        interior = next(g for g in groups if g is not None)
        # groups[2] is the single-``$…$`` alternative — the only currency-
        # ambiguous case. It's currency when it carries no LaTeX marker and a
        # digit immediately follows the closing ``$`` (that ``$`` opens the next
        # amount, e.g. "$2,000 to $4,500"); leave such a span verbatim.
        if groups[2] is not None:
            nxt = text[m.end()] if m.end() < len(text) else ""
            if nxt.isdigit() and not _LATEX_MARKER_RE.search(interior):
                return m.group(0)
        return _strip_latex_inner(interior)

    return _MATH_SPAN_RE.sub(repl, text)


# --- whitespace cleanup ----------------------------------------------------
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANKLINES_RE = re.compile(r"\n{3,}")


def normalize_markup(text: str) -> str:
    """Return ``text`` with HTML-table, code-fence, Markdown-table and LaTeX
    markup flattened to plain text.

    A genuine no-op on ordinary prose (only redundant whitespace is collapsed):
    each transform is scoped to its real markup region — well-formed HTML tags,
    ``|``-delimited table rows, genuine ``$…$`` / ``\\(…\\)`` / ``\\[…\\]`` math
    spans — so currency (``$``), inequalities / placeholders (``<…>``), paths
    (``\\``) and sets (``{}``) in prose are preserved. On structured content:
    HTML tables become newline-separated rows of space-separated cell values,
    and LaTeX becomes its readable content (``\\frac{a}{b}`` -> ``a/b``, common
    symbols mapped to Unicode). Intended to run on each block's transcription
    before the engine writes the ``.txt`` and splits words.
    """
    if not text:
        return text
    if "&" in text:                       # entity decode only when needed
        text = html.unescape(text)
    if "```" in text or "~~~" in text:    # drop code-fence delimiter lines
        text = _CODE_FENCE_RE.sub("", text)
    if "<" in text:                       # HTML: tables, then any stray tags
        if "<table" in text.lower():
            text = _TABLE_RE.sub(_table_to_text, text)
        text = _strip_tags(text)
    if "|" in text:                       # Markdown pipe tables
        text = _flatten_markdown_tables(text)
    if "$" in text or "\\" in text:       # LaTeX (only inside real math spans)
        text = _strip_latex(text)
    text = _MULTISPACE_RE.sub(" ", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()
