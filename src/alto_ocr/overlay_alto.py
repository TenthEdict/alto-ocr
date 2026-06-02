"""Render ALTO line boxes over a source image, for alignment QA.

The GLMOCR engine derives line geometry itself (glm-ocr returns no
coordinates), so the failure mode to watch for is a *misaligned* box — one
whose rectangle doesn't sit on the text it transcribes. This tool overlays
every ``TextLine`` box from an ALTO file onto the page image so that
misalignment shows up immediately. It is the same method used to find the
original splice defect.

Usage::

    python -m src.alto_ocr.overlay_alto ALTO.xml SOURCE.tif OUT.png
    python -m src.alto_ocr.overlay_alto ALTO.xml SOURCE.tif OUT.png --width 6

After the institution-format conversion, ALTO coordinates are in ``mm10``
(1/10 mm) while the source image is in pixels. The two are reconciled by
scaling with the ratio of image pixels to the ALTO ``Page`` dimensions, so
the overlay lands correctly regardless of the image's DPI or any uniform
upscale applied during preprocessing. (Raw pre-conversion ALTO is in pixels
with a pixel-valued ``Page``, so the same ratio is 1:1 and it works there
too.)

Pure stdlib + Pillow on purpose: this is a QA aid that should run without the
OCR pipeline's heavier dependencies (OpenCV/lxml) installed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw

# Archival scans are huge; don't trip Pillow's decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = None

# Post-conversion ALTO is v2; accept v3 as well so the tool also works on raw
# engine/Tesseract output during debugging.
_ALTO_NAMESPACES = (
    "http://www.loc.gov/standards/alto/ns-v2#",
    "http://www.loc.gov/standards/alto/ns-v3#",
)


def _find_first(root: ET.Element, tag: str) -> ET.Element | None:
    for ns in _ALTO_NAMESPACES:
        el = root.find(f".//{{{ns}}}{tag}")
        if el is not None:
            return el
    return None


def _find_all(root: ET.Element, tag: str) -> list[ET.Element]:
    for ns in _ALTO_NAMESPACES:
        found = root.findall(f".//{{{ns}}}{tag}")
        if found:
            return found
    return []


def overlay(
    alto_path: Path,
    image_path: Path,
    out_path: Path,
    *,
    width: int = 4,
    color: tuple[int, int, int] = (220, 0, 0),
) -> int:
    """Draw every ALTO ``TextLine`` box on ``image_path``; save to ``out_path``.

    Returns the number of boxes drawn. Raises ``SystemExit`` with an
    actionable message when the ALTO has no usable ``Page`` geometry (e.g. a
    failure-stub ALTO, whose ``<Layout/>`` has nothing to overlay).
    """
    im = Image.open(image_path).convert("RGB")
    img_w, img_h = im.size

    root = ET.parse(str(alto_path)).getroot()
    page = _find_first(root, "Page")
    if page is None:
        raise SystemExit(f"No <Page> element in {alto_path} - nothing to overlay.")
    try:
        page_w = int(page.get("WIDTH", "0"))
        page_h = int(page.get("HEIGHT", "0"))
    except ValueError:
        raise SystemExit(f"<Page> WIDTH/HEIGHT are not integers in {alto_path}.")
    if page_w <= 0 or page_h <= 0:
        raise SystemExit(f"<Page> has non-positive dimensions in {alto_path}.")

    # mm10 (or pixel) ALTO frame → image pixel frame.
    sx = img_w / page_w
    sy = img_h / page_h

    draw = ImageDraw.Draw(im)
    count = 0
    for ln in _find_all(root, "TextLine"):
        try:
            x = int(ln.get("HPOS", "0"))
            y = int(ln.get("VPOS", "0"))
            w = int(ln.get("WIDTH", "0"))
            h = int(ln.get("HEIGHT", "0"))
        except ValueError:
            continue
        draw.rectangle(
            [x * sx, y * sy, (x + w) * sx, (y + h) * sy],
            outline=color, width=width,
        )
        count += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="overlay_alto",
        description="Overlay ALTO TextLine boxes on a source image for QA.",
    )
    parser.add_argument("alto", type=Path, help="ALTO .xml file")
    parser.add_argument("image", type=Path, help="source image (TIF/PNG/JPEG/...)")
    parser.add_argument("out", type=Path, help="output PNG with boxes drawn")
    parser.add_argument(
        "--width", type=int, default=4,
        help="box outline stroke width in px (default: 4)",
    )
    args = parser.parse_args(argv)

    n = overlay(args.alto, args.image, args.out, width=args.width)
    print(f"Drew {n} line box(es) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
