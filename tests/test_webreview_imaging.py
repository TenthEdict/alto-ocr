"""Tests for the review server's page rendering (webreview/imaging.py).

Covers: the one-decode-per-page working copy and its cache, cache
invalidation when a scan is replaced in place, the working-copy cap, crop
geometry expressed in **source** pixels (the frame the API speaks), padding
and clamping at the page edge, degenerate boxes not raising, and LRU
pruning.

Run from the repo root:

    python -m unittest tests.test_webreview_imaging
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from src.alto_ocr.webreview import imaging  # noqa: E402


def _tif(path: Path, size: tuple[int, int], colour: str = "white") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, format="TIFF")


class WorkingCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.scan = self.root / "scans" / "p1.tif"
        _tif(self.scan, (1200, 1600))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_records_both_frames(self) -> None:
        wc = imaging.working_copy(self.scan, self.cache, max_px=600)
        self.assertEqual(wc.source_size, (1200, 1600))
        self.assertEqual(wc.working_size, (450, 600))
        self.assertAlmostEqual(wc.scale, 450 / 1200, places=6)
        self.assertTrue(wc.path.is_file())

    def test_small_scan_is_not_upscaled(self) -> None:
        wc = imaging.working_copy(self.scan, self.cache, max_px=4000)
        self.assertEqual(wc.working_size, (1200, 1600))

    def test_second_call_reuses_the_render(self) -> None:
        """One decode per page is the whole point: a corrector clicking
        through sixty words must not pay sixty TIF decodes."""
        first = imaging.working_copy(self.scan, self.cache, max_px=600)
        stamp = first.path.stat().st_mtime_ns
        second = imaging.working_copy(self.scan, self.cache, max_px=600)
        self.assertEqual(second.path, first.path)
        self.assertEqual(second.path.stat().st_mtime_ns, stamp)

    def test_replacing_the_scan_invalidates_the_render(self) -> None:
        """A rescan of a damaged page must not serve the old pixels under the
        new page's word boxes."""
        first = imaging.working_copy(self.scan, self.cache, max_px=600)
        _tif(self.scan, (900, 900), "black")
        second = imaging.working_copy(self.scan, self.cache, max_px=600)
        self.assertNotEqual(second.path, first.path)
        self.assertEqual(second.source_size, (900, 900))

    def test_a_different_cap_is_a_different_render(self) -> None:
        a = imaging.working_copy(self.scan, self.cache, max_px=600)
        b = imaging.working_copy(self.scan, self.cache, max_px=1000)
        self.assertNotEqual(a.path, b.path)

    def test_bitonal_and_cmyk_scans_render(self) -> None:
        """The preservation masters include 1-bit and CMYK pages, neither of
        which JPEG can encode without conversion."""
        for mode in ("1", "L", "CMYK", "I;16"):
            with self.subTest(mode=mode):
                path = self.root / "scans" / f"{mode.replace(';', '_')}.tif"
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new(mode, (300, 400)).save(path, format="TIFF")
                wc = imaging.working_copy(path, self.cache, max_px=200)
                self.assertTrue(wc.path.is_file())

    def test_multipage_tif_uses_the_first_frame(self) -> None:
        path = self.root / "scans" / "multi.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (400, 500), "white").save(
            path, format="TIFF", save_all=True,
            append_images=[Image.new("RGB", (80, 90), "black")])
        wc = imaging.working_copy(path, self.cache, max_px=4000)
        self.assertEqual(wc.source_size, (400, 500))


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.scan = self.root / "p1.tif"
        # A white page with a black band at y=400..500, x=200..600, so a crop
        # can be checked by where the ink lands rather than by coordinates.
        img = Image.new("RGB", (1000, 1200), "white")
        for x in range(200, 600):
            for y in range(400, 500):
                img.putpixel((x, y), (0, 0, 0))
        img.save(self.scan, format="TIFF")
        # Native-scale working copy (the cap is on the LONG edge, so 2000
        # leaves a 1000x1200 scan untouched): crop assertions below are then
        # exact source-pixel arithmetic.
        self.wc = imaging.working_copy(self.scan, self.cache, max_px=2000)
        # And a downscaled one, scale 0.5, to pin the source->working mapping.
        self.wc_small = imaging.working_copy(self.scan, self.cache, max_px=600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_page_render_honours_the_width(self) -> None:
        with Image.open(BytesIO(imaging.render_page(self.wc, width=500))) as im:
            self.assertEqual(im.width, 500)
            self.assertEqual(im.height, 600)

    def test_page_render_never_upscales(self) -> None:
        with Image.open(BytesIO(imaging.render_page(self.wc, width=5000))) as im:
            self.assertEqual(im.size, self.wc.working_size)

    def test_crop_box_is_in_source_pixels(self) -> None:
        """The frame the API speaks -- ``remediate.Word.box_px`` -- not the
        working copy's own scale."""
        data = imaging.render_crop(self.wc, (200, 400, 400, 100), pad=0, zoom=1.0)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.size, (400, 100))
            # Cropped exactly onto the ink band, so the middle is black.
            r, g, b = im.convert("RGB").getpixel((200, 50))
            self.assertLess(r + g + b, 120)

    def test_crop_padding_adds_context_on_both_sides(self) -> None:
        """Ascenders and descenders routinely fall outside the box Tesseract
        reports, so a word cropped exactly to its box is hard to read."""
        data = imaging.render_crop(self.wc, (200, 400, 400, 100), pad=20, zoom=1.0)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.size, (440, 140))

    def test_crop_clamps_at_the_page_edge(self) -> None:
        data = imaging.render_crop(self.wc, (0, 0, 50, 50), pad=40, zoom=1.0)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.size, (90, 90))     # 50 + 40 on the inside edges only

    def test_crop_zoom_magnifies(self) -> None:
        data = imaging.render_crop(self.wc, (200, 400, 100, 50), pad=0, zoom=3.0)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.size, (300, 150))

    def test_crop_respects_the_width_ceiling(self) -> None:
        data = imaging.render_crop(self.wc, (0, 0, 900, 100), pad=0, zoom=8.0,
                                   max_width=1400)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.width, 1400)

    def test_crop_maps_source_pixels_through_a_downscaled_working_copy(self) -> None:
        """The API speaks source pixels whatever resolution the cached
        working copy happens to be at -- nothing outside imaging.py needs to
        know a working copy exists."""
        self.assertEqual(self.wc_small.working_size, (500, 600))
        self.assertAlmostEqual(self.wc_small.scale, 0.5, places=6)
        data = imaging.render_crop(self.wc_small, (200, 400, 400, 100),
                                   pad=0, zoom=1.0)
        with Image.open(BytesIO(data)) as im:
            self.assertEqual(im.size, (200, 50))
            # Still centred on the ink band, half-size.
            r, g, b = im.convert("RGB").getpixel((100, 25))
            self.assertLess(r + g + b, 120)

    def test_degenerate_box_yields_a_pixel_not_an_error(self) -> None:
        """Malformed ALTO geometry is a data problem for the corrector to see
        on the page, not a reason to fail their request."""
        for box in ((0, 0, 0, 0), (-9000, -9000, 5, 5), (99999, 99999, 10, 10)):
            with self.subTest(box=box):
                with Image.open(BytesIO(imaging.render_crop(
                        self.wc, box, pad=0, zoom=1.0))) as im:
                    self.assertGreaterEqual(im.width, 1)


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prunes_down_to_the_ceiling(self) -> None:
        scans = []
        for i in range(6):
            path = self.root / f"p{i}.tif"
            _tif(path, (700, 900))
            scans.append(path)
            imaging.working_copy(path, self.cache, max_px=700)
        before = sum(p.stat().st_size for p in self.cache.glob("*.jpg"))
        self.assertGreater(before, 0)
        freed = imaging.prune_cache(self.cache, max_bytes=before // 3)
        self.assertGreater(freed, 0)
        after = sum(p.stat().st_size for p in self.cache.glob("*.jpg"))
        self.assertLessEqual(after, before // 3 + max(
            (p.stat().st_size for p in self.cache.glob("*.jpg")), default=0))

    def test_under_the_ceiling_is_a_no_op(self) -> None:
        _tif(self.root / "p.tif", (400, 400))
        imaging.working_copy(self.root / "p.tif", self.cache, max_px=400)
        self.assertEqual(imaging.prune_cache(self.cache, max_bytes=10 ** 9), 0)

    def test_an_evicted_render_is_rebuilt_on_demand(self) -> None:
        """Renders are pure functions of scans that still exist, so eviction
        only ever costs a re-decode."""
        scan = self.root / "p.tif"
        _tif(scan, (400, 500))
        first = imaging.working_copy(scan, self.cache, max_px=400)
        first.path.unlink()
        second = imaging.working_copy(scan, self.cache, max_px=400)
        self.assertTrue(second.path.is_file())
        self.assertEqual(second.source_size, (400, 500))

    def test_missing_cache_dir_prunes_to_nothing(self) -> None:
        self.assertEqual(imaging.prune_cache(self.cache / "absent", max_bytes=0), 0)


if __name__ == "__main__":
    unittest.main()
