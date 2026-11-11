"""Shared ALTO-reading helpers for the overlay QA tools.

Both ``overlay_alto.py`` (TextLine boxes) and ``scripts/overlay_words.py``
(per-word String boxes) read an ALTO file, resolve its namespace, pull integer
box geometry, and scale ALTO coordinates onto the source image. Those pieces
lived as drifting copies in each tool; they live here once instead.

After the institution-format conversion ALTO is in ``mm10`` (1/10 mm) with an
``mm10``-valued ``<Page>``; scaling every box by image-pixels / Page-dimension
lands it correctly regardless of the image's DPI or any uniform preprocessing
upscale. Raw pre-conversion ALTO is in pixels with a pixel-valued ``Page``, so
the same ratio is 1:1 and the helpers work there too.

Pure stdlib + Pillow on purpose: the QA overlays must run without the OCR
pipeline's heavier dependencies (OpenCV/lxml) installed.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps

# Archival scans are huge (hundreds of megapixels), so raise Pillow's
# decompression-bomb guard far past any real scan — but keep a ceiling so a
# corrupt or malicious file can't demand unbounded memory (Pillow warns at
# this value and errors at 2x it).
Image.MAX_IMAGE_PIXELS = 1_000_000_000

# Post-conversion ALTO is v2; accept v3 as well so the tools also work on raw
# engine/Tesseract output during debugging.
ALTO_NAMESPACES = (
    "http://www.loc.gov/standards/alto/ns-v2#",
    "http://www.loc.gov/standards/alto/ns-v3#",
)


_ORIENTATION_TAG = 0x0112  # EXIF / TIFF "Orientation"


def upright(img: Image.Image) -> Image.Image:
    """``img`` with its EXIF/TIFF orientation tag applied — the page as an
    image viewer shows it, which is the frame ALTO coordinates are in.

    The one definition of that frame: OCR (``preprocess._load_grayscale``),
    the Tk Review tab, the browser review's working copy and the QA overlays
    all load scans through this. Before 2026-11-11 only OCR applied the tag,
    so a JPEG stored sideways plus an orientation tag was read upright and
    then reviewed sideways — word boxes off the words, and for a 90° tag the
    width and height swapped under the box scaling.

    Returns ``img`` itself when there is nothing to do (the common case).
    ``ImageOps.exif_transpose`` would return a full copy there, which for a
    scan of hundreds of megapixels is a second copy of the page in memory.
    """
    try:
        orientation = img.getexif().get(_ORIENTATION_TAG, 1)
    except Exception:  # noqa: BLE001 - unreadable metadata: show the pixels as stored
        return img
    if orientation not in (2, 3, 4, 5, 6, 7, 8):
        return img
    return ImageOps.exif_transpose(img)


def find_first(root: ET.Element, tag: str) -> ET.Element | None:
    for ns in ALTO_NAMESPACES:
        el = root.find(f".//{{{ns}}}{tag}")
        if el is not None:
            return el
    return None


def find_all(root: ET.Element, tag: str) -> list[ET.Element]:
    for ns in ALTO_NAMESPACES:
        found = root.findall(f".//{{{ns}}}{tag}")
        if found:
            return found
    return []


def box_of(el: ET.Element) -> tuple[int, int, int, int] | None:
    """``(HPOS, VPOS, WIDTH, HEIGHT)`` as ints, or ``None`` if any is missing or
    non-numeric (a malformed box is skipped rather than drawn at the origin)."""
    try:
        return (
            int(el.get("HPOS", "")),
            int(el.get("VPOS", "")),
            int(el.get("WIDTH", "")),
            int(el.get("HEIGHT", "")),
        )
    except ValueError:
        return None


def load_image_and_scale(
    alto_path: Path, image_path: Path,
) -> tuple[Image.Image, ET.Element, float, float]:
    """Open ``image_path`` and parse ``alto_path``; return ``(image, root, sx,
    sy)`` where ``sx``/``sy`` map ALTO coordinates onto image pixels.

    Raises ``SystemExit`` with an actionable message when the ALTO has no usable
    ``<Page>`` geometry (e.g. a failure-stub ALTO whose ``<Layout/>`` is empty).
    """
    im = upright(Image.open(image_path)).convert("RGB")
    img_w, img_h = im.size

    root = ET.parse(str(alto_path)).getroot()
    page = find_first(root, "Page")
    if page is None:
        raise SystemExit(f"No <Page> element in {alto_path} - nothing to overlay.")
    try:
        page_w = int(page.get("WIDTH", "0"))
        page_h = int(page.get("HEIGHT", "0"))
    except ValueError:
        raise SystemExit(f"<Page> WIDTH/HEIGHT are not integers in {alto_path}.")
    if page_w <= 0 or page_h <= 0:
        raise SystemExit(f"<Page> has non-positive dimensions in {alto_path}.")

    return im, root, img_w / page_w, img_h / page_h
