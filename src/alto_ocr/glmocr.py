"""Ollama HTTP client for the local glm-ocr vision model.

Empirically derived behavior on Ollama 0.21.2 + glm-ocr, captured in
scripts/probe_*.py. The constraints below were characterized on the
``glm-ocr:latest`` (F16) build; the default model tag is now
``glm-ocr:q8_0`` (Q8_0 quantization of the same 1.1B base), which shares
the same architecture and exhibits the same constraints (the GGML_ASSERT
projector quirk, the ``"---"`` blank sentinel, prompt sensitivity, and the
absence of structured/bbox output are all architecture-level, not
quantization-level):

  * Image input: must be a PNG (TIFs return degenerate output) and the
    longest edge must be <= 1024 px or the model raises GGML_ASSERT.
    At <= 672 px the model hallucinates; 1024 is the sweet spot.
  * Prompt: minimal "OCR" returns the full page including printed
    headers; longer prompts ("OCR this page. Output only ...") drop
    headers. Use the bare prompt.
  * Output: plain prose with paragraph breaks. No structural hints
    (lines, regions, bboxes) regardless of how the prompt asks.
  * Determinism: temperature=0 produces identical output across runs.
  * num_predict default (128) truncates pages — must be >= 8192.
  * Blank pages return "---" (3 chars). Normalize to empty.

No external HTTP library — stdlib urllib is enough and the project
keeps its no-extra-deps stance.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

import cv2
import numpy as np

# _CHAT_COMMENTARY_RE lives in ._util (stdlib-only) so the QA script can import
# it as a regression canary without pulling in this module's cv2/numpy. Its full
# precision-first design rationale is documented there.
from ._util import _CHAT_COMMENTARY_RE
from .config import (  # noqa: F401 - re-exported; they live in stdlib-only config
    DEFAULT_HOST,
    DEFAULT_MAX_EDGE_PX,
    DEFAULT_MODEL,
    DEFAULT_NUM_PREDICT,
    DEFAULT_TIMEOUT_S,
    GLMOCRConfig,
)

logger = logging.getLogger(__name__)


class GLMOCRError(RuntimeError):
    """glm-ocr HTTP/protocol failure. The pipeline catches and writes a stub."""


def resolve_endpoint(override: str | None = None) -> str:
    """Resolution order: explicit override > OLLAMA_HOST env > default.

    OLLAMA_HOST is the standard Ollama env var; mirroring it lets users
    point at a remote Ollama without a CLI flag.
    """
    if override:
        return override.rstrip("/")
    env = os.environ.get("OLLAMA_HOST", "").strip()
    return env.rstrip("/") if env else DEFAULT_HOST


def _array_to_png_b64(image: np.ndarray, max_edge: int) -> str:
    """Encode a numpy image as base64-PNG, downsizing to <= max_edge first.

    glm-ocr requires <= 1024 longest edge; oversized inputs return HTTP
    500 with GGML_ASSERT in the body.
    """
    h, w = image.shape[:2]
    if max(h, w) > max_edge:
        scale = max_edge / max(h, w)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)
    success, buf = cv2.imencode(".png", image)
    if not success:
        raise GLMOCRError("Failed to encode image as PNG for glm-ocr")
    return base64.b64encode(buf.tobytes()).decode("ascii")


_IMAGE_DESCRIPTION_LEADS = (
    "the image",
    "this image",
    "the photograph",
    "the picture",
    "this is a photo",
    "this is a photograph",
    "this is an illustration",
    "this is a picture",
    "this is a drawing",
    "this is an image",
    "this appears to be a",
    "the page is blank",
    "the page appears",
    # Refusal phrasings — distinct enough from real archival opening
    # lines that the precision is high. Matches e.g. "The text provided
    # is too blurry and fragmented to be accurately transcribed."
    "the text provided is",
    "the text in the image",
    "the text in the page",
    "the text appears to be too",
    "the text is too blurry",
    "the text is too illegible",
)


def _looks_like_image_description(text: str) -> bool:
    """Detect when glm-ocr is describing the image instead of transcribing.

    The model is multimodal: pages with visual content but no readable text
    (photographs, illustrations, severely degraded scans) make it slip out
    of OCR mode and produce chat-style "the image shows X" responses
    rather than a transcript. The blank-page sentinel ("---") only fires
    when the model sees genuinely nothing; description mode is a separate
    failure path with its own stereotyped phrasing. Match conservatively
    on the leading phrase so we don't false-positive on documents whose
    own text happens to mention images.
    """
    if not text:
        return False
    head = text.lstrip().lower()
    if any(head.startswith(p) for p in _IMAGE_DESCRIPTION_LEADS):
        return True
    # glm-ocr sometimes prefixes a short chat-style section header
    # ("OVERVIEW", "ANALYSIS", "DESCRIPTION", …) before the description proper,
    # which dodges the leading-phrase match. Skip a single short leading line
    # and re-check, so e.g. "OVERVIEW\n\nThe image provided is a blank page…"
    # is still caught. Kept conservative (one line, <=3 words) so a real short
    # title above a body paragraph isn't given extra license to false-positive.
    first, sep, rest = head.partition("\n")
    if sep and len(first.split()) <= 3:
        rest = rest.lstrip()
        if any(rest.startswith(p) for p in _IMAGE_DESCRIPTION_LEADS):
            return True
    return False


def _strip_chat_commentary(text: str) -> str:
    """Remove glm-ocr's chat-style meta-commentary lines, keeping real text.

    Drops any line matching ``_CHAT_COMMENTARY_RE`` (a description of the
    page, a self-correction, or a "the text reads:" re-quote of the content)
    and returns the rest. Removing whole lines — rather than truncating at the
    first one — preserves real transcription that the model interleaves after
    a commentary aside. If a page is *entirely* commentary, returns "".
    """
    if not text:
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines if not _CHAT_COMMENTARY_RE.match(ln)]
    n_removed = len(lines) - len(kept)
    result = "\n".join(kept).strip()
    if n_removed:
        if not result and any(ln.strip() for ln in lines):
            # Removed EVERY line — usually a pure description/commentary page,
            # but surface it (INFO, not DEBUG) so a mis-strip of a real page is
            # visible rather than silently emptied. See _CHAT_COMMENTARY_RE note.
            logger.info("glm-ocr: stripped entire page as chat-commentary (%d line(s)); "
                        "verify it was not real text: %r", n_removed, text[:80])
        else:
            logger.debug("glm-ocr: stripped %d chat-commentary line(s)", n_removed)
    return result


# Fallback resize edges to try when the model returns GGML_ASSERT.
# The assertion is a content × size interaction in the multimodal
# projector — small perturbations in image dimensions reliably avoid
# it on pages that trip it at the configured max_edge. Empirically
# verified on McKennaPapers MSP00213_84c635...:_00026 (fails at 1024,
# works at 1008). Descending so OCR quality stays as high as possible.
_GGML_ASSERT_FALLBACK_EDGES = (1008, 960, 920, 840, 800, 728, 672)


def _post_generate(img_b64: str, config: GLMOCRConfig) -> dict:
    """One /api/generate POST. Returns the parsed JSON payload or raises."""
    body = {
        "model": config.model,
        "prompt": "OCR",  # see module docstring — minimal prompt is best
        "images": [img_b64],
        "stream": False,
        "options": {
            "num_predict": config.num_predict,
            "temperature": 0.0,
        },
    }
    req = urllib.request.Request(
        f"{config.endpoint}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
            payload_bytes = resp.read()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise GLMOCRError(f"glm-ocr HTTP {e.code}: {body_text}") from None
    except urllib.error.URLError as e:
        raise GLMOCRError(f"glm-ocr connection error: {e.reason}") from None
    except TimeoutError:
        raise GLMOCRError(f"glm-ocr timeout after {config.timeout_s}s") from None
    try:
        return json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GLMOCRError(f"glm-ocr returned non-JSON: {e}") from None


# Repetition-loop collapse. glm-ocr — like many small OCR models — falls into
# degenerate loops on SPARSE pages (figure/photo/schematic captions, title
# pages): it latches onto the little real text present and repeats it, verbatim
# or lightly rephrased, until it exhausts the token budget. Verified on this
# collection: a one-line figure caption emitted 160×; a 4-line title block 38×.
# Left alone, the repeats land in the .txt transcript AND split into hundreds of
# bogus per-word <String> boxes. We collapse this deterministically.
#
# Why not Ollama's generation-time ``repeat_penalty``: tested at 1.3/1.5 it
# corrupts LEGITIMATELY repeated text (a radio-comms page's "Friendship Seven,
# Friendship Seven" became "Friendship Seven, F friendship Seen") and silently
# drops ~10% of real content on clean pages — undetectable at 4,241-page scale.
# So we never touch generation; we only collapse output that is *provably*
# degenerate, leaving every healthy page byte-identical.
_LOOP_MIN_REPEAT = 3          # a single repeated line must recur >= this often to count as a loop
_LOOP_MAX_PERIOD_FRAC = 0.67  # coarse cap on the period scan; the >= 3× floor in _find_periodic_cut is the real gate
_LOOP_MIN_UNIT_CHARS = 40     # a multi-/long-line repeating unit this big = a real loop

# Consecutive tandem-run collapse (catches the high-rep "messy" loops that exact
# whole-page periodicity misses because a stray line breaks the period — e.g.
# "Transfer Van" x608 + 3 strays, or a caption block x42 after a title block).
_LOOP_TANDEM_MAX_PERIOD = 30  # longest repeating block (in lines) to look for
_LOOP_TANDEM_MIN_REPS = 4     # a block must repeat back-to-back >= this many times
_LOOP_TANDEM_EXTREME_REPS = 30  # ...or this many, if its lines are all short tokens
_LOOP_TANDEM_LINE_CHARS = 20  # a "substantial" line: a unit with one this long is real text (caption/title), not a tally token

# Intra-line loop collapse. The two passes above are LINE-based; a loop confined
# to ONE line is invisible to them. That is the shape dense tabular pages and
# run-on table headers produce, because the model never emits a newline while
# looping: MSF00327 page 00022 is a *single* 450-word line of "1.67 67" x216,
# page 00012 one line of "0" x553, page 00019 a 1,475-word line whose column
# header repeats x43. (Page 00022 did not even reach the passes above — a text
# with no newline returned early.) Same discipline as _collapse_tandem, applied
# to whitespace tokens within a line: EXACT match, back-to-back, smallest period.
#
# Thresholds are measured, not guessed. Over the 4,844-page MSA00005 + MSF00327
# corpus, every intra-line tandem run of a SUBSTANTIAL unit (some token >=
# _LOOP_INLINE_TOKEN_CHARS) is either a real loop at 108x or legitimate content
# at exactly 3x ("Met or in process:", "Friendship Seven," — the same
# radio-comms page the module note above cites); short-token runs top out at 19x
# legitimately ("T" x19, "x" x9, "40.7" x7) against loops at 216x/549x/553x.
# Both floors sit at ~2x the observed legitimate maximum, far below any loop.
_LOOP_INLINE_MAX_PERIOD = 30   # longest repeating token-block to look for
_LOOP_INLINE_MIN_REPS = 6      # substantial unit: collapse at this many back-to-back repeats
_LOOP_INLINE_EXTREME_REPS = 40 # ...or this many, if the unit is all short tokens
_LOOP_INLINE_TOKEN_CHARS = 8   # a token this long makes a unit "substantial" (real words, not tally cells)

# Enumerated loop collapse. A loop the model NUMBERS as it goes ("1. X", "2. X",
# "3. X", ...) defeats every exact-match pass above, since no two lines are
# byte-identical: MSF00327 page 00079 emitted one invented sentence 71x this way
# (2,877 words on a page holding ~460). We compare lines by their body with the
# leading list marker removed. Guarded on body SIZE and ALPHABETIC content so a
# genuine numbered form or index — "1. .....", "2. ....." — is never touched.
# Corpus-wide (4,844 pages) there is no legitimate enumerated run at all: the
# only two hits are this loop and a 3x verbatim repeat that is also a loop.
#
# The 4x floor is deliberate PARITY with _LOOP_TANDEM_MIN_REPS, not a fresh
# judgement: the same four identical bodies WITHOUT markers already collapse via
# _collapse_tandem today. Stripping a marker before comparing changes what we can
# see, not how much repetition we treat as degenerate — so the residual risk here
# (a rating form whose numbered rows carry identical option text) is one the
# shipped code has always accepted for the unnumbered form of the same page.
_LOOP_ENUM_MIN_REPS = 4          # identical bodies back-to-back before it counts as a loop
_LOOP_ENUM_MIN_BODY_CHARS = 40   # a shorter body is a form field / index entry, not looped prose
_LOOP_ENUM_MIN_BODY_ALPHA = 12   # ...and it must be mostly text, not dot leaders or rules

# A leading list marker: "1.", "12)", "(3)", "iv.", "a]" — or a bullet.
_ENUMERATOR_RE = re.compile(r"^[(\[]?(?:\d{1,3}|[ivxlIVXL]{1,5}|[A-Za-z])[.):\]]\s+")
_BULLET_RE = re.compile(r"^[-*•·]\s+")


def _find_periodic_cut(nonblank: list[str]) -> int | None:
    """If the non-blank lines are a unit repeated >= 3×, return the unit length
    (in non-blank lines); else None.

    Detects ``nb == unit * k`` (k >= 3) with the final copy possibly *truncated*
    (the 2048-token cap often cuts the last repeat mid-way), via the periodicity
    test ``nb[i] == nb[i % P]`` for the smallest period ``P``. A verbatim block
    (or one long line) repeating back-to-back >= 3× is a generation loop, never
    real prose — so we keep one copy.

    The >= 3× floor (``L >= 2*P + 1`` ⇒ every unit line recurs >= 3×) is the same
    guarantee the old ``_LOOP_MIN_REPEAT`` gate gave. Content that appears only
    ~2× is far more often legitimately doubled — a repeated letterhead/caption, a
    carbon-copy form, or a running head bracketing one body line (the
    ``[X, body, X]`` shape, where only X recurs) — than a generation loop, so it
    is left intact rather than risk silently halving real text.

    LIMITATION (deliberate): a *short* repeating unit (< _LOOP_MIN_UNIT_CHARS,
    multi-line) is NOT collapsed — short repeated rows (tallies, ledgers,
    "0 0 0") are far more often legitimate content than a loop, so a tiny block
    is left duplicated rather than risk gutting real tabular data.
    """
    L = len(nonblank)
    if L < 2:
        return None
    max_period = int(L * _LOOP_MAX_PERIOD_FRAC)
    for P in range(1, max_period + 1):
        if not all(nonblank[i] == nonblank[i % P] for i in range(L)):
            continue
        # Require >= 3 occurrences of the unit (final copy may be truncated by
        # the token cap): L >= 2*P + 1 means every unit line recurs >= 3×.
        if L < 2 * P + 1:
            return None
        if P == 1:
            return P  # a single line repeated >= 3× is a loop regardless of length
        # multi-line block repeated >= 3×: a loop iff the unit is substantial
        if sum(len(s) for s in nonblank[:P]) >= _LOOP_MIN_UNIT_CHARS:
            return P
        return None
    return None


def _collapse_tandem(lines: list[str]) -> list[str]:
    """Collapse a CONSECUTIVE tandem repeat of a block to a single copy.

    Scans left-to-right; at each position finds the smallest period ``p`` whose
    block repeats back-to-back and, if that run qualifies as a loop, keeps one
    copy and skips the rest. EXACT match, order-preserving. A run qualifies iff
    it repeats >= _LOOP_TANDEM_MIN_REPS times AND (its unit contains a
    substantial line >= _LOOP_TANDEM_LINE_CHARS — real caption/title text, not a
    tally token — OR it repeats an extreme >= _LOOP_TANDEM_EXTREME_REPS times).
    Those guards keep it OFF legit short-token periodic content (roll-calls,
    tallies, constant table columns repeated a modest number of times) while
    catching the high-rep degenerate loops that exact whole-page periodicity
    misses because a stray line breaks the period. Verified on a 125-page batch:
    collapses 44 severe-loop pages, 0 clean pages touched.
    """
    out: list[str] = []
    i, n = 0, len(lines)
    stripped = [ln.strip() for ln in lines]  # strip once, not per (i, p) window
    while i < n:
        chosen_p = chosen_reps = 0
        for p in range(1, min(_LOOP_TANDEM_MAX_PERIOD, (n - i) // 2) + 1):
            unit = lines[i:i + p]
            unit_nb = [s for s in stripped[i:i + p] if s]
            if not unit_nb:
                continue  # don't anchor a run on a blank-only unit
            reps, j = 1, i + p
            while j + p <= n and lines[j:j + p] == unit:
                reps += 1
                j += p
            if reps < _LOOP_TANDEM_MIN_REPS:
                continue
            substantial = max(len(s) for s in unit_nb) >= _LOOP_TANDEM_LINE_CHARS
            if substantial or reps >= _LOOP_TANDEM_EXTREME_REPS:
                chosen_p, chosen_reps = p, reps
                break
        if chosen_p:
            out.extend(lines[i:i + chosen_p])  # keep exactly one copy
            logger.debug("glm-ocr: collapsed tandem loop (period=%d, reps=%d)",
                         chosen_p, chosen_reps)
            i += chosen_p * chosen_reps
        else:
            out.append(lines[i])
            i += 1
    return out


def _collapse_inline_run(words: list[str]) -> list[str]:
    """Collapse CONSECUTIVE tandem repeats of a token block within one line.

    The token-level twin of :func:`_collapse_tandem`, with the same shape and the
    same discipline: scan left-to-right, take the SMALLEST period whose block
    repeats back-to-back, and keep exactly one copy. EXACT match,
    order-preserving. A run qualifies iff it repeats >= _LOOP_INLINE_MIN_REPS
    times AND (its unit holds a token >= _LOOP_INLINE_TOKEN_CHARS — real words,
    so a loop — OR it repeats an extreme >= _LOOP_INLINE_EXTREME_REPS times).

    Those two guards are what keep it off legitimate tabular data, where a row of
    identical short cells ("6 6 6 6 6", "0 0 0") is ordinary content, not a loop.
    """
    out: list[str] = []
    i, n = 0, len(words)
    while i < n:
        chosen_p = chosen_reps = 0
        for p in range(1, min(_LOOP_INLINE_MAX_PERIOD, (n - i) // 2) + 1):
            unit = words[i:i + p]
            reps, j = 1, i + p
            while j + p <= n and words[j:j + p] == unit:
                reps += 1
                j += p
            if reps < _LOOP_INLINE_MIN_REPS:
                continue
            substantial = max(len(t) for t in unit) >= _LOOP_INLINE_TOKEN_CHARS
            if substantial or reps >= _LOOP_INLINE_EXTREME_REPS:
                chosen_p, chosen_reps = p, reps
                break
        if chosen_p:
            out.extend(words[i:i + chosen_p])  # keep exactly one copy
            logger.debug("glm-ocr: collapsed intra-line loop (period=%d, reps=%d)",
                         chosen_p, chosen_reps)
            i += chosen_p * chosen_reps
        else:
            out.append(words[i])
            i += 1
    return out


def _collapse_inline(text: str) -> str:
    """Apply :func:`_collapse_inline_run` to each line, per the module note above.

    A line that collapses to the same token count is emitted BYTE-IDENTICAL (the
    original string, not a re-join), so healthy pages keep their exact original
    spacing and this pass is a provable no-op on them.
    """
    out: list[str] = []
    for line in text.split("\n"):
        words = line.split()
        if len(words) < _LOOP_INLINE_MIN_REPS:
            out.append(line)  # too short to hold a qualifying run
            continue
        collapsed = _collapse_inline_run(words)
        out.append(line if len(collapsed) == len(words) else " ".join(collapsed))
    return "\n".join(out)


def _enum_body(line: str) -> str | None:
    """Return ``line`` stripped of a leading list marker, or None if it has none."""
    s = line.strip()
    body = _ENUMERATOR_RE.sub("", s, count=1)
    if body == s:
        body = _BULLET_RE.sub("", s, count=1)
    return None if body == s else body.strip()


def _collapse_enumerated(lines: list[str]) -> list[str]:
    """Collapse a run of list items whose bodies are identical to the first item.

    Catches the loop the model numbers as it goes ("1. X", "2. X", "3. X", ...),
    which every exact-match pass misses because the marker makes each line
    unique. Compares only the body; keeps the first item verbatim (marker and
    all) and drops the rest, including any blank lines between them — so items
    separated by blank lines, as glm-ocr emits them, are still seen as a run.

    Guarded on body length and alphabetic content (see the module note above), so
    a real numbered form or index whose items are short or mostly dot leaders is
    never collapsed. Order-preserving; returns ``lines`` unchanged if nothing
    qualifies.
    """
    nb_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    bodies = [_enum_body(lines[i]) for i in nb_idx]
    drop: set[int] = set()
    k, m = 0, len(nb_idx)
    while k < m:
        body = bodies[k]
        if (body is None
                or len(body) < _LOOP_ENUM_MIN_BODY_CHARS
                or sum(c.isalpha() for c in body) < _LOOP_ENUM_MIN_BODY_ALPHA):
            k += 1
            continue
        j = k
        while j + 1 < m and bodies[j + 1] == body:
            j += 1
        if j - k + 1 >= _LOOP_ENUM_MIN_REPS:
            logger.debug("glm-ocr: collapsed enumerated loop (reps=%d, body=%.40s)",
                         j - k + 1, body)
            # The token cap usually cuts the last item mid-way, leaving a body
            # that is a proper prefix of the looped one ("71. **Skyline"). That
            # is the same truncated final copy _find_periodic_cut allows for, so
            # take it with the run rather than leave a dangling fragment.
            if (j + 1 < m and bodies[j + 1]
                    and body.startswith(bodies[j + 1])
                    and len(bodies[j + 1]) < len(body)):
                j += 1
            drop.update(range(nb_idx[k] + 1, nb_idx[j] + 1))
        k = j + 1
    return lines if not drop else [ln for i, ln in enumerate(lines) if i not in drop]


def _collapse_repetition_loops(text: str) -> str:
    """Collapse degenerate glm-ocr repetition loops to their real content.

    Four EXACT, order-preserving passes — all leave healthy pages byte-identical
    and never touch legit repetitive content (roll-calls, tallies, ledgers,
    short-token table columns):

    1. **Consecutive tandem runs** (``_collapse_tandem``): a block repeated
       back-to-back many times (a caption ×42, "Transfer Van" ×608) → one copy,
       guarded so short-token periodic data is spared.
    2. **Enumerated runs** (``_collapse_enumerated``): the same body repeated
       under an incrementing list marker ("1. X", "2. X", … ×71) → first item.
       The marker is what makes 1. and 2. above blind to this shape.
    3. **Whole-page exact period** (``_find_periodic_cut``): the page is one unit
       repeated k× (k >= 3) with the final copy possibly truncated by the token
       cap (a caption ×160, a table ×3) → first copy. A block that appears only
       ~2× is treated as legitimately doubled content and left intact.
    4. **Intra-line runs** (``_collapse_inline``): a loop inside a SINGLE line
       ("1.67 67" ×216, "0" ×553), which 1.–3. cannot see because they compare
       whole lines. Runs last, on the output of the others.

    We deliberately do NOT do fuzzy / unique-ratio de-duplication (it cannot tell
    a generation loop from a genuinely repetitive document and would gut real
    tables), nor generation-time ``repeat_penalty`` (corrupts real repeated text;
    see module note). Non-exact "messy" loops are left duplicated rather than
    risk destroying content.
    """
    if "\n" in text:
        lines = _collapse_tandem(text.split("\n"))
        lines = _collapse_enumerated(lines)
        nb_idx = [i for i, ln in enumerate(lines) if ln.strip()]
        nonblank = [lines[i].strip() for i in nb_idx]
        period = _find_periodic_cut(nonblank)
        if period is not None:
            cut = nb_idx[period - 1] + 1  # keep through the period-th non-blank line
            if cut < len(lines):
                logger.debug("glm-ocr: collapsed periodic loop (period=%d non-blank lines, "
                             "%d -> %d lines)", period, len(lines), cut)
                lines = lines[:cut]
        text = "\n".join(lines)
    # Intra-line runs LAST: the line-based passes above see exactly the input
    # they always did (so their behavior is unchanged), and a collapse here can
    # never make two distinct lines identical and feed them a false match.
    return _collapse_inline(text)


def transcribe(image: np.ndarray, config: GLMOCRConfig) -> str:
    """Send one page image to glm-ocr; return the transcribed text.

    Raises GLMOCRError on transport / protocol failure. Empty-page
    responses ("---") and image-description hallucinations are both
    normalized to empty string so downstream consumers see a uniform
    "no text on this page" signal.

    On ``GGML_ASSERT`` failures (a model bug where certain image content
    × dimensions combinations trip the multimodal projector), retries at
    a sequence of smaller resize edges before giving up. Other model
    errors propagate immediately.
    """
    edges_to_try = [config.max_edge_px]
    edges_to_try.extend(e for e in _GGML_ASSERT_FALLBACK_EDGES if e < config.max_edge_px)

    last_error: GLMOCRError | None = None
    for attempt, edge in enumerate(edges_to_try):
        img_b64 = _array_to_png_b64(image, edge)
        try:
            payload = _post_generate(img_b64, config)
        except GLMOCRError as e:
            # Only retry the specific projector assertion; other failures
            # (timeouts, connection errors, non-JSON) are not size-related.
            if "GGML_ASSERT" not in str(e):
                raise
            last_error = e
            if attempt + 1 < len(edges_to_try):
                logger.warning(
                    "glm-ocr GGML_ASSERT at edge=%d; retrying at edge=%d",
                    edge, edges_to_try[attempt + 1],
                )
            continue
        break
    else:
        # Loop fell through without breaking — every attempt hit GGML_ASSERT.
        raise GLMOCRError(
            f"glm-ocr GGML_ASSERT persisted at every fallback edge "
            f"({edges_to_try}). Image is hitting the projector bug regardless "
            f"of size. Last error: {last_error}"
        ) from None

    text = (payload.get("response") or "").strip()

    # Blank-page sentinel — model returns "---" for true blanks. Normalize.
    if text == "---":
        return ""

    # Image-description fallback — model slipped out of OCR mode on a
    # page with no readable text. Suppress so the page reads as empty.
    if _looks_like_image_description(text):
        logger.warning(
            "glm-ocr returned an image description rather than a transcript "
            "(suppressing). First 120 chars: %r",
            text[:120],
        )
        return ""

    # Strip chat-style commentary the model appends/interleaves after a real
    # transcription ("The image shows…", "The text reads: …", "Wait, …"), then
    # collapse any exact repetition loop. Both no-op on clean pages. Order is
    # safe: stripping a commentary line sitting between two identical REAL blocks
    # makes them adjacent only ~2×, and _find_periodic_cut requires >= 3× before
    # it cuts — so the second real copy survives.
    text = _strip_chat_commentary(text)
    if not text:
        return ""
    pre_collapse_len = len(text)
    text = _collapse_repetition_loops(text)

    # A non-"stop" finish (reason=length) is only worth warning about when it
    # truncated REAL content. Measure shrink against the POST-strip length, NOT
    # the raw length: commentary removal is orthogonal to truncation, so only a
    # sharp LOOP collapse means the truncation was benign (raising
    # --glmocr-num-predict would just generate more loop). Gating on the raw
    # length would let heavy commentary-stripping mask a real truncation.
    done_reason = payload.get("done_reason")
    if done_reason and done_reason != "stop":
        if len(text) >= pre_collapse_len * 0.5:
            logger.warning("glm-ocr stopped with reason=%s; kept %d chars may be "
                           "truncated — consider raising --glmocr-num-predict",
                           done_reason, len(text))
        else:
            logger.debug("glm-ocr stopped with reason=%s but output collapsed %d->%d "
                         "chars (looping page; truncation benign)",
                         done_reason, pre_collapse_len, len(text))
    return text


def check_endpoint(config: GLMOCRConfig) -> str:
    """Return the model's full tag string if the endpoint is reachable
    and the model is loaded; raise GLMOCRError otherwise.

    Used at startup so the user gets an immediate, actionable error
    rather than failing on the first page.
    """
    req = urllib.request.Request(f"{config.endpoint}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        raise GLMOCRError(
            f"Cannot reach Ollama at {config.endpoint} ({e}). "
            "Start it with `ollama serve` or pass --glmocr-endpoint."
        ) from None

    models = [m.get("name", "") for m in data.get("models", [])]
    if config.model not in models:
        raise GLMOCRError(
            f"Model {config.model!r} not found on {config.endpoint}. "
            f"Available: {models}. Pull with `ollama pull {config.model}`."
        )
    return config.model
