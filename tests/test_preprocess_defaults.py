"""Lock the preprocessing defaults — especially binarize OFF (v0.8.8).

The MSA00018 pilot (2026-07-30) measured that adaptive thresholding hurt
Tesseract 5 on every fixture and pilot collection (mean WC up 0.08–0.49
with it off; faint strokes survive, paper grain stops reading as
punctuation). These tests pin the decided defaults so a refactor can't
silently re-enable binarization, and cover the CLI mapping plus the GUI's
one-shot settings migration (saved GUI settings outrank dataclass
defaults, so a stale pre-0.8.8 ``do_binarize: true`` must be retired once).

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_preprocess_defaults
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.preprocess import PreprocessConfig  # noqa: E402

# The GUI module needs tkinter at import time; the migration function is a
# pure dict transform, but skip its tests where Tk isn't installed (CI).
try:
    from src.alto_ocr.gui import _migrate_settings
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


@unittest.skipIf(_migrate_settings is None, "tkinter not available")
class SettingsMigrationTests(unittest.TestCase):
    def test_stale_binarize_preference_retired_once(self) -> None:
        # A pre-0.8.8 settings file: do_binarize True is just the old
        # default echoed back by a save, not a user choice — drop it.
        s = _migrate_settings({"do_binarize": True, "psm": 3})
        self.assertNotIn("do_binarize", s)
        self.assertTrue(s["_migrated_binarize_default_off"])
        self.assertEqual(s["psm"], 3)  # everything else untouched

    def test_post_migration_choice_is_kept(self) -> None:
        # After the marker exists, a saved do_binarize was chosen by the
        # user under the new default — keep it.
        s = _migrate_settings(
            {"do_binarize": True, "_migrated_binarize_default_off": True})
        self.assertTrue(s["do_binarize"])

    def test_fresh_settings_gain_marker(self) -> None:
        s = _migrate_settings({})
        self.assertTrue(s["_migrated_binarize_default_off"])


if __name__ == "__main__":
    unittest.main()
