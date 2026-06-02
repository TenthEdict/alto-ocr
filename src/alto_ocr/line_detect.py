"""Layout analysis for the GLMOCR engine: page → text blocks → line bboxes.

glm-ocr returns clean transcribed prose with no spatial information. The
public archive UI needs ALTO with per-word pixel bboxes for hit
highlighting, and (load-bearing) the engine transcribes one *block* at a
time so each block's text pairs unambiguously with that block's geometry —
no guessing glm-ocr's cross-column reading order. This module produces the
blocks and their line bboxes from the page image directly (Tesseract's
layout analysis collapses on the cursive material glm-ocr exists for).

Pipeline (a light XY-cut, the classic document-layout decomposition):

  1. Clean a binary ink mask: adaptive threshold, subtract printed ruled
     lines, drop too-tall blobs (binding curl, scanner edge, taped repairs).
  2. Vertical ink projection → column x-ranges. A run of near-empty columns
     wider than a gutter separates columns; any number of columns is found,
     and a single-column page yields one range.
  3. For multi-column pages, separate **full-width bands** (titles / section
     headers that cross the gutters) from the column body, so a spanning
     title is its own block rather than being sliced across columns.
  4. Within each single-column block, a smoothed horizontal projection +
     peak detection yields one bbox per text line (clean, because the block
     is a single column — the earlier full-width projection could not
     separate columns, and connected-component smearing merged adjacent
     lines / dropped isolated words).

Reading order falls out of the decomposition: bands top-to-bottom, columns
left-to-right within a band, lines top-to-bottom within a column.

Validated against single-column cursive (handwritten technical notes) and a
three-column printed page ("Canons of Ethics for Engineers": full-width
title over three body columns).

Tunables are exposed for a future GUI's "advanced" panel; operators don't
need to touch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class DetectParams:
    # Adaptive threshold
    block_size: int = 51
    c_subtract: int = 15

    # Rule-line subtraction (printed ruled paper / table borders)
    rule_h_frac: float = 0.30
    rule_v_frac: float = 0.30
    rule_dilate: int = 2

    # Tall-blob suppression (binding curl, scanner edge, taped repairs)
    max_blob_height_frac: float = 0.45

    # Solid-blob suppression (hole-punch marks, ink stamps, filled scanner
    # artifacts). A connected component bigger than (``solid_blob_min_frac`` ×
    # page height)² whose pixels fill more than ``solid_blob_fill`` of its bbox
    # is dropped — text strokes are thin and never fill their bbox that densely,
    # so this removes solid marks (which otherwise read as a spurious column)
    # without touching real text.
    solid_blob_min_frac: float = 0.008
    solid_blob_fill: float = 0.55

    # Column detection. The vertical ink projection is smoothed over
    # ``col_smooth_frac`` × page width, then a column is an x-run whose smoothed
    # ink stays above ``col_ink_thresh_frac`` × a robust column level (the 75th
    # percentile of inked columns — robust to a dense patch like equations or a
    # heavy signature, which a max-based bar would let carve real columns out;
    # the profile is bimodal, so the threshold sits in the valley between the
    # column level and the near-zero gutters). Columns split
    # only at background gaps wider than ``col_min_gutter_frac`` × page width;
    # ``col_min_width_frac`` drops thin noise strips (margin marks, binding
    # holes). The bias is deliberately toward a single column — over-splitting
    # cuts words mid-line and feeds half-columns to the OCR.
    col_smooth_frac: float = 0.004
    col_ink_thresh_frac: float = 0.30
    col_min_gutter_frac: float = 0.008
    col_min_width_frac: float = 0.04
    # Drop a candidate column carrying less than this fraction of the densest
    # column's ink — margin marks / scanner-edge lines form a low-ink strip
    # that would otherwise read as a skinny column.
    col_min_ink_frac: float = 0.18

    # Column detection runs over a vertical "core" band [core_top_frac,
    # core_bottom_frac] × page height, skipping the top margin (where a
    # full-width title spans and masks the gutters) and the bottom (footers,
    # page numbers). In the core, real column gutters are empty across nearly
    # every row, while incidental gaps (a centered equation, a short line) are
    # drowned out by the surrounding body — so the body's true column count
    # shows through. Blocks still span full height, so the title is transcribed
    # (split across whichever columns its words fall under).
    core_top_frac: float = 0.12
    core_bottom_frac: float = 0.97

    # Per-block line detection (smoothed horizontal projection + peaks)
    smooth_sigma_frac: float = 0.0018
    peak_min_prominence_frac: float = 0.08
    peak_min_distance_frac: float = 0.009
    width_height_frac: float = 0.5
    min_line_height: int = 12
    max_line_height_frac: float = 0.06
    line_pad: int = 3


@dataclass(frozen=True)
class LineBox:
    """A detected text-line bbox in image-pixel coordinates."""
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class TextBlock:
    """A single-column region of the page plus its line bboxes (page coords).

    The engine crops ``(x, y, w, h)`` from the page image, transcribes that
    crop through glm-ocr, and distributes the returned text across ``lines``
    (already in top-to-bottom reading order).
    """
    x: int
    y: int
    w: int
    h: int
    lines: tuple[LineBox, ...]


def _odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1


def _segments(active: np.ndarray, min_gap: int, min_len: int = 1) -> list[tuple[int, int]]:
    """Contiguous True runs of ``active``, merging runs separated by < ``min_gap``.

    Returns inclusive ``(start, end)`` index pairs of length >= ``min_len``.
    """
    idx = np.flatnonzero(active)
    if len(idx) == 0:
        return []
    out: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev - 1 >= min_gap:
            if prev - start + 1 >= min_len:
                out.append((start, prev))
            start = i
        prev = i
    if prev - start + 1 >= min_len:
        out.append((start, prev))
    return out


def _clean_mask(image: np.ndarray, params: DetectParams) -> np.ndarray:
    """Binary ink mask (ink = 255) with rules and too-tall blobs removed."""
    gray = image
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if gray.dtype != np.uint8:
        gray = gray.astype(np.uint8)

    h, w = gray.shape

    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, _odd(params.block_size), params.c_subtract,
    )

    rule_w = max(11, int(params.rule_h_frac * w))
    h_rules = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (rule_w, 1)), iterations=1,
    )
    rule_h_px = max(11, int(params.rule_v_frac * h))
    v_rules = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, rule_h_px)), iterations=1,
    )
    rules = cv2.bitwise_or(h_rules, v_rules)
    if params.rule_dilate > 0:
        k = params.rule_dilate * 2 + 1
        rules = cv2.dilate(
            rules, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)), iterations=1,
        )
    cleaned = cv2.subtract(binary, rules)

    max_blob_h = int(params.max_blob_height_frac * h)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned, connectivity=8)
    if num_labels > 1:
        bw = stats[1:, cv2.CC_STAT_WIDTH].astype(np.float64)
        bh = stats[1:, cv2.CC_STAT_HEIGHT].astype(np.float64)
        area = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
        fill = area / np.maximum(1.0, bw * bh)
        min_solid_area = (params.solid_blob_min_frac * h) ** 2
        too_tall = bh > max_blob_h
        solid = (fill > params.solid_blob_fill) & (area > min_solid_area)
        drop = np.where(too_tall | solid)[0] + 1
        if len(drop):
            cleaned = cleaned.copy()
            cleaned[np.isin(labels, drop)] = 0

    return cleaned


def _column_ranges(cleaned: np.ndarray, params: DetectParams) -> list[tuple[int, int]]:
    """Column x-ranges from the vertical ink projection (over full height).

    A spanning title raises the gutter floor only slightly (a few title rows
    vs. many body rows), so gutters remain clear minima. Thresholding relative
    to the median inked column (not the max) keeps a dense patch from raising
    the bar and carving real columns out; thin strips (margin marks) are
    dropped via ``col_min_width_frac``.
    """
    h, w = cleaned.shape
    col_ink = (cleaned > 0).sum(axis=0).astype(np.float64)
    if col_ink.max() <= 0:
        return []
    win = max(3, int(params.col_smooth_frac * w))
    smoothed = np.convolve(col_ink, np.ones(win) / win, mode="same")
    peak = float(smoothed.max())
    if peak <= 0:
        return []
    inked = smoothed[smoothed > 0.02 * peak]
    level = float(np.percentile(inked, 75)) if inked.size else peak
    active = smoothed > params.col_ink_thresh_frac * level
    min_gap = max(1, int(params.col_min_gutter_frac * w))
    min_w = max(1, int(params.col_min_width_frac * w))
    ranges = _segments(active, min_gap=min_gap, min_len=min_w)
    if len(ranges) <= 1:
        return ranges
    inks = [float(smoothed[a : b + 1].sum()) for (a, b) in ranges]
    keep = params.col_min_ink_frac * max(inks)
    return [r for r, ink in zip(ranges, inks) if ink >= keep]


def _lines_in_block(
    cleaned: np.ndarray, x0: int, y0: int, x1: int, y1: int,
    page_h: int, params: DetectParams,
) -> list[LineBox]:
    """One bbox per text line in a single-column region, via projection peaks.

    Operates on ``cleaned[y0:y1, x0:x1]``; returns page-coordinate boxes with
    x tightened to each line's inked extent.
    """
    block = cleaned[y0:y1, x0:x1]
    if block.size == 0:
        return []
    bh, bw = block.shape

    profile = (block > 0).sum(axis=1).astype(np.float32)
    sigma = max(1.0, params.smooth_sigma_frac * page_h)
    radius = int(round(sigma * 3))
    xs = np.arange(-radius, radius + 1, dtype=np.float32)
    g = np.exp(-(xs * xs) / (2 * sigma * sigma))
    g /= g.sum()
    sm = np.convolve(profile, g, mode="same")
    if sm.max() <= 0:
        return []

    peaks: list[int] = []
    n = len(sm)
    i = 1
    while i < n - 1:
        if sm[i] > sm[i - 1] and sm[i] >= sm[i + 1]:
            j = i
            while j < n - 1 and sm[j + 1] == sm[i]:
                j += 1
            if sm[j] > sm[min(j + 1, n - 1)]:
                peaks.append((i + j) // 2)
            i = j + 1
        else:
            i += 1

    prom = params.peak_min_prominence_frac * float(sm.max())
    peaks = [p for p in peaks if sm[p] >= prom]
    min_dist = max(1, int(params.peak_min_distance_frac * page_h))
    peaks.sort(key=lambda p: -sm[p])
    kept: list[int] = []
    for p in peaks:
        if all(abs(p - k) >= min_dist for k in kept):
            kept.append(p)
    kept.sort()

    min_h = params.min_line_height
    max_h = int(params.max_line_height_frac * page_h)
    pad = params.line_pad
    H, W = cleaned.shape

    boxes: list[LineBox] = []
    for p in kept:
        peak_h = float(sm[p])
        edge = params.width_height_frac * peak_h
        a = p
        while a > 0 and sm[a - 1] >= edge:
            a -= 1
        b = p
        while b < n - 1 and sm[b + 1] >= edge:
            b += 1
        if (b - a + 1) < min_h or (b - a + 1) > max_h:
            continue
        band = block[a:b + 1, :]
        col_has_ink = (band > 0).any(axis=0)
        if not col_has_ink.any():
            continue
        cols = np.flatnonzero(col_has_ink)
        bx0, bx1 = int(cols[0]), int(cols[-1])
        gx0 = max(0, x0 + bx0 - pad)
        gx1 = min(W - 1, x0 + bx1 + pad)
        gy0 = max(0, y0 + a - pad)
        gy1 = min(H - 1, y0 + b + pad)
        boxes.append(LineBox(gx0, gy0, gx1 - gx0 + 1, gy1 - gy0 + 1))
    return boxes


def _make_block(
    cleaned: np.ndarray, x0: int, x1: int, params: DetectParams,
) -> TextBlock | None:
    """Build a full-height column block (x0..x1 inclusive), or None if empty.

    The block spans the column's inked row extent so its crop is a tall, narrow
    strip — the shape glm-ocr transcribes well, unlike a wide/short single-line
    crop. Anything that visually spans the gutters (a page title) is simply
    split across the columns it overlaps; each fragment lands in the column
    whose x-range covers it, which is still correct for word highlighting.
    """
    H, W = cleaned.shape
    col = cleaned[:, x0 : x1 + 1]
    rows = np.flatnonzero((col > 0).any(axis=1))
    if len(rows) == 0:
        return None
    y0, y1 = int(rows[0]), int(rows[-1])
    lines = _lines_in_block(cleaned, x0, y0, x1 + 1, y1 + 1, H, params)
    if not lines:
        return None
    return TextBlock(x0, y0, x1 - x0 + 1, y1 - y0 + 1, tuple(lines))


def detect_blocks(image: np.ndarray, params: DetectParams | None = None) -> list[TextBlock]:
    """Decompose a 2-D grayscale page into full-height single-column blocks.

    One block per detected column, left to right (a single-column page is one
    block); each carries its line bboxes top-to-bottom. The engine transcribes
    each block's crop independently, so its text pairs with its own geometry
    without guessing glm-ocr's cross-column reading order. Reading order is
    therefore column-major, matching how the per-block crops are transcribed.
    """
    params = params or DetectParams()
    cleaned = _clean_mask(image, params)
    h, w = cleaned.shape

    # Detect columns over the vertical core band so a full-width title (top) or
    # footer/page number (bottom) doesn't mask the body's gutters. Blocks span
    # full height, so the title is still transcribed — split across whichever
    # columns its words fall under, which is correct for word highlighting.
    cy0 = int(params.core_top_frac * h)
    cy1 = max(cy0 + 1, int(params.core_bottom_frac * h))
    col_ranges = _column_ranges(cleaned[cy0:cy1, :], params) or [(0, w - 1)]

    blocks = [_make_block(cleaned, x0, x1, params) for (x0, x1) in col_ranges]
    return [b for b in blocks if b is not None]


def detect_lines(image: np.ndarray, params: DetectParams | None = None) -> list[LineBox]:
    """All line bboxes on the page in reading order (blocks flattened).

    Convenience for callers/tools that only want the line geometry; the engine
    uses ``detect_blocks`` so it can transcribe per block.
    """
    return [ln for block in detect_blocks(image, params) for ln in block.lines]


def ink_density(image: np.ndarray, box: LineBox, params: DetectParams | None = None) -> float:
    """Fraction of the bbox's area that is ink, on the cleaned binary mask."""
    params = params or DetectParams()
    cleaned = _clean_mask(image, params)
    crop = cleaned[box.y : box.y2 + 1, box.x : box.x2 + 1]
    if crop.size == 0:
        return 0.0
    return float((crop > 0).mean())


def overlay_boxes(
    image: np.ndarray,
    boxes: Sequence[LineBox],
    blocks: Sequence[TextBlock] = (),
) -> np.ndarray:
    """BGR image with green numbered line bboxes (and optional blue block rects).

    Convenience for debugging / future GUI preview. Not used in production.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    for blk in blocks:
        cv2.rectangle(rgb, (blk.x, blk.y), (blk.x + blk.w, blk.y + blk.h), (255, 120, 0), 4)
    for i, b in enumerate(boxes, start=1):
        cv2.rectangle(rgb, (b.x, b.y), (b.x2, b.y2), (0, 200, 0), 2)
        label = str(i)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        ly = max(th + 2, b.y - 4)
        cv2.rectangle(rgb, (b.x, ly - th - 4), (b.x + tw + 4, ly), (0, 200, 0), -1)
        cv2.putText(rgb, label, (b.x + 2, ly - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
    return rgb
