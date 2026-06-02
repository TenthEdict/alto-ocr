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
import urllib.error
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "glm-ocr:q8_0"
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_EDGE_PX = 1024
DEFAULT_NUM_PREDICT = 8192


class GLMOCRError(RuntimeError):
    """glm-ocr HTTP/protocol failure. The pipeline catches and writes a stub."""


@dataclass(frozen=True)
class GLMOCRConfig:
    endpoint: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_edge_px: int = DEFAULT_MAX_EDGE_PX
    num_predict: int = DEFAULT_NUM_PREDICT


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
_LOOP_MIN_LINES = 6          # too few lines to judge repetition reliably
_LOOP_DEGENERATE_RATIO = 0.5  # unique/total non-blank lines at/above this = healthy
_LOOP_MIN_REPEAT = 3          # some line must recur >= this often to count as a loop


def _collapse_repetition_loops(text: str) -> str:
    """Collapse a degenerate repetition loop to its unique lines, in order.

    Fires ONLY when the output is dominated by repetition — fewer than
    ``_LOOP_DEGENERATE_RATIO`` unique non-blank lines AND some line repeated
    at least ``_LOOP_MIN_REPEAT`` times. Both conditions are required so a
    healthy page (≈100% unique lines in practice) — including a data table
    with a few incidentally-equal rows — is returned untouched. On a loop,
    we keep each distinct line's first occurrence in original order, which
    preserves the real content (the loop's first cycle) and discards the rest.
    """
    if "\n" not in text:
        return text
    lines = text.split("\n")
    nonblank = [s for s in (ln.strip() for ln in lines) if s]
    if len(nonblank) < _LOOP_MIN_LINES:
        return text
    distinct = set(nonblank)
    if len(distinct) / len(nonblank) >= _LOOP_DEGENERATE_RATIO:
        return text  # healthy page — leave byte-identical
    # Need a genuinely repeated line, not just many distinct short lines.
    if len(nonblank) - len(distinct) < _LOOP_MIN_REPEAT - 1:
        return text
    max_repeat = max(nonblank.count(s) for s in distinct)
    if max_repeat < _LOOP_MIN_REPEAT:
        return text
    seen: set[str] = set()
    kept: list[str] = []
    for ln in lines:
        key = ln.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(ln.rstrip())
    logger.debug("glm-ocr: collapsed repetition loop (%d lines -> %d unique)",
                 len(lines), len(kept))
    return "\n".join(kept)


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

    if payload.get("done_reason") and payload["done_reason"] != "stop":
        logger.warning("glm-ocr stopped with reason=%s (length=%d)",
                       payload["done_reason"], len(text))

    # Collapse degenerate repetition loops (sparse-page failure mode). No-op on
    # healthy pages; see _collapse_repetition_loops.
    return _collapse_repetition_loops(text)


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
