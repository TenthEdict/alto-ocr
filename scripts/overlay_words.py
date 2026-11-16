"""Render ALTO *word-level* boxes over a source image, for word-box QA.

Companion to ``src/alto_ocr/overlay_alto.py`` (which draws ``TextLine``
boxes). Word geometry is what the public archive's hit boxes and the
review tools draw, so the failure modes to watch for are at the word
level:

  * a word box whose left/right edge clips the glyphs it names
    (e.g. a column crop starting a couple characters too far right);
  * a search hit highlighting roughly the wrong spot on the line.

This tool makes both visible. It draws every ``<String>`` box, and with
``--search TERM`` it dims the page and highlights only the String elements
whose CONTENT matches — exactly what the public archive UI does when a user
searches the transcript and it draws hit rectangles over the page image.

Usage::

    # Draw every word box (green), optionally line boxes too (blue):
    python scripts/overlay_words.py ALTO.xml SOURCE.tif OUT.png
    python scripts/overlay_words.py ALTO.xml SOURCE.tif OUT.png --line-boxes

    # Simulate a transcript search — highlight only matching words (red):
    python scripts/overlay_words.py ALTO.xml SOURCE.tif OUT.png --search engineer

Coordinate handling mirrors ``overlay_alto.py``: after the
institution-format conversion ALTO is in ``mm10`` with an ``mm10``-valued
``<Page>``, so scaling every box by image-pixels / Page-dimension lands it
correctly regardless of DPI or any uniform preprocessing upscale. Raw
pre-conversion ALTO is in pixels with a pixel-valued Page, where the same
ratio is 1:1 — so the tool works on both.

Pure stdlib + Pillow on purpose (no OpenCV/lxml), so it runs as a
standalone QA aid without the pipeline's heavier deps. User-facing strings
are ASCII only (the legacy Windows cp1252 console can't encode arrows/
em-dashes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Allow running as `python scripts/overlay_words.py` from the repo root. The
# shared ALTO helpers live in the package but are pure stdlib + Pillow (no
# OpenCV/lxml), so this stays a light standalone QA aid.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.alto_parse import box_of, find_all, load_image_and_scale  # noqa: E402


def overlay_words(
    alto_path: Path,
    image_path: Path,
    out_path: Path,
    *,
    search: str | None = None,
    line_boxes: bool = False,
    width: int = 3,
) -> tuple[int, int]:
    """Draw word (and optionally line) boxes from ``alto_path`` on the image.

    Returns ``(words_drawn, matches)``. When ``search`` is set, the page is
    dimmed and only matching String boxes are drawn (filled + outlined) so
    the hit stands out the way the archive UI renders it; ``matches`` is how
    many String CONTENTs contained the term (case-insensitive). Without a
    search, every word box is drawn and ``matches`` is 0.

    Raises ``SystemExit`` with an actionable message when the ALTO has no
    usable ``<Page>`` geometry (e.g. a failure-stub ALTO with an empty
    ``<Layout/>``).
    """
    im, root, sx, sy = load_image_and_scale(alto_path, image_path)

    needle = search.lower() if search else None

    # In search mode, dim the whole page first so the highlighted hits pop —
    # same visual idea as the archive UI graying the page behind a match.
    if needle is not None:
        dim = Image.blend(im, Image.new("RGB", im.size, (255, 255, 255)), 0.55)
        im = dim
    draw = ImageDraw.Draw(im, "RGBA")

    if line_boxes:
        for ln in find_all(root, "TextLine"):
            b = box_of(ln)
            if b is None:
                continue
            x, y, w, h = b
            draw.rectangle(
                [x * sx, y * sy, (x + w) * sx, (y + h) * sy],
                outline=(0, 90, 220, 255), width=max(1, width - 1),
            )

    words_drawn = 0
    matches = 0
    for st in find_all(root, "String"):
        b = box_of(st)
        if b is None:
            continue
        x, y, w, h = b
        rect = [x * sx, y * sy, (x + w) * sx, (y + h) * sy]
        content = st.get("CONTENT", "")
        if needle is not None:
            if needle not in content.lower():
                continue
            # Highlighted hit: translucent fill + solid outline.
            draw.rectangle(rect, fill=(255, 60, 60, 90), outline=(210, 0, 0, 255), width=width)
            matches += 1
        else:
            draw.rectangle(rect, outline=(0, 170, 0, 255), width=width)
        words_drawn += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)
    return words_drawn, matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="overlay_words",
        description="Overlay ALTO String (word) boxes on a source image for QA.",
    )
    parser.add_argument("alto", type=Path, help="ALTO .xml file")
    parser.add_argument("image", type=Path, help="source image (TIF/PNG/JPEG/...)")
    parser.add_argument("out", type=Path, help="output PNG with boxes drawn")
    parser.add_argument(
        "--search", default=None, metavar="TERM",
        help="Highlight only words whose CONTENT contains TERM (case-insensitive), "
             "dimming the rest - simulates the archive UI's hit highlighting.",
    )
    parser.add_argument(
        "--line-boxes", action="store_true",
        help="Also draw the enclosing TextLine boxes (blue).",
    )
    parser.add_argument(
        "--width", type=int, default=3,
        help="box outline stroke width in px (default: 3)",
    )
    args = parser.parse_args(argv)

    words, matches = overlay_words(
        args.alto, args.image, args.out,
        search=args.search, line_boxes=args.line_boxes, width=args.width,
    )
    if args.search:
        print(f"Matched {matches} word box(es) for '{args.search}' -> {args.out}")
    else:
        print(f"Drew {words} word box(es) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
