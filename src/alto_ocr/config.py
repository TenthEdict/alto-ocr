"""Run-configuration dataclasses, kept dependency-free (stdlib only).

The GUI needs these defaults to build its window, but their historic homes
(``preprocess``, ``ocr``) import OpenCV and numpy at module
level: ~190 MB of native libraries that made the window wait for them on
every launch, though the Review tab never touches them and the Run tab only
does once a run starts. Living here, they load in milliseconds; the old
modules re-export them, so ``preprocess.PreprocessConfig`` and friends still
work everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PreprocessConfig:
    """Per-stage toggles + tunable params. Construct one per run."""

    min_dpi: int = 300
    do_deskew: bool = True
    do_denoise: bool = True
    do_clahe: bool = True
    # OFF by default since v0.8.8: Tesseract 5's LSTM engine does its own
    # thresholding and reads grayscale better than anything we hand it
    # pre-binarized. Measured 2026-07-30 across three unrelated collections
    # (mean WC, adaptive-threshold ON -> OFF): ZeffPapers/MSP00074
    # 0.828 -> 0.909, MSA00005 0.583 -> 0.781 and 0.453 -> 0.945, MSA00018
    # 0.803 -> 0.919. Pages needing review fell 28 -> 6, 9 -> 3 and 25 -> 6
    # respectively. The mechanism is speckle: on toned or grainy paper the
    # adaptive window flips background grain to ink, which Tesseract emits as
    # stray punctuation ('.', '|', ':') and glues onto real words ('the.',
    # 'of.'). Token analysis confirmed the words that disappear when it is
    # off are those artifacts, while real words are gained. Turn back on per
    # collection with --binarize if one ever benefits; locked by
    # tests/test_preprocess_defaults.py — don't re-enable without re-measuring.
    do_binarize: bool = False
    do_border_crop: bool = True

    deskew_min_angle_deg: float = 0.2  # below this, skip rotation
    deskew_max_angle_deg: float = 15.0  # above this, treat as bad estimate

    bilateral_d: int = 7
    bilateral_sigma_color: int = 50
    bilateral_sigma_space: int = 50

    clahe_clip: float = 2.0
    clahe_tile: int = 8

    adaptive_block_size: int = 31  # must be odd
    adaptive_C: int = 15

    border_crop_threshold: int = 30  # 0–255; pixels darker → considered border


@dataclass(frozen=True)
class OCROptions:
    language: str = "eng"
    psm: int = 3  # fully automatic page segmentation
    oem: int = 3  # default LSTM engine
    # Per-page wall-clock limit. A pathological page (gigapixel scan, a
    # layout analysis that never converges) would otherwise hang the batch
    # with nothing for per-page isolation to catch; on timeout the page
    # gets a failure stub and the run moves on.
    timeout_s: int = 600
