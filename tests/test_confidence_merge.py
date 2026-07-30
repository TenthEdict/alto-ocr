"""Tests for the Tesseract TSV → ALTO confidence merge (WC/PC attributes).

Tesseract's ``alto`` renderer emits no confidence values, so the engine runs
one recognition pass with ``alto tsv txt`` configs and merges the TSV's
per-word confidences into the ALTO (``String/@WC``, mean → ``Page/@PC``).
These tests lock in the bbox-matching rules and that the added attributes
survive institution-format conversion (px → mm10 + namespace swap), since
the remediation workflow triages on them downstream.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_confidence_merge
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.alto_format import (  # noqa: E402
    ALTO_NS_V2,
    convert_to_institution_format,
)
from src.alto_ocr.ocr import _merge_tsv_confidence  # noqa: E402

_RAW_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
\t<Description>
\t\t<MeasurementUnit>pixel</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page WIDTH="2400" HEIGHT="3000" PHYSICAL_IMG_NR="0" ID="page_0">
\t\t\t<PrintSpace HPOS="0" VPOS="0" WIDTH="2400" HEIGHT="3000">
\t\t\t\t<TextBlock ID="block_0" HPOS="100" VPOS="200" WIDTH="800" HEIGHT="60">
\t\t\t\t\t<TextLine ID="line_0" HPOS="100" VPOS="200" WIDTH="800" HEIGHT="60">
\t\t\t\t\t\t<String ID="string_0" HPOS="100" VPOS="200" WIDTH="200" HEIGHT="50" CONTENT="Board"/>
\t\t\t\t\t\t<SP WIDTH="20" VPOS="200" HPOS="300"/>
\t\t\t\t\t\t<String ID="string_1" HPOS="320" VPOS="200" WIDTH="240" HEIGHT="50" CONTENT="Minutes"/>
\t\t\t\t\t\t<String ID="string_2" HPOS="600" VPOS="200" WIDTH="150" HEIGHT="50" CONTENT="1927"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
    "\tleft\ttop\twidth\theight\tconf\ttext"
)

# Word rows (level 5) for string_0 and string_1; string_2 deliberately has no
# row. Level-1 page row (conf -1) must be ignored.
_TSV = "\n".join([
    _TSV_HEADER,
    "1\t1\t0\t0\t0\t0\t0\t0\t2400\t3000\t-1\t",
    "5\t1\t1\t1\t1\t1\t100\t200\t200\t50\t96.394\tBoard",
    "5\t1\t1\t1\t1\t2\t320\t200\t240\t50\t51.2\tMinutes",
]) + "\n"


def _strings_by_id(root: etree._Element) -> dict[str, etree._Element]:
    return {
        el.get("ID"): el
        for el in root.iter()
        if etree.QName(el).localname == "String"
    }


class MergeTsvConfidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.alto = self.dir / "page.xml"
        self.tsv = self.dir / "page.tsv"
        self.alto.write_text(_RAW_ALTO, encoding="utf-8")
        self.tsv.write_text(_TSV, encoding="utf-8")

    def test_wc_set_on_bbox_matches_only(self) -> None:
        _merge_tsv_confidence(self.alto, self.tsv)
        strings = _strings_by_id(etree.parse(str(self.alto)).getroot())
        self.assertEqual(strings["string_0"].get("WC"), "0.96")
        self.assertEqual(strings["string_1"].get("WC"), "0.51")
        self.assertIsNone(strings["string_2"].get("WC"), "unmatched bbox must not gain WC")

    def test_page_pc_is_mean_word_confidence(self) -> None:
        _merge_tsv_confidence(self.alto, self.tsv)
        root = etree.parse(str(self.alto)).getroot()
        page = next(el for el in root.iter() if etree.QName(el).localname == "Page")
        # mean(96.394, 51.2) = 73.797 → 0.74. The level-1 row's -1 is excluded.
        self.assertEqual(page.get("PC"), "0.74")

    def test_tsv_without_word_rows_adds_no_wc_or_pc(self) -> None:
        self.tsv.write_text(_TSV_HEADER + "\n1\t1\t0\t0\t0\t0\t0\t0\t2400\t3000\t-1\t\n",
                            encoding="utf-8")
        _merge_tsv_confidence(self.alto, self.tsv)
        root = etree.parse(str(self.alto)).getroot()
        page = next(el for el in root.iter() if etree.QName(el).localname == "Page")
        self.assertIsNone(page.get("PC"))
        self.assertIsNone(_strings_by_id(root)["string_0"].get("WC"))

    def test_wc_pc_survive_institution_format_conversion(self) -> None:
        _merge_tsv_confidence(self.alto, self.tsv)
        convert_to_institution_format(
            self.alto, 1, dpi=300, source_filename="page.tif",
        )
        root = etree.parse(str(self.alto)).getroot()
        self.assertEqual(root.nsmap.get(None), ALTO_NS_V2)

        strings = _strings_by_id(root)  # IDs rewritten to P1_ST0000N by conversion
        by_content = {el.get("CONTENT"): el for el in strings.values()}
        self.assertEqual(by_content["Board"].get("WC"), "0.96")
        self.assertEqual(by_content["Minutes"].get("WC"), "0.51")
        self.assertIsNone(by_content["1927"].get("WC"))
        # Coordinates converted to mm10 (100 px @ 300 dpi → 85), WC untouched.
        self.assertEqual(by_content["Board"].get("HPOS"), "85")

        page = next(el for el in root.iter() if etree.QName(el).localname == "Page")
        self.assertEqual(page.get("PC"), "0.74")
        # Reference attribute order puts WC right after CONTENT.
        self.assertEqual(
            list(by_content["Board"].attrib)[:7],
            ["ID", "HPOS", "VPOS", "WIDTH", "HEIGHT", "CONTENT", "WC"],
        )

    def test_duplicate_bboxes_pair_in_document_order(self) -> None:
        alto = _RAW_ALTO.replace(
            'HPOS="320" VPOS="200" WIDTH="240" HEIGHT="50"',
            'HPOS="100" VPOS="200" WIDTH="200" HEIGHT="50"',
        )
        self.alto.write_text(alto, encoding="utf-8")
        tsv = "\n".join([
            _TSV_HEADER,
            "5\t1\t1\t1\t1\t1\t100\t200\t200\t50\t90\tBoard",
            "5\t1\t1\t1\t1\t2\t100\t200\t200\t50\t30\tMinutes",
        ]) + "\n"
        self.tsv.write_text(tsv, encoding="utf-8")
        _merge_tsv_confidence(self.alto, self.tsv)
        strings = _strings_by_id(etree.parse(str(self.alto)).getroot())
        self.assertEqual(strings["string_0"].get("WC"), "0.90")
        self.assertEqual(strings["string_1"].get("WC"), "0.30")


if __name__ == "__main__":
    unittest.main()
