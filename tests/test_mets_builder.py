"""Tests for ``mets.build_mets`` — xs:ID sanitization, checksums, structMap.

Archival filenames carry spaces and punctuation that are illegal in xs:ID
values; the builder sanitizes IDs while keeping hrefs/LABELs raw so humans
can still pair files visually. Locked in here: NCName sanitization, hash
dedup when two stems sanitize identically, fptr/file ID referential
integrity, ORDER/LABEL in the structMap, checksum + size fixity, and the
``with_plaintext`` opt-out.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_mets_builder
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr import __version__  # noqa: E402
from src.alto_ocr.mets import (  # noqa: E402
    METS_NS,
    XLINK_NS,
    PageEntry,
    build_mets,
)

_M = f"{{{METS_NS}}}"


class MetsBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.obj = Path(self._tmp.name) / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir()

    def _write_pages(self, *stems: str) -> list[PageEntry]:
        pages = []
        for i, stem in enumerate(stems, start=1):
            (self.obj / "ALTO" / f"{stem}.xml").write_text(
                f"<alto>{stem}</alto>", encoding="utf-8")
            (self.obj / "Text" / f"{stem}.txt").write_text(
                f"text {stem}", encoding="utf-8")
            pages.append(PageEntry(stem=stem, order=i))
        return pages

    def _build(self, pages, **kw) -> etree._Element:
        return build_mets("RefA", self.obj, pages, **kw).getroot()

    def _files(self, root) -> dict[str, etree._Element]:
        return {f.get("ID"): f for f in root.iter(f"{_M}file")}

    def test_spaces_sanitized_in_ids_raw_in_hrefs_and_labels(self) -> None:
        root = self._build(self._write_pages("scan 001"))
        (fid, f), *rest = [(k, v) for k, v in self._files(root).items()
                           if k.startswith("alto_")]
        self.assertEqual(fid, "alto_scan_001", "xs:ID must not contain spaces")
        loc = f.find(f"{_M}FLocat")
        self.assertEqual(loc.get(f"{{{XLINK_NS}}}href"), "ALTO/scan 001.xml",
                         "href keeps the raw filename")
        div = next(d for d in root.iter(f"{_M}div") if d.get("TYPE") == "page")
        self.assertEqual(div.get("LABEL"), "scan 001")
        self.assertEqual(div.get("ORDER"), "1")

    def test_sanitization_collisions_get_unique_ids(self) -> None:
        # "a b" and "a_b" both sanitize to "a_b" — the second must get a
        # deterministic hash suffix, and every fptr must reference a real ID.
        root = self._build(self._write_pages("a b", "a_b"))
        ids = set(self._files(root))
        self.assertEqual(len(ids), 4, "2 pages x 2 groups, all IDs unique")
        fptr_refs = {p.get("FILEID") for p in root.iter(f"{_M}fptr")}
        self.assertEqual(fptr_refs, ids, "structMap must reference exactly the file IDs")

    def test_plaintext_optout_omits_text_group(self) -> None:
        root = self._build(self._write_pages("p1"), with_plaintext=False)
        grps = [g.get("USE") for g in root.iter(f"{_M}fileGrp")]
        self.assertEqual(grps, ["ocr"])
        div = next(d for d in root.iter(f"{_M}div") if d.get("TYPE") == "page")
        self.assertEqual(len(div.findall(f"{_M}fptr")), 1)

    def test_checksums_and_sizes_match_disk(self) -> None:
        pages = self._write_pages("p1")
        root = self._build(pages, with_checksums=True)
        alto_file = self.obj / "ALTO" / "p1.xml"
        f = next(v for k, v in self._files(root).items() if k.startswith("alto_"))
        self.assertEqual(f.get("CHECKSUM"),
                         hashlib.md5(alto_file.read_bytes()).hexdigest())
        self.assertEqual(f.get("CHECKSUMTYPE"), "MD5")
        self.assertEqual(f.get("SIZE"), str(alto_file.stat().st_size))

        root = self._build(pages, with_checksums=False)
        f = next(v for k, v in self._files(root).items() if k.startswith("alto_"))
        self.assertIsNone(f.get("CHECKSUM"))
        self.assertIsNotNone(f.get("SIZE"), "SIZE stays even without checksums")

    def test_agent_records_tool_and_version(self) -> None:
        root = self._build(self._write_pages("p1"))
        name = next(root.iter(f"{_M}name"))
        self.assertEqual(name.text, f"alto_ocr {__version__}")


if __name__ == "__main__":
    unittest.main()
