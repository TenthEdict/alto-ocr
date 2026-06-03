"""Small cross-cutting helpers shared by the CLI and the QA scripts.

Kept dependency-free (stdlib only) so a script can import it without pulling
in typer/rich/opencv. Holds both a runtime helper (``force_utf8_output``) and
the glm-ocr chat-commentary pattern (``_CHAT_COMMENTARY_RE``): the engine uses
it to strip model self-talk, and the QA script (``scripts/scan_output.py``)
imports it as a regression canary. Sharing one compiled pattern keeps the two
from drifting — and lives here, not in ``glmocr``, so the canary import doesn't
drag in cv2/numpy.
"""

from __future__ import annotations

import re
import sys


def force_utf8_output() -> None:
    """Make stdout/stderr tolerate non-ASCII output on Windows consoles.

    Rich tables/log lines and OCR text emit characters (``→``, ``←``, em
    dashes, ½, CJK) that the legacy Windows cp1252 console codec can't encode,
    which otherwise crashes at print time (observed under redirected output).
    Reconfigure the streams to UTF-8 with a lenient error handler, and on
    Windows switch the console output code page to UTF-8 so renderers that
    encode via the code page (not the Python stream) succeed too.

    All best-effort: every step is guarded so a locked-down environment can't
    stop the tool from running. No-op on POSIX, where stdout is already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            # 65001 = CP_UTF8. Output code page only; leave the input code page
            # alone so console input handling is unaffected.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # best effort; never fatal
            pass


# Chat-style commentary glm-ocr appends AFTER (or interleaves with) a real
# transcription — a separate, more pervasive failure than the whole-page
# descriptions ``_looks_like_image_description`` catches. Observed on MSA00005
# front-matter / photo / title / cover pages: the model transcribes the page,
# then adds meta lines like "The image is a title page…", "The text in the
# image reads: …" (re-quoting the real text — the source of the duplicate
# content), "The text provided does not contain…", "Wait, let me re-read…",
# "I'll just output the text." We delete such lines, keeping everything else
# (so interleaved real text survives).
#
# PRECISION-FIRST (this is line-DELETION applied to an accepted transcript, so
# a false match silently loses real content across ALL collections). The
# patterns are therefore restricted to phrasings that are genuinely model-voice
# and do NOT collide with ordinary archival prose:
#   * "The image …" — the model's own word for the SCAN; real captions say
#     "the photograph/figure/the photo at right", so we deliberately do NOT
#     match photograph/photo/picture/illustration/etc. (those occur in real
#     captions, e.g. "The photograph shows the 1923 class.").
#   * "the text in the image / the text provided / the text is clear|legible|
#     as follows|a description / the text is centered|positioned|arranged|…
#     (layout description) / the text reads:|states: (colon-anchored, the
#     model's re-quote form) / the text on the image|page|scan …" — meta about
#     the scan that documents never write. We do NOT match bare "the text is a
#     <noun>" ("The text is a binding contract."), "the text reads, in part …",
#     "the text states that …", "the text on the monument …" (inscription /
#     caption prose), "the text mentions/describes/…", or "the document is
#     about …" (all real prose).
#   * Self-talk: "I'll output/transcribe/re-read", "I'm unable/sorry", "let me
#     re-read/re-examine", "here's the text[:]" (bare/colon-anchored lead-in),
#     "Wait, <let me|the text|the word|I'll|…>", bare "It's:". We do NOT match "I will
#     provide/use …", "Here is the text of the <document> …", bare "wait,/okay,/
#     sorry,/unfortunately,", "I cannot/can't", or "let me check/look/try" (all
#     occur in real letters, speeches, minutes).
# Apostrophes accept both ASCII ' (U+0027) and curly ’ (U+2019) — glm-ocr emits
# the curly form. KNOWN RESIDUAL: a real line literally starting "The image
# is/shows …" is still stripped (uncommon — documents rarely open a line that
# way); a fully-stripped page is logged at INFO so it can be spot-checked.
_CHAT_COMMENTARY_RE = re.compile(
    r"^\s*(?:"
    # "The image is/shows/…" — model's word for the scan itself (NOT photograph/picture/…).
    r"(?:the|this)\s+image\s+(?:is|was|shows?|depicts?|appears?|contains?|displays?|presents?|features?|portrays?|seems?|provides?)\b"
    r"|(?:the|this)\s+image\s*$"                       # bare "The image" fragment
    # Meta about "the text"/scan — phrasings real documents do not use. The
    # reads/states/on-the arms are NARROWED to the model's re-quote form (a
    # trailing colon / "as follows", or a scan-referent noun) so they no longer
    # eat real prose like "The text reads, in part …", "The text states that …",
    # or inscription captions "The text on the monument …".
    r"|the\s+text\s+(?:provided\b|in\s+the\s+(?:image|picture|photo|document|page)\b"
    r"|on\s+the\s+(?:image|picture|photo|scan|page|document)\b"
    r"|reads?(?:\s*:|\s+as\s+follows)|states?(?:\s*:|\s+as\s+follows)|is:"
    r"|is\s+(?:clear|clearly|legible|illegible|as\s+follows|a\s+description|a\s+transcription"
    # layout description (e.g. "The text is centered on a white background…") — model-voice
    r"|centered|centred|positioned|aligned|arranged|formatted|justified|displayed)\b)"
    # Model self-talk / refusals (kept specific to avoid real first-person prose).
    # Dropped "provide"/"use" — they collide with ordinary letter/minutes prose
    # ("I will provide a full accounting…", "I will use my best judgment…").
    r"|i(?:['’]ll|\s+will)\s+(?:just\s+)?(?:output|transcribe|re-?read)\b"
    r"|i(?:['’]m|\s+am)\s+(?:unable|sorry)\b"
    r"|let\s+me\s+re-?(?:read|examine)\b"
    # "Here is the text[:]" lead-in — anchored to a bare/colon-terminated form so
    # it does not eat "Here is the text of the resolution adopted by the board."
    r"|here(?:['’]s|\s+is)\s+the\s+(?:text|transcription)"
    r"(?:\s+(?:of|from|in|on)\s+the\s+(?:image|picture|photo|scan|page|document))?\s*:?\s*$"
    r"|wait,?\s+(?:let\s+me|the\s+text|the\s+word|i['’]ll|i['’]m|here)\b"
    r"|it['’]s:\s*$"                              # bare "It's:"
    r")",
    re.IGNORECASE,
)
