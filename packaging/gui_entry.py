"""PyInstaller entry point for the desktop GUI.

The stdio reconfigure mirrors ``cli_entry.py``: in case anything routes
through stdout/stderr (e.g. an exception traceback before the Tk window
opens), we don't want a UnicodeEncodeError on cp1252.
"""

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from alto_ocr.gui import main


if __name__ == "__main__":
    main()
