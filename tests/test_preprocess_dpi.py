"""Regression tests for the DPI-tag sanity floor in preprocessing.

An implausibly small DPI tag (OpenCV-written TIFs default XResolution to 1;
some scanners write 0) must be treated as *unknown*, not trusted: trusting
``dpi=1`` makes the upscale factor ``min_dpi/1`` = 300x — a gigapixel Lanczos
resize that spins every core for hours and stalls the batch without raising
anything the per-page failure isolation could catch. Found live 2026-07-30.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_preprocess_dpi
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.preprocess import _MIN_PLAUSIBLE_DPI, _upscale_for_dpi  # noqa: E402


class UpscaleForDpiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.img = np.full((20, 30), 255, np.uint8)

    def test_garbage_dpi_tag_is_ignored_not_trusted(self) -> None:
        for tag in (0, 1, 2, _MIN_PLAUSIBLE_DPI - 1):
            out, eff = _upscale_for_dpi(self.img, tag, 300)
            self.assertEqual(out.shape, self.img.shape,
                             f"dpi tag {tag} must not trigger an upscale")
            self.assertEqual(eff, 300)

    def test_unknown_dpi_passes_through(self) -> None:
        out, eff = _upscale_for_dpi(self.img, None, 300)
        self.assertEqual(out.shape, self.img.shape)
        self.assertEqual(eff, 300)

    def test_plausible_low_dpi_still_upscales(self) -> None:
        out, eff = _upscale_for_dpi(self.img, 150, 300)
        self.assertEqual(out.shape, (40, 60))  # 2x
        self.assertEqual(eff, 300)

    def test_floor_boundary_upscales(self) -> None:
        out, eff = _upscale_for_dpi(self.img, _MIN_PLAUSIBLE_DPI, 300)
        scale = 300 / _MIN_PLAUSIBLE_DPI
        self.assertEqual(out.shape, (round(20 * scale), round(30 * scale)))
        self.assertEqual(eff, 300)

    def test_sufficient_dpi_untouched(self) -> None:
        out, eff = _upscale_for_dpi(self.img, 400, 300)
        self.assertEqual(out.shape, self.img.shape)
        self.assertEqual(eff, 400)


if __name__ == "__main__":
    unittest.main()
