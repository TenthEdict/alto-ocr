"""Tests for the review server's object discovery (webreview/discovery.py).

Covers: Goobi's ``metadata/<id>/ocr/alto_ocr/<RefID>`` symlink-view layout,
the standalone output-root layout, preservation-folder preference
(``_master`` before ``_media``), the rule that a Ref ID from a URL is looked
up rather than joined to a path, per-page state derivation including a
corrupt ALTO surfacing as ``failed`` instead of breaking the object, the
worst-first ordering (reviewed pages sink), and the stats cache's
filesystem-derived invalidation.

Run from the repo root:

    python -m unittest tests.test_webreview_discovery
"""

from __future__ import annotations

import functools
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.webreview.config import ReviewServerConfig  # noqa: E402
from src.alto_ocr.webreview.discovery import (  # noqa: E402
    ObjectIndex,
    PageState,
    StatsCache,
    discover_goobi_objects,
    discover_standalone_objects,
    scan_object,
)

_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
{provenance}\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="{pc}">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="200">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Board" WC="0.96"/>
\t\t\t\t\t\t<String ID="P1_ST00002" HPOS="320" VPOS="100" WIDTH="150" HEIGHT="60" CONTENT="cf" WC="0.30"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

_PROVENANCE = ('\t\t<Processing ID="review">\n'
               '\t\t\t<processingDateTime>2026-08-25T12:00:00+00:00</processingDateTime>\n'
               '\t\t\t<processingAgency>alto_ocr</processingAgency>\n'
               '\t\t\t<processingStepDescription>human review: 4 word(s) corrected'
               '</processingStepDescription>\n'
               '\t\t</Processing>\n')


def _alto(pc: str = "0.63", reviewed: bool = False) -> str:
    return _ALTO.format(pc=pc, provenance=_PROVENANCE if reviewed else "")


def _write_image(path: Path) -> None:
    """A real 1x1 TIF -- index_source_images filters on suffix, but a later
    imaging test opening one of these needs it to actually decode."""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 10), "white").save(path, format="TIFF")


@functools.lru_cache(maxsize=1)
def _symlinks_available() -> bool:
    """Whether this process may create symlinks.

    Windows grants that privilege only under Developer Mode or elevation;
    without either, symlink creation raises ``OSError`` (WinError 1314). That
    is a limit of the environment, not a failure of the code under test, so
    the symlink-layout tests below SKIP rather than error -- otherwise every
    run on an ordinary Windows workstation reports failures that say nothing
    about the change being made, and the pre-push hook blocks every push.
    CI still exercises them (Linux, and Windows runners hold the privilege).

    Probed by actually creating one, not by checking the platform: the
    privilege is per-process and per-configuration, not per-OS.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()
        try:
            os.symlink(target, Path(tmp) / "link", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_NEEDS_SYMLINKS = unittest.skipUnless(
    _symlinks_available(),
    "symlink creation not permitted here (Windows needs Developer Mode or elevation)",
)


@_NEEDS_SYMLINKS
class GoobiLayoutTests(unittest.TestCase):
    """The layout the bridge builds: real ALTO folders, a symlink view."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "metadata"
        self.ref = "MSP00064_9528"
        self.process = self.root / "1234"
        real_alto = self.process / "ocr" / f"{self.ref}_alto"
        real_txt = self.process / "ocr" / f"{self.ref}_txt"
        real_alto.mkdir(parents=True)
        real_txt.mkdir(parents=True)
        (real_alto / f"{self.ref}_00001.xml").write_text(_alto(), encoding="utf-8")
        (real_alto / f"{self.ref}_00002.xml").write_text(_alto("0.91", reviewed=True),
                                                         encoding="utf-8")
        view = self.process / "ocr" / "alto_ocr" / self.ref
        view.mkdir(parents=True)
        os.symlink(f"../../{self.ref}_alto", view / "ALTO")
        os.symlink(f"../../{self.ref}_txt", view / "Text")
        _write_image(self.process / "images" / f"{self.ref}_master" / f"{self.ref}_00001.tif")
        _write_image(self.process / "images" / f"{self.ref}_master" / f"{self.ref}_00002.tif")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_finds_the_object_through_the_symlink_view(self) -> None:
        objects = discover_goobi_objects(self.root)
        self.assertEqual([o.ref_id for o in objects], [self.ref])
        obj = objects[0]
        self.assertEqual(obj.process_id, "1234")
        self.assertTrue(obj.alto_dir.is_dir())
        # Editing through the view must reach Goobi's own folder -- that is
        # the whole reason the view exists.
        self.assertEqual(obj.alto_dir.resolve(),
                         (self.process / "ocr" / f"{self.ref}_alto").resolve())

    def test_prefers_the_master_scans_over_a_derived_copy(self) -> None:
        """Word boxes are in the master's coordinate frame; ``_media`` is a
        derived display copy that need not share its pixel dimensions."""
        _write_image(self.process / "images" / f"{self.ref}_media" / f"{self.ref}_00001.tif")
        obj = discover_goobi_objects(self.root)[0]
        self.assertEqual(obj.image_dir.name, f"{self.ref}_master")

    def test_empty_master_does_not_shadow_populated_media(self) -> None:
        """Goobi creates ``<title>_master`` whether or not anything was
        imported into it, so on a process whose scans went to ``_media`` the
        empty master must not win -- otherwise every page reports "no source
        scan" with a populated folder sitting next to it. Observed on this
        install's process 7."""
        import shutil
        master = self.process / "images" / f"{self.ref}_master"
        shutil.rmtree(master)
        master.mkdir()
        _write_image(self.process / "images" / f"{self.ref}_media" / f"{self.ref}_00001.tif")
        obj = discover_goobi_objects(self.root)[0]
        self.assertEqual(obj.image_dir.name, f"{self.ref}_media")

    def test_empty_folder_is_still_reported_when_nothing_has_images(self) -> None:
        """Reported with its intended location rather than none at all, so
        the worklist can say which folder is empty."""
        import shutil
        shutil.rmtree(self.process / "images" / f"{self.ref}_master")
        (self.process / "images" / f"{self.ref}_master").mkdir()
        obj = discover_goobi_objects(self.root)[0]
        self.assertEqual(obj.image_dir.name, f"{self.ref}_master")

    def test_junk_files_do_not_count_as_images(self) -> None:
        """Windows generates Thumbs.db continuously and it is all over the
        test data."""
        import shutil
        shutil.rmtree(self.process / "images" / f"{self.ref}_master")
        master = self.process / "images" / f"{self.ref}_master"
        master.mkdir()
        (master / "Thumbs.db").write_bytes(b"junk")
        _write_image(self.process / "images" / f"{self.ref}_media" / f"{self.ref}_00001.tif")
        obj = discover_goobi_objects(self.root)[0]
        self.assertEqual(obj.image_dir.name, f"{self.ref}_media")

    def test_dangling_view_is_not_an_object(self) -> None:
        """A view left behind after Goobi's folder was deleted has nothing to
        review, and must not appear as an empty object in the worklist."""
        import shutil
        shutil.rmtree(self.process / "ocr" / f"{self.ref}_alto")
        self.assertEqual(discover_goobi_objects(self.root), [])

    def test_missing_root_is_reported_as_empty_not_an_error(self) -> None:
        self.assertEqual(discover_goobi_objects(self.root / "nope"), [])

    def test_goobi_process_link_is_relative(self) -> None:
        """The review UI is reached *from* Goobi, so the browser already knows
        the hostname; a relative link works whatever it is."""
        obj = discover_goobi_objects(self.root)[0]
        self.assertEqual(obj.goobi_task_url_hint,
                         "/goobi/process_edit.xhtml?id=1234")

    def test_index_lookup_never_joins_a_url_path(self) -> None:
        """A Ref ID arrives from a browser. Joining it to a root would make
        every review URL a directory traversal."""
        index = ObjectIndex(ReviewServerConfig(goobi_metadata_root=self.root))
        self.assertIsNotNone(index.get(self.ref))
        for hostile in ("../../etc", "..", ".", "/etc/passwd",
                        f"../{self.ref}", f"{self.ref}/../{self.ref}"):
            with self.subTest(ref=hostile):
                self.assertIsNone(index.get(hostile))

    def test_page_stems_resolve_against_the_folder_listing(self) -> None:
        index = ObjectIndex(ReviewServerConfig(goobi_metadata_root=self.root))
        obj = index.get(self.ref)
        self.assertIsNotNone(index.alto_for(obj, f"{self.ref}_00001"))
        for hostile in ("../../../etc/passwd", "..", f"../{self.ref}_00001"):
            with self.subTest(stem=hostile):
                self.assertIsNone(index.alto_for(obj, hostile))
        self.assertIsNone(index.image_for(obj, "../../secret"))

    def test_new_object_appears_without_waiting_for_the_ttl(self) -> None:
        """A miss forces a rescan: a corrector following a link to an object
        created seconds ago must not get a 404."""
        index = ObjectIndex(ReviewServerConfig(goobi_metadata_root=self.root),
                            ttl_seconds=9999)
        self.assertEqual(len(index.objects()), 1)
        other = self.root / "1235" / "ocr" / "alto_ocr" / "MSP00084_1" / "ALTO"
        other.mkdir(parents=True)
        (other / "p1.xml").write_text(_alto(), encoding="utf-8")
        self.assertIsNotNone(index.get("MSP00084_1"))


class StandaloneLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "output"
        self.src = root / "preservation"
        (self.out / "OBJ_A" / "ALTO").mkdir(parents=True)
        (self.out / "OBJ_A" / "ALTO" / "p1.xml").write_text(_alto(), encoding="utf-8")
        (self.out / "not_an_object").mkdir()
        _write_image(self.src / "OBJ_A" / "p1.tif")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_finds_objects_and_their_per_object_scans(self) -> None:
        objects = discover_standalone_objects(self.out, self.src)
        self.assertEqual([o.ref_id for o in objects], ["OBJ_A"])
        self.assertEqual(objects[0].image_dir, self.src / "OBJ_A")

    def test_flat_source_root_is_accepted(self) -> None:
        """Pointing the server at one object's scans, as the Tk tab allows."""
        objects = discover_standalone_objects(self.out, self.src / "OBJ_A")
        self.assertEqual(objects[0].image_dir, self.src / "OBJ_A")

    def test_goobi_copy_wins_a_ref_id_collision(self) -> None:
        """That is the copy the workflow is tracking; a correction made
        anywhere else would never reach it."""
        meta = Path(self.tmp.name) / "metadata"
        view = meta / "77" / "ocr" / "alto_ocr" / "OBJ_A"
        (view / "ALTO").mkdir(parents=True)
        (view / "ALTO" / "p1.xml").write_text(_alto(), encoding="utf-8")
        index = ObjectIndex(ReviewServerConfig(
            goobi_metadata_root=meta, output_root=self.out, source_root=self.src))
        self.assertEqual(index.get("OBJ_A").process_id, "77")


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alto_dir = root / "OBJ" / "ALTO"
        self.alto_dir.mkdir(parents=True)
        (self.alto_dir / "p1.xml").write_text(_alto("0.63"), encoding="utf-8")
        (self.alto_dir / "p2.xml").write_text(_alto("0.91", reviewed=True), encoding="utf-8")
        self.src = root / "scans"
        _write_image(self.src / "p1.tif")
        from src.alto_ocr.webreview.discovery import ReviewObject
        self.obj = ReviewObject(ref_id="OBJ", object_dir=root / "OBJ", image_dir=self.src)
        from src.alto_ocr.remediate import index_source_images
        self.images = index_source_images(self.src)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_derives_page_state_and_totals(self) -> None:
        pages, stats = scan_object(self.obj, self.images, low_wc=0.5)
        self.assertEqual([p.stem for p in pages], ["p1", "p2"])
        self.assertEqual(stats.pages, 2)
        self.assertEqual(stats.reviewed_pages, 1)
        self.assertEqual(stats.pages_to_review, 1)
        self.assertEqual(stats.words, 4)
        self.assertEqual(stats.low_words, 2)
        self.assertEqual(stats.missing_images, 1)   # p2 has no scan
        self.assertAlmostEqual(stats.mean_pc, (0.63 + 0.91) / 2, places=6)
        self.assertTrue(pages[0].has_image)
        self.assertFalse(pages[1].has_image)
        self.assertTrue(pages[1].reviewed)

    def test_corrupt_alto_surfaces_as_a_failed_page(self) -> None:
        """One bad file in a 900-page object must not make the other 899
        unreviewable, and ``failed`` already sorts first."""
        (self.alto_dir / "p3.xml").write_text("<alto>truncated", encoding="utf-8")
        pages, stats = scan_object(self.obj, self.images, low_wc=0.5)
        bad = next(p for p in pages if p.stem == "p3")
        self.assertEqual(bad.status, "failed")
        self.assertEqual(stats.failed_pages, 1)
        self.assertEqual(stats.pages, 3)

    def test_worst_first_sinks_reviewed_pages(self) -> None:
        states = [
            PageState("a", "scored", 0.95, 10, 0, reviewed=False, has_image=True),
            PageState("b", "scored", 0.40, 10, 8, reviewed=False, has_image=True),
            PageState("c", "scored", 0.10, 10, 9, reviewed=True, has_image=True),
            PageState("d", "failed", None, 0, 0, reviewed=False, has_image=True),
        ]
        order = [s.stem for s in sorted(states, key=lambda s: s.sort_key_worst_first)]
        # Failure first, then worst unreviewed, then the rest -- and the
        # already-corrected page last despite having the lowest confidence.
        self.assertEqual(order, ["d", "b", "a", "c"])


class StatsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alto_dir = root / "OBJ" / "ALTO"
        self.alto_dir.mkdir(parents=True)
        (self.alto_dir / "p1.xml").write_text(_alto("0.63"), encoding="utf-8")
        from src.alto_ocr.webreview.discovery import ReviewObject
        self.obj = ReviewObject(ref_id="OBJ", object_dir=root / "OBJ", image_dir=None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repeat_reads_are_served_from_cache(self) -> None:
        cache = StatsCache()
        first, _ = cache.get(self.obj, {})
        second, _ = cache.get(self.obj, {})
        self.assertIs(first, second)

    def test_an_edit_through_any_tool_invalidates_it(self) -> None:
        """Keyed on the ALTO folder's own state, so a save from the Tk GUI or
        an external editor cannot leave a stale count behind."""
        cache = StatsCache()
        _, before = cache.get(self.obj, {})
        self.assertEqual(before.pages, 1)
        (self.alto_dir / "p2.xml").write_text(_alto("0.20"), encoding="utf-8")
        _, after = cache.get(self.obj, {})
        self.assertEqual(after.pages, 2)

    def test_content_change_alone_invalidates_it(self) -> None:
        """Total bytes is in the fingerprint precisely so this works: the
        rewrite below lands inside the filesystem's mtime granularity
        (~4 ms locally, coarser over SMB), so mtime alone would miss it."""
        cache = StatsCache()
        _, before = cache.get(self.obj, {})
        self.assertFalse(before.reviewed_pages)
        (self.alto_dir / "p1.xml").write_text(_alto("0.63", reviewed=True), encoding="utf-8")
        _, after = cache.get(self.obj, {})
        self.assertEqual(after.reviewed_pages, 1)


class ConfigTests(unittest.TestCase):
    def test_output_root_without_source_root_is_refused(self) -> None:
        """The scans are not in the output tree, and a word cannot be checked
        against a page image that was never located."""
        with self.assertRaises(ValueError) as ctx:
            ReviewServerConfig(output_root=Path("/tmp/out"))
        self.assertIn("--source-root", str(ctx.exception))

    def test_no_roots_at_all_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ReviewServerConfig()


if __name__ == "__main__":
    unittest.main()
