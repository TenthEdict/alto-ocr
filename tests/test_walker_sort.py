"""Tests for the walker's ordering + stem-collision prediction.

Page order is part of the output contract (structMap ORDER comes from it,
and repackage preserves it forever after), and the collision predictor is
what keeps two sources from silently overwriting each other's ALTO/TXT.
Neither had direct tests before the 2026-07-30 review. Locked in here:
natural sort ("Page 2" < "Page 10", case-insensitive), rel-path grouping
(sub-folders steer coarse order), rasterized-PDF stem prediction, the
documented collision semantics (``Foo.pdf`` + ``Foo.tif`` do NOT collide;
``Foo.pdf`` page 1 + ``Foo_page001.tif`` DO), and suffix handling.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_walker_sort
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.walker import (  # noqa: E402
    SourceFile,
    _check_stem_collisions,
    _find_sources_in,
    natural_chunks,
    rel_natural_key,
)


class NaturalSortTests(unittest.TestCase):
    def test_numeric_runs_compare_numerically(self) -> None:
        names = ["Page 10", "Page 2", "page 1"]
        self.assertEqual(sorted(names, key=natural_chunks),
                         ["page 1", "Page 2", "Page 10"])

    def test_case_insensitive(self) -> None:
        self.assertEqual(sorted(["b.tif", "A.tif"], key=natural_chunks),
                         ["A.tif", "b.tif"])

    def test_rel_key_groups_by_subfolder_then_page(self) -> None:
        base = Path("C:/coll/RefA")
        paths = [
            base / "1928" / "meeting" / "Page 1.tif",
            base / "1927" / "UA58i19270613SM" / "Page 2.tif",
            base / "1927" / "UA58i19270104SM" / "Page 1.tif",
            base / "1927" / "UA58i19270613SM" / "Page 10.tif",
        ]
        ordered = sorted(paths, key=lambda p: rel_natural_key(p, base))
        self.assertEqual(
            [str(p.relative_to(base)) for p in ordered],
            [str(Path("1927/UA58i19270104SM/Page 1.tif")),
             str(Path("1927/UA58i19270613SM/Page 2.tif")),
             str(Path("1927/UA58i19270613SM/Page 10.tif")),
             str(Path("1928/meeting/Page 1.tif"))],
        )


class PredictedStemTests(unittest.TestCase):
    def test_tif_maps_to_its_own_stem(self) -> None:
        s = SourceFile(path=Path("scan 001.tif"), page_count=1)
        self.assertEqual(s.predicted_output_stems(), ["scan 001"])

    def test_pdf_expands_to_zero_padded_page_stems(self) -> None:
        s = SourceFile(path=Path("Minutes.pdf"), page_count=12)
        stems = s.predicted_output_stems()
        self.assertEqual(stems[0], "Minutes_page001")
        self.assertEqual(stems[-1], "Minutes_page012")
        self.assertEqual(len(stems), 12)


class CollisionPredictionTests(unittest.TestCase):
    def test_pdf_and_tif_sharing_a_stem_do_not_collide(self) -> None:
        # Documented contract: Foo.pdf expands to Foo_pageNNN, so Foo.tif is
        # a different output stem — legitimate, not an error.
        sources = [SourceFile(Path("Foo.tif"), 1), SourceFile(Path("Foo.pdf"), 2)]
        self.assertEqual(_check_stem_collisions("RefA", sources), [])

    def test_standalone_page_tif_collides_with_rasterized_pdf_page(self) -> None:
        sources = [SourceFile(Path("Foo_page001.tif"), 1),
                   SourceFile(Path("Foo.pdf"), 2)]
        collisions = _check_stem_collisions("RefA", sources)
        self.assertEqual([c[0] for c in collisions], ["Foo_page001"])

    def test_same_stem_different_image_ext_collides(self) -> None:
        sources = [SourceFile(Path("a.tif"), 1), SourceFile(Path("a.jpg"), 1)]
        self.assertEqual([c[0] for c in _check_stem_collisions("RefA", sources)],
                         ["a"])


class DiscoveryScanTests(unittest.TestCase):
    def test_suffix_case_and_junk_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("A.TIF", "b.JPeg", "notes.db", "readme.txt"):
                (d / name).write_bytes(b"")
            sources = _find_sources_in(d)
            self.assertEqual(sorted(s.path.name for s in sources),
                             ["A.TIF", "b.JPeg"],
                             "image suffixes match case-insensitively; "
                             "non-image files are ignored")


if __name__ == "__main__":
    unittest.main()
