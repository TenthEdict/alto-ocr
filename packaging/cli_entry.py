"""PyInstaller entry point for the CLI.

Kept as a tiny shim so the spec can ``Analysis(['cli_entry.py'])``
without PyInstaller picking up the package's ``__main__.py`` (we don't
have one).

The stdio reconfigure below forces UTF-8 on Windows. Rich (used by typer
for help formatting and by our ``doctor`` command) emits box-drawing and
status glyphs that crash the legacy Windows console (cp1252) with
``UnicodeEncodeError``. Reconfiguring before the app is imported means
``rich.Console()`` and any module-level prints use the corrected stream.
"""

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from alto_ocr.main import app


if __name__ == "__main__":
    app()
