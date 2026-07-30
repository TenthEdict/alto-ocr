"""Tests for the repackage command's logic (remediate.repackage_object).

After hand-correction the ALTO is the source of truth: transcripts re-render
through the canonical ``alto_to_text`` and the METS is rebuilt so checksums
match the edited files. These tests lock in: idempotent txt regeneration,
checksum refresh against edited ALTO, structMap page-order preservation
(stem sorting cannot reconstruct nested-folder order), appending pages the
old METS didn't know, dropping METS entries with no ALTO on disk, and the
opt-out behaviors (no Text/, no METS).

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_repackage
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.mets import METS_NS, XLINK_NS, PageEntry, build_mets, write_mets  # noqa: E402
from src.alto_ocr.remediate import (  # noqa: E402
    iter_object_dirs,
    repackage_object,
)

_M = f"{{{METS_NS}}}"


def _write_alto(path: Path, *words: str) -> None:
    strings = "\n".join(
        f'\t\t\t\t\t\t<String ID="S{i}" HPOS="{100 + i * 300}" VPOS="100" '
        f'WIDTH="200" HEIGHT="60" CONTENT="{w}"/>'
        for i, w in enumerate(words, start=1)
    )
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description><MeasurementUnit>mm10</MeasurementUnit></Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159">
\t\t\t<PrintSpace ID="PS1" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="100">
\t\t\t\t\t<TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
{strings}
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
""", encoding="utf-8")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _mets_files(mets_path: Path) -> dict[str, dict]:
    """href → attribute dict for every <file> in a METS."""
    root = etree.parse(str(mets_path)).getroot()
    out: dict[str, dict] = {}
    for f in root.iter(f"{_M}file"):
        loc = f.find(f"{_M}FLocat")
        out[loc.get(f"{{{XLINK_NS}}}href")] = dict(f.attrib)
    return out


def _mets_page_labels(mets_path: Path) -> list[str]:
    root = etree.parse(str(mets_path)).getroot()
    divs = [el for el in root.iter(f"{_M}div") if el.get("TYPE") == "page"]
    return [el.get("LABEL") for el in sorted(divs, key=lambda e: int(e.get("ORDER")))]


class _ObjectFixture(unittest.TestCase):
    """RefA: two ALTO pages, stale Text files, and a METS whose structMap
    deliberately orders page002 before page001 (stem sort would invert it)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.obj = self.root / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir()
        _write_alto(self.obj / "ALTO" / "page001.xml", "Board", "Minutes")
        _write_alto(self.obj / "ALTO" / "page002.xml", "Adjourned")
        (self.obj / "Text" / "page001.txt").write_text("stale one", encoding="utf-8")
        (self.obj / "Text" / "page002.txt").write_text("stale two", encoding="utf-8")
        self.mets = self.obj / "RefA.mets.xml"
        write_mets(build_mets(
            "RefA", self.obj,
            [PageEntry(stem="page002", order=1), PageEntry(stem="page001", order=2)],
        ), self.mets)


class RepackageTests(_ObjectFixture):
    def test_txt_regenerated_canonically_and_idempotently(self) -> None:
        r = repackage_object(self.obj)
        self.assertEqual(r.txt_written, 2)
        self.assertTrue(r.mets_written)
        self.assertTrue(r.ok)
        self.assertEqual((self.obj / "Text" / "page001.txt").read_text(encoding="utf-8"),
                         "Board Minutes\n")
        self.assertEqual((self.obj / "Text" / "page002.txt").read_text(encoding="utf-8"),
                         "Adjourned\n")
        again = repackage_object(self.obj)
        self.assertEqual(again.txt_written, 0, "unchanged transcripts must not be rewritten")

    def test_mets_checksums_match_edited_files(self) -> None:
        before = _mets_files(self.mets)
        _write_alto(self.obj / "ALTO" / "page001.xml", "Board", "of", "Trustees")
        repackage_object(self.obj)
        after = _mets_files(self.mets)

        self.assertNotEqual(before["ALTO/page001.xml"]["CHECKSUM"],
                            after["ALTO/page001.xml"]["CHECKSUM"])
        self.assertEqual(after["ALTO/page001.xml"]["CHECKSUM"],
                         _md5(self.obj / "ALTO" / "page001.xml"))
        self.assertEqual(after["Text/page001.txt"]["CHECKSUM"],
                         _md5(self.obj / "Text" / "page001.txt"))
        self.assertEqual((self.obj / "Text" / "page001.txt").read_text(encoding="utf-8"),
                         "Board of Trustees\n")

    def test_structmap_order_is_preserved(self) -> None:
        repackage_object(self.obj)
        self.assertEqual(_mets_page_labels(self.mets), ["page002", "page001"])

    def test_new_page_is_appended_after_existing_order(self) -> None:
        _write_alto(self.obj / "ALTO" / "page000.xml", "Cover")
        r = repackage_object(self.obj)
        self.assertTrue(r.ok)
        # Old order kept first; the new page000 appends despite sorting first.
        self.assertEqual(_mets_page_labels(self.mets), ["page002", "page001", "page000"])
        self.assertEqual((self.obj / "Text" / "page000.txt").read_text(encoding="utf-8"),
                         "Cover\n")

    def test_mets_entry_without_alto_is_dropped_with_note(self) -> None:
        write_mets(build_mets(
            "RefA", self.obj,
            [PageEntry(stem="ghost", order=1),
             PageEntry(stem="page001", order=2),
             PageEntry(stem="page002", order=3)],
        ), self.mets)
        r = repackage_object(self.obj)
        self.assertFalse(r.ok)
        self.assertIn("ghost", r.problems[0])
        self.assertEqual(_mets_page_labels(self.mets), ["page001", "page002"])

    def test_no_mets_flag_leaves_mets_untouched(self) -> None:
        before = self.mets.read_bytes()
        r = repackage_object(self.obj, write_mets_file=False)
        self.assertFalse(r.mets_written)
        self.assertEqual(self.mets.read_bytes(), before)
        self.assertEqual(r.txt_written, 2)


class OptOutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_object_without_text_dir_gets_alto_only_mets(self) -> None:
        obj = self.root / "RefB"
        (obj / "ALTO").mkdir(parents=True)
        _write_alto(obj / "ALTO" / "p1.xml", "Hello")
        write_mets(build_mets("RefB", obj, [PageEntry(stem="p1", order=1)],
                              with_plaintext=False), obj / "RefB.mets.xml")
        r = repackage_object(obj)
        self.assertEqual(r.txt_written, 0)
        self.assertTrue(r.mets_written)
        hrefs = set(_mets_files(obj / "RefB.mets.xml"))
        self.assertEqual(hrefs, {"ALTO/p1.xml"})
        self.assertFalse((obj / "Text").exists(), "repackage must not invent Text/")

    def test_object_without_mets_regenerates_txt_only(self) -> None:
        obj = self.root / "RefC"
        (obj / "ALTO").mkdir(parents=True)
        (obj / "Text").mkdir()
        _write_alto(obj / "ALTO" / "p1.xml", "Hi")
        r = repackage_object(obj)
        self.assertFalse(r.mets_written)
        self.assertEqual(r.txt_written, 1)
        self.assertFalse((obj / "RefC.mets.xml").exists(), "repackage must not invent a METS")

    def test_iter_object_dirs_contract(self) -> None:
        a = self.root / "RefA" / "ALTO"
        a.mkdir(parents=True)
        (self.root / "not_an_object").mkdir()
        (self.root / "stray.txt").write_text("x", encoding="utf-8")
        self.assertEqual([d.name for d in iter_object_dirs(self.root)], ["RefA"])
        # Pointed directly at one object, the root itself is the object.
        self.assertEqual(iter_object_dirs(self.root / "RefA"), [self.root / "RefA"])


if __name__ == "__main__":
    unittest.main()
