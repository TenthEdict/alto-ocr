"""Hardening sweep tests: XML entity resolution off, CSV formula safety,
bounded image sizes.

ALTO/METS live on shared network drives and round-trip through external
editors, so pipeline parses go through ``alto_format.hardened_parser`` —
DTD-declared entities (the XXE vector) must never be resolved into the
document. The triage CSVs are explicitly opened in Excel, so cells sourced
from filesystem names are neutralized against formula injection. Pillow's
decompression-bomb ceiling is raised for archival scans but no longer
disabled outright.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_hardening
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.alto_format import hardened_parser  # noqa: E402
from src.alto_ocr.triage import (  # noqa: E402
    ObjectConfidence,
    PageConfidence,
    scan_alto_file,
    write_csvs,
)


class EntityResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _xxe_alto(self) -> Path:
        secret = self.dir / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
        alto = self.dir / "page.xml"
        alto.write_text(
            '<?xml version="1.0"?>\n'
            f'<!DOCTYPE alto [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>\n'
            '<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">'
            "<Description><MeasurementUnit>&xxe;</MeasurementUnit></Description>"
            "<Layout/></alto>",
            encoding="utf-8",
        )
        return alto

    def test_external_entity_is_not_resolved(self) -> None:
        root = etree.parse(str(self._xxe_alto()), hardened_parser()).getroot()
        serialized = etree.tostring(root).decode("utf-8", errors="replace")
        self.assertNotIn("TOP-SECRET-CONTENT", serialized,
                         "a DTD entity must never pull file contents in")

    def test_triage_scan_survives_xxe_attempt_without_leaking(self) -> None:
        info = scan_alto_file(self._xxe_alto())
        # No <Page> -> classifies as failed; the point is: no crash, no read.
        self.assertEqual(info.status, "failed")


class CsvInjectionTests(unittest.TestCase):
    def test_formula_leading_cells_are_neutralized(self) -> None:
        evil_page = PageConfidence(stem="=HYPERLINK(evil)", status="scored",
                                   pc=0.5, words=3, scored_words=3,
                                   low_words=1, wc_sum=1.5)
        obj = ObjectConfidence(ref_id="=cmd|calc", pages=[evil_page])
        with tempfile.TemporaryDirectory() as td:
            obj_csv, pages_csv = write_csvs([obj], Path(td))
            with obj_csv.open(newline="", encoding="utf-8-sig") as f:
                row = list(csv.reader(f))[1]
            self.assertTrue(row[0].startswith("'="), row[0])
            with pages_csv.open(newline="", encoding="utf-8-sig") as f:
                row = list(csv.reader(f))[1]
            self.assertTrue(row[0].startswith("'="), row[0])
            self.assertTrue(row[1].startswith("'="), row[1])


class ImageBombGuardTests(unittest.TestCase):
    def test_pillow_ceiling_is_bounded_not_disabled(self) -> None:
        import src.alto_ocr.alto_parse  # noqa: F401 — sets the constant
        from PIL import Image
        self.assertIsNotNone(Image.MAX_IMAGE_PIXELS,
                             "the decompression-bomb guard must stay armed")
        self.assertGreaterEqual(Image.MAX_IMAGE_PIXELS, 500_000_000,
                                "…but far above any real archival scan")


if __name__ == "__main__":
    unittest.main()
