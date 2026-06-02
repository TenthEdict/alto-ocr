"""Cross-platform build driver for the alto_ocr PyInstaller bundle.

Run from the project root::

    python packaging/build.py

What it does:
  1. Cleans previous build/ and dist/ artefacts.
  2. Runs PyInstaller against ``packaging/alto_ocr.spec``.
  3. Drops ``packaging/dist_README.txt`` into the bundle as ``README.txt``.
  4. Zips the bundle into ``dist/alto-ocr-<version>-<platform>.zip``.

The output works on whichever platform you build on (PyInstaller bundles
are platform-specific). Build on Windows for Windows targets, Linux/WSL
for Linux targets.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = PROJECT_ROOT / "packaging"
SPEC_FILE = PACKAGING_DIR / "alto_ocr.spec"
DIST_README = PACKAGING_DIR / "dist_README.txt"
BUNDLE_DIR = PROJECT_ROOT / "dist" / "alto-ocr"
BUILD_DIR = PROJECT_ROOT / "build"


def _read_version() -> str:
    init = PROJECT_ROOT / "src" / "alto_ocr" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            # __version__ = "0.1.0"
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def _platform_tag() -> str:
    sysname = platform.system().lower()  # 'windows', 'linux', 'darwin'
    arch = platform.machine().lower() or "x86_64"
    return f"{sysname}-{arch}"


def _clean() -> None:
    for d in (BUNDLE_DIR.parent, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _run_pyinstaller() -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(SPEC_FILE),
        "--clean",
        "--noconfirm",
        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(BUILD_DIR),
    ]
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)


def _drop_readme() -> None:
    if DIST_README.is_file():
        shutil.copy2(DIST_README, BUNDLE_DIR / "README.txt")


def _zip_bundle(version: str) -> Path:
    zip_name = f"alto-ocr-{version}-{_platform_tag()}.zip"
    zip_path = PROJECT_ROOT / "dist" / zip_name
    if zip_path.exists():
        zip_path.unlink()
    print(f">> zipping {BUNDLE_DIR.name}/ -> {zip_path.name}")
    # Use the bundle's parent as the arc-root so the zip extracts into a
    # single ``alto-ocr/`` folder rather than dumping its contents flat.
    arc_root = BUNDLE_DIR.parent
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in BUNDLE_DIR.rglob("*"):
            zf.write(path, path.relative_to(arc_root))
    return zip_path


def main() -> int:
    if not SPEC_FILE.is_file():
        print(f"missing spec: {SPEC_FILE}", file=sys.stderr)
        return 1
    version = _read_version()
    print(f"alto_ocr version: {version}")
    print(f"platform tag:     {_platform_tag()}")
    _clean()
    _run_pyinstaller()
    if not BUNDLE_DIR.is_dir():
        print(f"build did not produce {BUNDLE_DIR}", file=sys.stderr)
        return 1
    _drop_readme()
    zip_path = _zip_bundle(version)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\nDone. Bundle: {BUNDLE_DIR}")
    print(f"      Zip:    {zip_path}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
