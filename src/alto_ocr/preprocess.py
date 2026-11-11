"""Image preprocessing for OCR accuracy.

Tesseract's accuracy is dominated by input image quality. This module runs
each TIF through a configurable pipeline before handing it to Tesseract.

Stages, in order:

1. **Load + grayscale** — color → 8-bit grayscale; bitonal (1-bit) lifted
   to grayscale so downstream stages have something to work with.
2. **DPI sanity** — read DPI from the TIF tags. If below ``min_dpi``, upscale
   with Lanczos so Tesseract sees ≥ ``min_dpi`` effective DPI. The corrected
   DPI is what we pass to Tesseract via ``--dpi``.
3. **Deskew** — estimate rotation via minimum-area-rect on the foreground;
   rotate to straighten. Skipped if the estimated angle is below a small
   threshold (correcting near-zero angles only adds resampling noise).
4. **Denoise** — bilateral filter; preserves stroke edges while smoothing
   scan grain.
5. **Contrast** — CLAHE (adaptive histogram equalization). Helps faded or
   unevenly-lit pages.
6. **Binarize** — adaptive Gaussian threshold. **Off by default** (v0.8.8):
   Tesseract 5's LSTM thresholds internally, and A/B runs on the fixtures
   plus the MSA00018 pilot showed pre-binarized input *lowers* mean WC on
   every collection tried (ZeffPapers 0.83→0.91 off, MSA00005 0.45→0.95)
   — faint strokes vanish and paper grain turns into punctuation speckle
   the LSTM reads as text. Available per-collection via ``--binarize``
   for the rare scan CLAHE can't normalize.
7. **Border crop** — trim solid black scanning margins that touch the page
   edge and confuse layout analysis.

Each stage is independently toggleable via ``PreprocessConfig``. Defaults
are conservative — safe for typical archival scans but tunable per-collection.

The preprocessed image and its effective DPI are returned together; the DPI
is what Tesseract should be told the image is, post-upscale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .alto_parse import upright
from .config import PreprocessConfig  # noqa: F401 - re-exported; lives in stdlib-only config

logger = logging.getLogger(__name__)

# Pillow refuses images over 2x its default limit (~179 megapixels) as
# decompression bombs and warns above half that. Oversize scans legitimately
# exceed it — a 24x36 inch sheet at 600 DPI is 311 MP — and every file here
# comes off the institution's own scanners. The review UIs already raised the
# limit; OCR did not, so the same page failed from the CLI, a batch worker or
# Goobi while working from the GUI, where importing the Review tab had raised
# it for the whole process (found 2026-11-11). Raised, not disabled: a
# corrupt header still cannot demand unbounded memory.
_MAX_IMAGE_PIXELS = 1_000_000_000
if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < _MAX_IMAGE_PIXELS:
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS


@dataclass
class PreprocessResult:
    """Output of the preprocessing pipeline."""

    image: np.ndarray  # 2-D uint8 (binary or grayscale)
    effective_dpi: int
    source_dpi: int | None
    stages_applied: tuple[str, ...] = field(default_factory=tuple)


def _read_dpi(path: Path) -> int | None:
    """Pull DPI from TIF metadata via Pillow. Returns None if unknown."""
    try:
        with Image.open(path) as img:
            dpi = img.info.get("dpi")
            if dpi:
                # PIL gives (x, y) — average if they differ slightly.
                x, y = float(dpi[0]), float(dpi[1])
                return int(round((x + y) / 2))
    except Exception as e:
        logger.debug("Could not read DPI from %s: %s", path, e)
    return None


def _load_grayscale(path: Path) -> np.ndarray:
    """Load any TIF/JPEG flavor as 8-bit grayscale, honoring orientation.

    Pillow handles TIF variants OpenCV chokes on (multi-page, 16-bit, LZW,
    bitonal G4-compressed). We let PIL do the decode, then hand a numpy
    array to OpenCV.

    EXIF/TIFF orientation is applied (``exif_transpose``) so the pixels OCR
    sees match what image viewers display. Some JPEG exports store a rotated
    page as sideways/upside-down pixels plus an orientation tag rather than
    as upright pixels; without this the page OCRs to rotated garbage even
    though it looks upright in Explorer. No-op when the file has no
    orientation tag (the common case) or the tag is already upright.
    """
    with Image.open(path) as img:
        n_frames = getattr(img, "n_frames", 1)
        if n_frames > 1:
            # Multi-page TIF — the first frame is the page. One file is one
            # page everywhere else (discovery's page count, the output stems,
            # the review UIs), so the rest are not OCR'd; say so, loudly,
            # rather than let pages vanish from the transcript unremarked.
            logger.warning(
                "%s holds %d frames; only the first is OCR'd, the other %d "
                "get no ALTO or transcript. If they are pages (not embedded "
                "thumbnails), split the file into one TIF per page and re-run.",
                path.name, n_frames, n_frames - 1,
            )
            img.seek(0)
        # Honor EXIF/TIFF orientation before any pixel processing, so detected
        # line boxes and ALTO coordinates land on the upright page.
        img = upright(img)
        if img.mode == "1":
            img = img.convert("L")
        elif img.mode not in ("L", "LA", "RGB", "RGBA"):
            img = img.convert("RGB")
        if img.mode == "LA":
            img = img.convert("L")
        arr = np.array(img)

    if arr.ndim == 3:
        if arr.shape[2] == 4:  # RGBA
            arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2GRAY)
        else:  # RGB
            arr = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if arr.dtype != np.uint8:
        # 16-bit grayscale → 8-bit.
        arr = cv2.normalize(arr, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return arr


# A DPI tag below this is metadata garbage, not a physical resolution —
# OpenCV-written TIFs default XResolution to 1, and some scanning software
# writes 0. Trusting such a tag makes the upscale factor min_dpi/tag
# (300x for a tag of 1): a gigapixel-scale Lanczos resize that grinds all
# cores for hours and stalls the batch with no exception for the per-page
# isolation to catch. A real page scan below 50 DPI would be illegible to
# OCR anyway, so below the floor the tag is ignored like a missing one.
_MIN_PLAUSIBLE_DPI = 50

# The floor above cannot catch a placeholder tag that looks like a real
# resolution: JPEG exports and camera files routinely carry 72 or 96 whatever
# they were shot at. A 4000x6000 JPEG tagged 72 would be resized to
# 16667x25000 (417 MP) — minutes of Lanczos, then a Tesseract run that times
# out, for a page that reads fine as it is. So an upscale whose result would
# exceed this many pixels is skipped the same way (found 2026-11-11). 150 MP
# is a 34x44 inch sheet at 300 DPI; a scan that large made below 300 DPI is
# rare enough that reading it at its native resolution is the better error.
_MAX_UPSCALED_PIXELS = 150_000_000


def _upscale_for_dpi(img: np.ndarray, source_dpi: int | None, min_dpi: int) -> tuple[np.ndarray, int]:
    """Upscale so effective DPI ≥ min_dpi. Returns (image, effective_dpi).

    If source DPI is unknown we trust the image as-is and report ``min_dpi``
    as the effective value (Tesseract needs *something* to pass via --dpi;
    its own auto-detect is unreliable on archival scans). An implausibly
    small DPI tag (< ``_MIN_PLAUSIBLE_DPI``) is treated the same as unknown,
    and so is one whose upscale would exceed ``_MAX_UPSCALED_PIXELS``.
    """
    if source_dpi is None:
        return img, min_dpi
    if source_dpi < _MIN_PLAUSIBLE_DPI:
        logger.warning(
            "Implausible DPI tag %d (< %d) — ignoring it; image passes through "
            "un-upscaled with --dpi %d. Check the source file's metadata.",
            source_dpi, _MIN_PLAUSIBLE_DPI, min_dpi,
        )
        return img, min_dpi
    if source_dpi >= min_dpi:
        return img, source_dpi
    scale = min_dpi / source_dpi
    new_w = int(round(img.shape[1] * scale))
    new_h = int(round(img.shape[0] * scale))
    if new_w * new_h > _MAX_UPSCALED_PIXELS:
        logger.warning(
            "DPI tag %d would upscale this %dx%d image to %dx%d (%.0f MP) — "
            "the tag is taken to be a placeholder, not the scan resolution; "
            "image passes through un-upscaled with --dpi %d.",
            source_dpi, img.shape[1], img.shape[0], new_w, new_h,
            new_w * new_h / 1e6, min_dpi,
        )
        return img, min_dpi
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4), min_dpi


def _estimate_skew(img: np.ndarray) -> float:
    """Estimate skew angle in degrees, positive = counter-clockwise.

    Uses minimum-area-rect on the foreground pixels of an inverted Otsu
    binarization. Robust on text-heavy pages; degrades gracefully on pages
    with sparse content (returns 0.0).
    """
    inv = cv2.bitwise_not(img)
    _, thresh = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    coords = cv2.findNonZero(thresh)
    if coords is None or len(coords) < 50:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # cv2.minAreaRect returns angle in (-90, 0]; normalize to deskew rotation.
    if angle < -45:
        angle = 90 + angle
    return -angle


def _deskew(img: np.ndarray, cfg: PreprocessConfig) -> tuple[np.ndarray, float]:
    angle = _estimate_skew(img)
    if abs(angle) < cfg.deskew_min_angle_deg:
        return img, 0.0
    if abs(angle) > cfg.deskew_max_angle_deg:
        logger.debug("Suspicious skew estimate %.2f°, skipping rotation", angle)
        return img, 0.0
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,  # white, so a paper-colored backdrop fills corners
    )
    return rotated, angle


def _crop_borders(img: np.ndarray, threshold: int) -> np.ndarray:
    """Trim a solid dark frame surrounding the page, if present.

    Bins by column/row mean intensity; finds the inset where rows/columns
    stop being almost entirely dark. Conservative: only crops if it would
    keep ≥ 80% of each dimension (otherwise the heuristic likely misfired).
    """
    h, w = img.shape[:2]
    col_mean = img.mean(axis=0)
    row_mean = img.mean(axis=1)

    def first_above(arr: np.ndarray) -> int:
        idx = np.argmax(arr > threshold)
        return int(idx) if arr[idx] > threshold else 0

    left = first_above(col_mean)
    right = w - first_above(col_mean[::-1])
    top = first_above(row_mean)
    bottom = h - first_above(row_mean[::-1])

    if right - left < 0.8 * w or bottom - top < 0.8 * h:
        return img
    if (left, top, right, bottom) == (0, 0, w, h):
        return img
    return img[top:bottom, left:right]


def preprocess(path: Path, cfg: PreprocessConfig | None = None) -> PreprocessResult:
    """Run the configured preprocessing pipeline on one TIF.

    Raises on unrecoverable I/O or decode errors so the caller can isolate
    the failure to a single page rather than corrupting an output set.
    """
    cfg = cfg or PreprocessConfig()
    stages: list[str] = []

    img = _load_grayscale(path)
    stages.append("grayscale")

    source_dpi = _read_dpi(path)
    native_shape = img.shape
    img, eff_dpi = _upscale_for_dpi(img, source_dpi, cfg.min_dpi)
    if img.shape != native_shape:  # not for a tag that was ignored
        stages.append(f"upscale({source_dpi}→{eff_dpi}dpi)")

    if cfg.do_deskew:
        img, angle = _deskew(img, cfg)
        if angle != 0.0:
            stages.append(f"deskew({angle:.2f}°)")

    if cfg.do_denoise:
        img = cv2.bilateralFilter(
            img, cfg.bilateral_d, cfg.bilateral_sigma_color, cfg.bilateral_sigma_space
        )
        stages.append("denoise")

    if cfg.do_clahe:
        clahe = cv2.createCLAHE(
            clipLimit=cfg.clahe_clip,
            tileGridSize=(cfg.clahe_tile, cfg.clahe_tile),
        )
        img = clahe.apply(img)
        stages.append("clahe")

    if cfg.do_binarize:
        block = cfg.adaptive_block_size
        if block % 2 == 0:
            block += 1
        img = cv2.adaptiveThreshold(
            img, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block,
            cfg.adaptive_C,
        )
        stages.append("binarize")

    if cfg.do_border_crop:
        before = img.shape
        img = _crop_borders(img, cfg.border_crop_threshold)
        if img.shape != before:
            stages.append(f"crop({before}→{img.shape})")

    return PreprocessResult(
        image=img,
        effective_dpi=eff_dpi,
        source_dpi=source_dpi,
        stages_applied=tuple(stages),
    )


def write_image(img: np.ndarray, dest: Path) -> None:
    """Write a preprocessed image to disk (PNG by default — lossless, small)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = dest.suffix.lower()
    if suffix in (".tif", ".tiff", ".png", ".bmp"):
        ok = cv2.imwrite(str(dest), img)
    else:
        ok = cv2.imwrite(str(dest.with_suffix(".png")), img)
    if not ok:
        raise IOError(f"Failed to write image: {dest}")
