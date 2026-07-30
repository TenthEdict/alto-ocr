"""Coordinate-frame integrity: engines must not run frame-changing preprocess.

ALTO box coordinates are emitted in the preprocessed image's frame, and every
consumer — the public archive's hit boxes, the Review tab, the QA overlays —
maps them onto the ORIGINAL image by the image-px / Page-dims ratio. Only the
uniform DPI upscale cancels in that mapping; deskew (rotation) and border-crop
(translation + resize) do not, so both engines force them off in
``adjust_preprocess``. Measured on ZeffPapers + MSA00005 (2026-07-30, 119
pages): deskew fired on 36% of pages (median 1.1 deg, max 4.4 deg), putting
hit boxes a full line off their words near page edges — and disabling it also
*raised* mean word confidence (+0.03), so the stopgap costs nothing. See the
"Coordinate-frame integrity" section in CLAUDE.md.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_coordinate_frame
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr.ocr import GLMOCREngine, TesseractEngine  # noqa: E402
from src.alto_ocr.preprocess import PreprocessConfig, preprocess  # noqa: E402


def _tesseract_engine() -> TesseractEngine:
    # No binary needed: adjust_preprocess never touches it.
    return TesseractEngine(binary=Path("tesseract-binary-not-needed"))


class AdjustPreprocessTests(unittest.TestCase):
    def test_frame_changing_stages_disabled(self) -> None:
        cfg = _tesseract_engine().adjust_preprocess(PreprocessConfig())
        self.assertFalse(cfg.do_deskew)
        self.assertFalse(cfg.do_border_crop)

    def test_user_settings_preserved(self) -> None:
        base = PreprocessConfig(min_dpi=400, do_binarize=False, do_clahe=False)
        cfg = _tesseract_engine().adjust_preprocess(base)
        self.assertEqual(cfg.min_dpi, 400)
        self.assertFalse(cfg.do_binarize)
        self.assertFalse(cfg.do_clahe)
        self.assertTrue(cfg.do_denoise, "untouched stages keep their setting")

    def test_both_engines_agree_on_frame_policy(self) -> None:
        # Drift guard: any engine that emits ALTO geometry must keep the
        # frame identical to the original image (uniform upscale excepted).
        for engine in (_tesseract_engine(), GLMOCREngine()):
            cfg = engine.adjust_preprocess(PreprocessConfig())
            self.assertFalse(cfg.do_deskew, engine.name)
            self.assertFalse(cfg.do_border_crop, engine.name)


class FramePreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _save(self, arr: np.ndarray, name: str) -> Path:
        p = self.dir / name
        Image.fromarray(arr).save(p, dpi=(300, 300))
        return p

    def test_bordered_page_keeps_full_frame(self) -> None:
        # White page with text-ish bars and a solid black scanner border.
        img = np.full((1000, 800), 255, np.uint8)
        for y in range(200, 760, 80):
            img[y:y + 16, 140:660] = 0
        img[:70, :] = 0
        img[-70:, :] = 0
        img[:, :70] = 0
        img[:, -70:] = 0
        src = self._save(img, "bordered.tif")

        # Fixture validity: with frame-changers ON, the crop really fires
        # (value-changing stages off so the solid border reaches
        # _crop_borders intact).
        base = PreprocessConfig(do_binarize=False, do_clahe=False, do_denoise=False)
        r_on = preprocess(src, base)
        self.assertLess(r_on.image.shape[0], 1000, "fixture: crop must fire")

        r_off = preprocess(src, _tesseract_engine().adjust_preprocess(base))
        self.assertEqual(r_off.image.shape, (1000, 800),
                         "Tesseract path must keep the original frame")
        joined = " ".join(r_off.stages_applied)
        self.assertNotIn("crop", joined)
        self.assertNotIn("deskew", joined)

    def test_skewed_page_not_rotated(self) -> None:
        img = np.full((1000, 800), 255, np.uint8)
        for y in range(150, 850, 60):
            img[y:y + 14, 100:700] = 0
        M = cv2.getRotationMatrix2D((400, 500), 3.0, 1.0)
        img = cv2.warpAffine(img, M, (800, 1000), borderValue=255)
        src = self._save(img, "skewed.tif")

        r_on = preprocess(src, PreprocessConfig())
        self.assertTrue(
            any(s.startswith("deskew") for s in r_on.stages_applied),
            "fixture: deskew must fire on the skewed page",
        )
        r_off = preprocess(src, _tesseract_engine().adjust_preprocess(PreprocessConfig()))
        self.assertFalse(
            any(s.startswith("deskew") for s in r_off.stages_applied),
            "Tesseract path must never rotate the frame",
        )


if __name__ == "__main__":
    unittest.main()
