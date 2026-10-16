"""Discovery must never silently drop an object that has pages.

An output-stem collision used to make ``discover_ref_id`` return ``None`` —
indistinguishable from "not an object" — so a batch would process everything
else and exit 0 while a real object vanished with only a warning line.
Collisions now come back as ``SkippedRefId`` records that callers fold into
the summary and the exit code (``pipeline.skipped_ref_result``); "no images
at all" stays ``None`` because collection dirs legitimately contain
non-object folders.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_discovery_skips
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.pipeline import skipped_ref_result  # noqa: E402
from src.alto_ocr.walker import (  # noqa: E402
    RefIdInputs,
    SkippedRefId,
    discover_collection,
    discover_ref_id,
)


class DiscoverySkipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make(self, ref: str, *names: str) -> Path:
        d = self.root / ref
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            (d / n).write_bytes(b"")  # walker never opens image files
        return d

    def test_collision_returns_skip_record(self) -> None:
        # Two TIFs in different sub-folders share the output stem "a".
        d = self._make("RefBad", "x/a.tif", "y/a.tif", "b.tif")
        item = discover_ref_id(d)
        self.assertIsInstance(item, SkippedRefId)
        self.assertEqual(item.ref_id, "RefBad")
        self.assertEqual(item.page_count, 3, "true page count for the summary")
        self.assertIn("'a'", item.reason)
        self.assertIn("collision", item.reason)

    def test_clean_object_returns_inputs(self) -> None:
        d = self._make("RefGood", "page1.tif", "page2.tif")
        item = discover_ref_id(d)
        self.assertIsInstance(item, RefIdInputs)
        self.assertEqual(item.page_count, 2)

    def test_non_object_dir_stays_none(self) -> None:
        d = self._make("notes", "readme.txt", "Thumbs.db")
        self.assertIsNone(discover_ref_id(d))

    def test_collection_separates_skips_from_processables(self) -> None:
        self._make("RefBad", "x/a.tif", "y/a.tif")
        self._make("RefGood", "p.tif")
        self._make("notes", "readme.txt")
        items, skipped = discover_collection(self.root)
        self.assertEqual([i.ref_id for i in items], ["RefGood"])
        self.assertEqual([s.ref_id for s in skipped], ["RefBad"])

    def test_jpeg_export_of_a_tif_is_dropped(self) -> None:
        # Capture One's sibling TIFF/ + JPEG/ exports of the same pages.
        d = self._make("RefPair", "TIFF/p1.tif", "TIFF/p2.TIFF",
                       "JPEG/p1.jpg", "JPEG/p2.jpeg", "JPEG/p3.jpg")
        item = discover_ref_id(d)
        self.assertIsInstance(item, RefIdInputs)
        self.assertEqual([s.path.name for s in item.sources],
                         ["p3.jpg", "p1.tif", "p2.TIFF"],
                         "TIF twins win; a JPEG with no TIF is kept")

    def test_jpeg_pair_does_not_hide_a_real_collision(self) -> None:
        d = self._make("RefBad", "x/a.tif", "y/a.tif", "y/a.jpg")
        item = discover_ref_id(d)
        self.assertIsInstance(item, SkippedRefId)
        self.assertIn("'a'", item.reason)

    def test_skipped_ref_result_shape(self) -> None:
        skip = SkippedRefId(ref_id="RefBad", reason="stem collision on 'a'",
                            page_count=4)
        r = skipped_ref_result(skip, self.root / "out")
        self.assertFalse(r.ok)
        self.assertEqual(r.page_count, 4)
        self.assertEqual(r.failed_page_count, 4,
                         "every page of a skipped object counts as unproduced")
        self.assertIn("collision", r.pages[0].error)


if __name__ == "__main__":
    unittest.main()
