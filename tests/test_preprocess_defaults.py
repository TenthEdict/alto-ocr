"""Lock the preprocessing defaults — especially binarize OFF (v0.8.8).

Until 2026-07-30 the pipeline handed Tesseract an adaptive-thresholded
image. Measuring three unrelated collections showed that costs accuracy
everywhere, because Tesseract 5's LSTM engine thresholds internally and our
adaptive window turns paper grain into speckle it then reads as punctuation:

    ZeffPapers/MSP00074  mean WC 0.828 -> 0.909   review pages 28 -> 6
    MSA00005 (11 pages)          0.583 -> 0.781                 7 -> 3
    MSA00005 (2 pages)           0.453 -> 0.945                 2 -> 0
    MSA00018 (82 pages)          0.803 -> 0.919                25 -> 6

These tests pin the decided defaults so a refactor can't silently re-enable
binarization, verify the default pipeline really hands OCR grayscale, and
cover the CLI mapping plus the GUI's one-shot settings migration (saved GUI
settings outrank dataclass defaults, so a stale pre-0.8.8
``do_binarize: true`` must be retired once — under either marker name the
two 0.8.8-era builds wrote).

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_preprocess_defaults
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr.preprocess import PreprocessConfig, preprocess  # noqa: E402

# The GUI module needs tkinter at import time; the migration function is a
# pure dict transform, but skip its tests where Tk isn't installed (CI).
try:
    from src.alto_ocr.gui import (
        _BINARIZE_MIGRATION_KEY,
        _BINARIZE_MIGRATION_KEY_2026_07_30,
        _migrate_settings,
    )
except ImportError:  # pragma: no cover - environment-dependent
    _migrate_settings = None


class DefaultTests(unittest.TestCase):
    def test_binarize_off_by_default(self) -> None:
        # v0.8.8 decision — don't re-enable without re-measuring (see
        # CLAUDE.md "Image preprocessing" and the MSA00018 pilot notes).
        self.assertFalse(PreprocessConfig().do_binarize)

    def test_other_stage_defaults_unchanged(self) -> None:
        cfg = PreprocessConfig()
        self.assertTrue(cfg.do_deskew)   # engines force this off themselves
        self.assertTrue(cfg.do_denoise)
        self.assertTrue(cfg.do_clahe)
        self.assertTrue(cfg.do_border_crop)
        self.assertEqual(cfg.min_dpi, 300)

    def test_cli_builder_maps_binarize_directly(self) -> None:
        from src.alto_ocr.main import _build_preprocess_config
        on = _build_preprocess_config(300, False, False, False, True, False)
        off = _build_preprocess_config(300, False, False, False, False, False)
        self.assertTrue(on.do_binarize)
        self.assertFalse(off.do_binarize)


class GrayscaleReachesOCRTests(unittest.TestCase):
    """With binarize off the pipeline must hand OCR continuous grayscale, not
    a two-valued image — that is the whole point of the default."""

    def test_default_output_is_not_two_valued(self) -> None:
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


@unittest.skipIf(_migrate_settings is None, "tkinter not available")
class SettingsMigrationTests(unittest.TestCase):
    def test_stale_binarize_preference_retired_once(self) -> None:
        # A pre-0.8.8 settings file: do_binarize True is just the old
        # default echoed back by a save, not a user choice — drop it.
        s = _migrate_settings({"do_binarize": True, "psm": 3})
        self.assertNotIn("do_binarize", s)
        self.assertTrue(s[_BINARIZE_MIGRATION_KEY])
        self.assertEqual(s["psm"], 3)  # everything else untouched

    def test_post_migration_choice_is_kept(self) -> None:
        # After the marker exists, a saved do_binarize was chosen by the
        # user under the new default — keep it.
        s = _migrate_settings(
            {"do_binarize": True, _BINARIZE_MIGRATION_KEY: True})
        self.assertTrue(s["do_binarize"])

    def test_reopt_in_survives_second_run(self) -> None:
        s = _migrate_settings({"do_binarize": True})
        s["do_binarize"] = True  # user re-checks it in the GUI, settings saved
        again = _migrate_settings(s)
        self.assertTrue(again["do_binarize"])

    def test_false_preference_is_left_alone(self) -> None:
        # A pre-migration False was a deliberate OFF under the old ON
        # default — keep it (it matches the new default anyway).
        s = _migrate_settings({"do_binarize": False})
        self.assertFalse(s["do_binarize"])

    def test_legacy_marker_prevents_remigration(self) -> None:
        # The 2026-07-30 branch build migrated under a different marker
        # name; a do_binarize present alongside it is a deliberate
        # re-opt-in and must not be cleared by a second migration.
        s = _migrate_settings(
            {"do_binarize": True, _BINARIZE_MIGRATION_KEY_2026_07_30: True})
        self.assertTrue(s["do_binarize"])

    def test_fresh_settings_gain_marker(self) -> None:
        s = _migrate_settings({})
        self.assertTrue(s[_BINARIZE_MIGRATION_KEY])
        self.assertNotIn("do_binarize", s)


if __name__ == "__main__":
    unittest.main()
