"""Preservation scans -> pixels a browser can actually display.

Browsers do not render TIF, and the collections' pages run 7 MB (diaries)
to 177 MB (oversize), so every page view needs a server-side conversion.
The scheme is one decode per page, never one per request:

1. First touch of a page decodes the TIF once and writes a **working copy**:
   a JPEG capped at ``working_px`` on its long edge, cached on disk.
2. Every later request -- the page view at whatever width, and each word
   crop -- is resampled or cropped out of that working copy.

A 177 MB oversize TIF takes seconds to decode; a corrector clicking through
sixty words on one page would pay that sixty times without step 1. Crops are
taken at the working copy's own resolution rather than downscaled, because
the crop is what the corrector actually reads the word from -- it is the
web equivalent of the Tk editor bar's magnified snippet.

Coordinates crossing the API are always in **source-image pixels**, the same
frame as ``remediate.Word.box_px``. The working copy's scale is an internal
detail; nothing outside this module needs to know a working copy exists.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from .._util import atomic_write_bytes

logger = logging.getLogger("alto_ocr.webreview.imaging")

# Pillow refuses images above ~178 Mpx as decompression bombs. The
# collections' oversize scans legitimately exceed that (a 177 MB page at
# 600 DPI is ~400 Mpx), and every file here came off the institution's own
# scanners through a folder an operator chose -- there is no untrusted
# upload path into this server. Raised rather than disabled so a genuinely
# absurd file still fails instead of exhausting the host's memory.
_MAX_PIXELS = 900_000_000
if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < _MAX_PIXELS:
    Image.MAX_IMAGE_PIXELS = _MAX_PIXELS

JPEG_QUALITY = 88
CROP_JPEG_QUALITY = 92      # the corrector reads words off crops; artefacts here cost accuracy


@dataclass(frozen=True)
class WorkingCopy:
    """A cached JPEG of one page plus the geometry to map back to the scan."""

    path: Path
    source_size: tuple[int, int]
    working_size: tuple[int, int]

    @property
    def scale(self) -> float:
        """Working pixels per source pixel (<= 1.0)."""
        return self.working_size[0] / self.source_size[0]


def _cache_key(image_path: Path, max_px: int) -> str:
    """Identity of a rendered page: which file, which bytes, which cap.

    mtime and size are in the key so replacing a scan in place -- a rescan
    of a damaged page, which does happen -- invalidates the render instead
    of serving the old pixels under the new page's word boxes.
    """
    try:
        st = image_path.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    raw = f"{os.fspath(image_path.resolve())}|{stamp}|{max_px}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _open_source(image_path: Path) -> Image.Image:
    """Open a scan as RGB, first frame only.

    ``convert("RGB")`` is not cosmetic: the preservation masters include
    1-bit bitonal, 16-bit greyscale and CMYK pages, none of which JPEG can
    encode directly.
    """
    im = Image.open(image_path)
    try:
        im.seek(0)          # multi-page TIF: page 1 is the object's page
    except (EOFError, ValueError):
        pass
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


def working_copy(image_path: Path, cache_dir: Path, *, max_px: int) -> WorkingCopy:
    """The cached working JPEG for a scan, rendering it if it is not there.

    Writes through ``atomic_write_bytes`` so two correctors opening the same
    page at the same moment cannot serve each other a half-written JPEG --
    the loser of the race overwrites with identical bytes.
    """
    key = _cache_key(image_path, max_px)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{key}.jpg"
    meta = cache_dir / f"{key}.txt"

    if cached.is_file() and meta.is_file():
        try:
            sw, sh, ww, wh = (int(v) for v in meta.read_text(encoding="utf-8").split())
            return WorkingCopy(cached, (sw, sh), (ww, wh))
        except (OSError, ValueError):
            logger.debug("Discarding unreadable cache metadata %s", meta)

    with _open_source(image_path) as im:
        source_size = im.size
        working = im
        if max(im.size) > max_px:
            scale = max_px / max(im.size)
            working = im.resize(
                (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                Image.LANCZOS,
            )
        buf = BytesIO()
        working.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        working_size = working.size
        if working is not im:
            working.close()

    atomic_write_bytes(cached, buf.getvalue())
    atomic_write_bytes(
        meta,
        f"{source_size[0]} {source_size[1]} {working_size[0]} {working_size[1]}".encode("utf-8"),
    )
    return WorkingCopy(cached, source_size, working_size)


def render_page(wc: WorkingCopy, *, width: int | None) -> bytes:
    """The whole page as JPEG, at *width* px (None = the working copy as-is).

    Serving the cached bytes untouched when no resize is needed matters more
    than it looks: it is the common case for the page view, and it turns a
    decode-plus-encode into a file read.
    """
    if width is None or width >= wc.working_size[0]:
        return wc.path.read_bytes()
    with Image.open(wc.path) as im:
        im = im.convert("RGB")
        height = max(1, round(im.height * (width / im.width)))
        resized = im.resize((width, height), Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        resized.close()
    return buf.getvalue()


def render_crop(wc: WorkingCopy, box_source_px: tuple[float, float, float, float], *,
                pad: int = 12, zoom: float = 1.0, max_width: int = 1400) -> bytes:
    """A word's region of the scan as JPEG, boxed in **source** pixels.

    *pad* is in source pixels and gives the word visual context -- a word
    cropped exactly to its ALTO box is much harder to read than the same
    word with a sliver of its neighbours, because ascenders and descenders
    routinely fall outside the box Tesseract reports.

    Empty box or a box entirely off the page yields a 1x1 JPEG rather than
    an error: malformed ALTO geometry is a data problem for the corrector to
    see on the page, not a reason to fail their request.
    """
    x, y, w, h = box_source_px
    s = wc.scale
    left = int(max(0, (x - pad) * s))
    top = int(max(0, (y - pad) * s))
    right = int(min(wc.working_size[0], (x + w + pad) * s))
    bottom = int(min(wc.working_size[1], (y + h + pad) * s))
    if right <= left or bottom <= top:
        left, top, right, bottom = 0, 0, 1, 1

    with Image.open(wc.path) as im:
        im = im.convert("RGB")
        crop = im.crop((left, top, right, bottom))
    target_w = min(max_width, max(1, round(crop.width * max(zoom, 0.1))))
    if target_w != crop.width:
        target_h = max(1, round(crop.height * (target_w / crop.width)))
        resized = crop.resize((target_w, target_h), Image.LANCZOS)
        crop.close()
        crop = resized
    buf = BytesIO()
    crop.save(buf, format="JPEG", quality=CROP_JPEG_QUALITY, optimize=True)
    crop.close()
    return buf.getvalue()


def prune_cache(cache_dir: Path, *, max_bytes: int) -> int:
    """Drop the oldest-accessed renders until the cache fits *max_bytes*.

    Approximately LRU, and only approximately: Linux mounts default to
    ``relatime``, so a read updates atime at most once a day and the order
    degrades toward oldest-written. Not worth fixing with a side index --
    renders are pure functions of scans that still exist, so a wrong
    eviction costs one re-decode and nothing else.

    Called after writes rather than on a timer, so the server has no
    background thread to shut down cleanly. Returns bytes freed.
    """
    try:
        entries = [
            (p.stat().st_atime, p.stat().st_size, p)
            for p in cache_dir.glob("*.jpg") if p.is_file()
        ]
    except OSError:
        return 0
    total = sum(size for _atime, size, _p in entries)
    if total <= max_bytes:
        return 0
    freed = 0
    for _atime, size, path in sorted(entries):
        if total - freed <= max_bytes:
            break
        try:
            path.unlink()
            path.with_suffix(".txt").unlink(missing_ok=True)
            freed += size
        except OSError:
            continue
    return freed
