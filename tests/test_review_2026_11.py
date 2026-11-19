"""Regression tests for the five defects found in the 2026-11-11 code review.

1. Tesseract children run with OpenMP held to one thread: concurrent
   multi-threaded Tesseract processes spin against each other (4 concurrent
   pages took 117 s where one takes 2.6 s).
2. OCR accepts scans past Pillow's default decompression-bomb limit, as the
   review UIs already did — the same page must not fail from the CLI and
   work from the GUI.
3. Every image loader applies the EXIF/TIFF orientation tag, so the review
   UIs and QA overlays show a page in the frame OCR read it in.
4. A multi-page TIF, of which only the first frame is OCR'd, is reported.
5. A placeholder DPI tag (72 on a full-resolution JPEG) cannot trigger a
   several-hundred-megapixel upscale.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_review_2026_11
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr import ocr, preprocess  # noqa: E402
from src.alto_ocr.alto_parse import load_image_and_scale, upright  # noqa: E402
from src.alto_ocr.config import PreprocessConfig  # noqa: E402
from src.alto_ocr.webreview import imaging  # noqa: E402

_ORIENTATION = 0x0112
_PLAIN = PreprocessConfig(do_denoise=False, do_clahe=False,
                          do_deskew=False, do_border_crop=False)


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


def _rotated_jpeg(path: Path, size: tuple[int, int], orientation: int) -> None:
    """A JPEG whose stored pixels are ``size`` and whose EXIF says to rotate.
    The top-left stored quadrant is black so the rotation is observable."""
    im = Image.new("L", size, 255)
    im.paste(0, (0, 0, size[0] // 2, size[1] // 2))
    exif = im.getexif()
    exif[_ORIENTATION] = orientation
    im.save(path, exif=exif, dpi=(300, 300), quality=95)


class TesseractThreadCapTests(_TmpCase):
    def _env_seen(self) -> dict:
        engine = ocr.TesseractEngine(binary=self.root / "tesseract.exe")
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(ocr.subprocess, "run", return_value=done) as run:
            with self.assertRaises(RuntimeError):
                engine.ocr(np.full((20, 20), 255, np.uint8),
                           self.root / "out" / "p1", dpi=300, page_index=1)
        return run.call_args.kwargs["env"]

    def test_openmp_is_held_to_one_thread(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("OMP_THREAD_LIMIT", None)
            env = self._env_seen()
        self.assertEqual(env["OMP_THREAD_LIMIT"], "1")
        self.assertIn("PATH", env, "the rest of the environment is inherited")

    def test_an_explicit_limit_wins(self) -> None:
        with mock.patch.dict(os.environ, {"OMP_THREAD_LIMIT": "4"}):
            env = self._env_seen()
        self.assertEqual(env["OMP_THREAD_LIMIT"], "4")


class LargeScanLimitTests(unittest.TestCase):
    def test_ocr_raises_pillows_limit_like_the_review_uis(self) -> None:
        # Importing preprocess alone must be enough: a batch worker or the
        # Goobi bridge never imports the Review tab.
        self.assertGreaterEqual(Image.MAX_IMAGE_PIXELS, 1_000_000_000)
        self.assertGreaterEqual(preprocess._MAX_IMAGE_PIXELS, 1_000_000_000)


class OrientationFrameTests(_TmpCase):
    """One page, one frame: OCR, both review UIs and the overlays agree."""

    def setUp(self) -> None:
        super().setUp()
        self.jpg = self.root / "rot.jpg"
        _rotated_jpeg(self.jpg, (80, 50), 6)  # stored landscape, shown portrait

    def test_ocr_reads_the_upright_page(self) -> None:
        img = preprocess.preprocess(self.jpg, _PLAIN).image
        self.assertEqual(img.shape[::-1], (50, 80))

    def test_review_loaders_use_the_same_frame(self) -> None:
        ocr_img = preprocess.preprocess(self.jpg, _PLAIN).image
        ocr_size = ocr_img.shape[::-1]

        tk = upright(Image.open(self.jpg)).convert("L")  # as review._open_page
        self.assertEqual(tk.size, ocr_size)
        # Same pixels, not just the same shape: the dark quadrant lands in
        # the same corner (orientation 6 moves top-left to top-right).
        self.assertLess(tk.getpixel((45, 5)), 60)
        self.assertLess(int(ocr_img[5, 45]), 60)
        self.assertGreater(tk.getpixel((5, 5)), 200)

        wc = imaging.working_copy(self.jpg, self.root / "cache", max_px=4000)
        self.assertEqual(wc.source_size, ocr_size)
        self.assertEqual(wc.working_size, ocr_size)

    def test_overlay_loader_uses_the_same_frame(self) -> None:
        alto = self.root / "p.xml"
        alto.write_text(
            '<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#"><Layout>'
            '<Page WIDTH="50" HEIGHT="80"/></Layout></alto>', encoding="utf-8")
        im, _root, sx, sy = load_image_and_scale(alto, self.jpg)
        self.assertEqual(im.size, (50, 80))
        self.assertEqual((sx, sy), (1.0, 1.0))

    def test_untagged_image_is_returned_as_is(self) -> None:
        plain = self.root / "plain.tif"
        Image.new("L", (30, 20), 255).save(plain)
        with Image.open(plain) as im:
            self.assertIs(upright(im), im, "no copy of a scan that needs no turning")

    def test_render_cache_key_changed_with_the_pixels(self) -> None:
        # Working copies cached before orientation was applied must not be
        # served under word boxes that are now in the upright frame.
        self.assertGreaterEqual(imaging._RENDER_VERSION, 2)
        with mock.patch.object(imaging, "_RENDER_VERSION", 1):
            old = imaging._cache_key(self.jpg, 4000)
        self.assertNotEqual(old, imaging._cache_key(self.jpg, 4000))


class MultiFrameTifTests(_TmpCase):
    def test_dropped_frames_are_reported(self) -> None:
        tif = self.root / "minutes.tif"
        frames = [Image.new("L", (40, 60), 255) for _ in range(3)]
        frames[0].save(tif, save_all=True, append_images=frames[1:], dpi=(300, 300))
        with self.assertLogs(preprocess.logger, level="WARNING") as logs:
            img = preprocess.preprocess(tif, _PLAIN).image
        self.assertEqual(img.shape, (60, 40))
        self.assertIn("minutes.tif holds 3 frames", logs.output[0])
        self.assertIn("other 2", logs.output[0])

    def test_thumbnail_frame_is_quiet(self) -> None:
        # Capture One embeds a reduced-size preview as frame 2.
        tif = self.root / "scan.tif"
        page = Image.new("L", (400, 600), 255)
        page.save(tif, save_all=True,
                  append_images=[Image.new("L", (40, 60), 255)], dpi=(300, 300))
        with self.assertNoLogs(preprocess.logger, level="WARNING"):
            img = preprocess.preprocess(tif, _PLAIN).image
        self.assertEqual(img.shape, (600, 400))

    def test_single_frame_is_quiet(self) -> None:
        tif = self.root / "p1.tif"
        Image.new("L", (40, 60), 255).save(tif, dpi=(300, 300))
        with self.assertNoLogs(preprocess.logger, level="WARNING"):
            preprocess.preprocess(tif, _PLAIN)


class UpscaleCapTests(_TmpCase):
    def test_placeholder_dpi_does_not_upscale_a_full_resolution_image(self) -> None:
        img = np.zeros((6000, 4000), np.uint8)  # 24 MP tagged 72 dpi -> 417 MP
        with self.assertLogs(preprocess.logger, level="WARNING") as logs:
            out, eff = preprocess._upscale_for_dpi(img, 72, 300)
        self.assertIs(out, img)
        self.assertEqual(eff, 300)
        self.assertIn("placeholder", logs.output[0])

    def test_genuine_low_dpi_scan_still_upscales(self) -> None:
        img = np.zeros((1650, 1275), np.uint8)  # letter page at 150 dpi
        out, eff = preprocess._upscale_for_dpi(img, 150, 300)
        self.assertEqual(out.shape, (3300, 2550))
        self.assertEqual(eff, 300)

    def test_cap_boundary(self) -> None:
        with mock.patch.object(preprocess, "_MAX_UPSCALED_PIXELS", 40 * 60):
            img = np.zeros((30, 20), np.uint8)
            out, _ = preprocess._upscale_for_dpi(img, 150, 300)  # exactly at the cap
            self.assertEqual(out.shape, (60, 40))
            with self.assertLogs(preprocess.logger, level="WARNING"):
                out, _ = preprocess._upscale_for_dpi(np.zeros((31, 20), np.uint8), 150, 300)
            self.assertEqual(out.shape, (31, 20))

    def test_stage_label_only_when_resized(self) -> None:
        jpg = self.root / "cam.jpg"
        Image.new("L", (40, 60), 255).save(jpg, dpi=(72, 72))
        with mock.patch.object(preprocess, "_MAX_UPSCALED_PIXELS", 10):
            with self.assertLogs(preprocess.logger, level="WARNING"):
                skipped = preprocess.preprocess(jpg, _PLAIN)
        self.assertEqual(skipped.image.shape, (60, 40))
        self.assertFalse(any(s.startswith("upscale") for s in skipped.stages_applied))
        resized = preprocess.preprocess(jpg, _PLAIN)
        self.assertTrue(any(s.startswith("upscale") for s in resized.stages_applied))


if __name__ == "__main__":
    unittest.main()
