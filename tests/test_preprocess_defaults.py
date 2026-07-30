"""Binarization defaults OFF, and a stale saved GUI preference is retired once.

Until 2026-07-30 the pipeline handed Tesseract an adaptive-thresholded image.
Measuring three unrelated collections showed that costs accuracy everywhere,
because Tesseract 5's LSTM engine thresholds internally and our adaptive window
turns paper grain into speckle it then reads as punctuation:

    ZeffPapers/MSP00074  mean WC 0.828 -> 0.909   review pages 28 -> 6
    MSA00005 (11 pages)          0.583 -> 0.781                 7 -> 3
    MSA00005 (2 pages)           0.453 -> 0.945                 2 -> 0
    MSA00018 (82 pages)          0.803 -> 0.919                25 -> 6

These tests pin the resulting default so it cannot drift back silently, and
cover the GUI migration — saved settings normally win over dataclass defaults,
so without it nobody who had already run the GUI would ever see the change.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_preprocess_defaults
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from src.alto_ocr.gui import _BINARIZE_MIGRATION_KEY, _migrate_settings  # noqa: E402
from src.alto_ocr.main import _build_preprocess_config  # noqa: E402
from src.alto_ocr.preprocess import PreprocessConfig  # noqa: E402


class TestBinarizeDefault(unittest.TestCase):
    def test_config_default_is_off(self):
        self.assertFalse(PreprocessConfig().do_binarize)

    def test_other_value_stages_still_default_on(self):
        """Only binarize changed — denoise/CLAHE were not part of the finding."""
        cfg = PreprocessConfig()
        self.assertTrue(cfg.do_denoise)
        self.assertTrue(cfg.do_clahe)

    def test_cli_default_leaves_binarize_off(self):
        cfg = _build_preprocess_config(300, False, False, False, False, False)
        self.assertFalse(cfg.do_binarize)

    def test_cli_binarize_flag_opts_in(self):
        cfg = _build_preprocess_config(300, False, False, False, True, False)
        self.assertTrue(cfg.do_binarize)


class TestGrayscaleReachesOCR(unittest.TestCase):
    """With binarize off the pipeline must hand OCR continuous grayscale, not a
    two-valued image — that is the whole point of the default."""

    def test_default_output_is_not_two_valued(self):
        from src.alto_ocr.preprocess import preprocess
        import tempfile

        from PIL import Image

        # A gradient with text-like dark marks: binarizing collapses it to {0,255}.
        arr = np.tile(np.linspace(90, 210, 400, dtype=np.uint8), (300, 1))
        arr[100:140, 50:350] = 20
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "page.tif"
            Image.fromarray(arr).save(src, dpi=(300, 300))

            out = preprocess(src, PreprocessConfig())
            self.assertGreater(len(np.unique(out.image)), 2,
                               "default pipeline handed OCR a binarized image")
            self.assertNotIn("binarize", out.stages_applied)

            opted_in = preprocess(src, PreprocessConfig(do_binarize=True))
            self.assertEqual(sorted(np.unique(opted_in.image).tolist()), [0, 255])
            self.assertIn("binarize", opted_in.stages_applied)


class TestGuiSettingsMigration(unittest.TestCase):
    def test_stale_true_preference_is_dropped_once(self):
        s = _migrate_settings({"do_binarize": True, "min_dpi": 300})
        self.assertNotIn("do_binarize", s)
        self.assertTrue(s[_BINARIZE_MIGRATION_KEY])
        self.assertEqual(s["min_dpi"], 300, "migration must not disturb other settings")

    def test_deliberate_reopt_in_survives_second_run(self):
        """Once migrated, a user who re-checks the box keeps it."""
        s = _migrate_settings({"do_binarize": True})
        s["do_binarize"] = True  # user re-checks it in the GUI, settings saved
        again = _migrate_settings(s)
        self.assertTrue(again["do_binarize"])

    def test_false_preference_is_left_alone(self):
        s = _migrate_settings({"do_binarize": False})
        self.assertFalse(s["do_binarize"])

    def test_empty_settings_are_safe(self):
        s = _migrate_settings({})
        self.assertTrue(s[_BINARIZE_MIGRATION_KEY])
        self.assertNotIn("do_binarize", s)


if __name__ == "__main__":
    unittest.main()
