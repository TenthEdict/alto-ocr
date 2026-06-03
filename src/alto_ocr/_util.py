"""Small cross-cutting helpers shared by the CLI and the QA scripts.

Kept dependency-free (stdlib only) so a script can import it without pulling
in typer/rich/opencv.
"""

from __future__ import annotations

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
