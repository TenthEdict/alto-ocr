# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for alto_ocr.

Produces a single onedir bundle containing two executables that share
one ``_internal/`` directory:

  * ``alto-ocr-gui[.exe]`` — windowed Tk GUI (no console pops up).
  * ``alto-ocr[.exe]``     — console CLI (typer-based).

Tesseract is NOT bundled — it stays a separate one-time install. The GUI
auto-detects the standard UB-Mannheim install on Windows and the
``--tesseract-path`` flag / settings field cover non-PATH installs.

Build with::

    python -m PyInstaller packaging/alto_ocr.spec --clean --noconfirm

Output lands in ``dist/alto-ocr/``. ``packaging/build.py`` wraps that
plus the zip step and the README copy.
"""

from pathlib import Path

# PyInstaller invokes the spec from the project root (where you run
# pyinstaller), so SPECPATH points to ``packaging/``. Build paths
# relative to the project root so the spec is location-stable.
PROJECT_ROOT = Path(SPECPATH).parent
SRC_ROOT = PROJECT_ROOT / "src"

block_cipher = None

# Both analyses share the same import paths and hidden-import set; only
# the entry script differs. The collect_submodules calls are belt-and-
# suspenders for libs that PyInstaller's static analysis sometimes misses
# (lxml in particular has a lot of dynamic dispatch).
common_kwargs = dict(
    pathex=[str(SRC_ROOT), str(PROJECT_ROOT / "packaging")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "alto_ocr",
        "alto_ocr.main",
        "alto_ocr.gui",
        "alto_ocr.pipeline",
        "alto_ocr.ocr",
        "alto_ocr.glmocr",
        "alto_ocr.line_detect",
        "alto_ocr.preprocess",
        "alto_ocr.mets",
        "alto_ocr.walker",
        # PIL plugins for TIFF I/O — usually auto-detected, listed
        # explicitly so a wheel rebuild can't silently drop them.
        "PIL.TiffImagePlugin",
        "PIL.PngImagePlugin",
        "PIL.JpegImagePlugin",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't use these and they're heavy.
        "matplotlib",
        "scipy",
        "pandas",
        "tornado",
        "IPython",
        "jupyter",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a_cli = Analysis([str(PROJECT_ROOT / "packaging" / "cli_entry.py")], **common_kwargs)
a_gui = Analysis([str(PROJECT_ROOT / "packaging" / "gui_entry.py")], **common_kwargs)

# MERGE deduplicates the shared runtime + deps so we ship one Python
# interpreter, one numpy, one cv2, etc. — the second EXE references
# resources owned by the first.
MERGE(
    (a_cli, "alto-ocr",     "alto-ocr"),
    (a_gui, "alto-ocr-gui", "alto-ocr-gui"),
)

pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="alto-ocr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                  # CLI keeps its console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="alto-ocr-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # GUI: suppress console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Single COLLECT producing one folder with both EXEs side by side and a
# shared _internal/ subfolder.
coll = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    exe_gui,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="alto-ocr",
)
