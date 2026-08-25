"""The delivery METS: flat hrefs and an Image fileGrp.

One Ref ID is delivered to the repository as a single flat folder holding the
preservation TIFFs, the ALTO, the transcripts and the manifest side by side
(see instructions.txt). Two consequences the working METS does not have:

* the manifest must reference the images. It sits in the same folder as them,
  so a METS listing only the OCR outputs would look complete while describing
  two thirds of the package.
* hrefs are bare filenames, not ``ALTO/x.xml`` — there are no subfolders to
  resolve against.

The working METS keeps its nested hrefs and image-free fileSec, because it
describes the pipeline's own tree and the re-OCR guard reads its checksums to
detect provenance-less edits. Both shapes are locked here so neither drifts
into the other.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_mets_delivery
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.mets import METS_NS, XLINK_NS, PageEntry, build_mets  # noqa: E402


class DeliveryMetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.obj = root / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir(parents=True)
        self.images = root / "RefA_master"
        self.images.mkdir()
        self.pages = [PageEntry(stem=f"page{n:03d}", order=n) for n in (1, 2)]
        for p in self.pages:
            (self.obj / "ALTO" / f"{p.stem}.xml").write_text("<alto/>", encoding="utf-8")
            (self.obj / "Text" / f"{p.stem}.txt").write_text("text\n", encoding="utf-8")
            (self.images / f"{p.stem}.tif").write_bytes(b"II*\x00fake-tiff-" + p.stem.encode())

    def _hrefs(self, tree) -> list[str]:
        return [el.get(f"{{{XLINK_NS}}}href")
                for el in tree.getroot().iter(f"{{{METS_NS}}}FLocat")]

    def _uses(self, tree) -> list[str]:
        return [el.get("USE") for el in tree.getroot().iter(f"{{{METS_NS}}}fileGrp")]

    def _delivery(self):
        return build_mets("RefA", self.obj, self.pages,
                          image_dir=self.images, flat_hrefs=True)

    # -- delivery shape --------------------------------------------------
    def test_images_are_described(self) -> None:
        self.assertIn("preservation", self._uses(self._delivery()))

    def test_every_file_is_listed_once_per_page(self) -> None:
        hrefs = self._hrefs(self._delivery())
        self.assertEqual(sorted(hrefs), [
            "page001.tif", "page001.txt", "page001.xml",
            "page002.tif", "page002.txt", "page002.xml",
        ])

    def test_hrefs_are_bare_filenames(self) -> None:
        self.assertFalse([h for h in self._hrefs(self._delivery()) if "/" in h],
                         "a flat folder has nothing to resolve a subdirectory against")

    def test_image_checksums_come_from_the_image_directory(self) -> None:
        # Images live outside the object dir; the METS must still size and
        # checksum them rather than silently omitting fixity.
        tree = self._delivery()
        want = hashlib.md5((self.images / "page001.tif").read_bytes()).hexdigest()  # noqa: S324
        for el in tree.getroot().iter(f"{{{METS_NS}}}file"):
            href = el.find(f"{{{METS_NS}}}FLocat").get(f"{{{XLINK_NS}}}href")
            if href == "page001.tif":
                self.assertEqual(el.get("CHECKSUM"), want)
                self.assertEqual(el.get("SIZE"), str((self.images / "page001.tif").stat().st_size))
                self.assertEqual(el.get("MIMETYPE"), "image/tiff")
                return
        self.fail("no file element for page001.tif")

    def test_each_page_div_points_at_all_three_files(self) -> None:
        tree = self._delivery()
        divs = [d for d in tree.getroot().iter(f"{{{METS_NS}}}div")
                if d.get("TYPE") == "page"]
        self.assertEqual(len(divs), 2)
        for d in divs:
            fptrs = d.findall(f"{{{METS_NS}}}fptr")
            self.assertEqual(len(fptrs), 3, "image + ALTO + text")

    # -- the working METS must not change --------------------------------
    def test_working_mets_keeps_nested_hrefs_and_no_images(self) -> None:
        tree = build_mets("RefA", self.obj, self.pages)
        hrefs = self._hrefs(tree)
        self.assertEqual(sorted(hrefs), [
            "ALTO/page001.xml", "ALTO/page002.xml",
            "Text/page001.txt", "Text/page002.txt",
        ])
        self.assertNotIn("preservation", self._uses(tree))

    def test_working_hrefs_resolve_against_the_object_dir(self) -> None:
        # The re-OCR guard reads this METS's checksums by resolving hrefs
        # under the object directory; if they stopped resolving, corrections
        # made in Goobi's editor would become invisible again.
        for href in self._hrefs(build_mets("RefA", self.obj, self.pages)):
            self.assertTrue((self.obj / href).is_file(), href)

    def test_plaintext_opt_out_still_honoured_with_images(self) -> None:
        tree = build_mets("RefA", self.obj, self.pages,
                          image_dir=self.images, flat_hrefs=True,
                          with_plaintext=False)
        self.assertEqual(sorted(self._hrefs(tree)),
                         ["page001.tif", "page001.xml", "page002.tif", "page002.xml"])


if __name__ == "__main__":
    unittest.main()
