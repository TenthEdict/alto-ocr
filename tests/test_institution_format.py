"""Tests for ``alto_format.convert_to_institution_format`` — the output contract.

This converter produces the institution's reference ALTO (the thing the
public archive actually consumes) and had no direct tests before the
2026-07-30 review. Locked in here: v3→v2 namespace + schemaLocation, px→mm10
coordinate conversion, margin derivation with edge clamping, sequential
element renumbering, sourceImageInformation, reference attribute order,
WC/PC survival, and idempotence (byte-identical on a second pass).

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_institution_format
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
    px_to_mm10,
)

# 2400x3000 px page @300dpi; one block spanning x 300..1800, y 600..1500.
V3_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
<Description><MeasurementUnit>pixel</MeasurementUnit></Description>
<Layout>
<Page WIDTH="2400" HEIGHT="3000" PHYSICAL_IMG_NR="0" ID="page_0" PC="0.71">
<PrintSpace HPOS="0" VPOS="0" WIDTH="2400" HEIGHT="3000">
<TextBlock ID="b0" HPOS="300" VPOS="600" WIDTH="1500" HEIGHT="900">
<TextLine ID="l0" HPOS="300" VPOS="600" WIDTH="1500" HEIGHT="60">
<String ID="w0" HPOS="300" VPOS="600" WIDTH="450" HEIGHT="60" CONTENT="Board" WC="0.87"/>
<SP ID="s0" HPOS="750" VPOS="600" WIDTH="30"/>
<String ID="w1" HPOS="780" VPOS="600" WIDTH="450" HEIGHT="60" CONTENT="Minutes" WC="0.55"/>
</TextLine>
</TextBlock>
</PrintSpace>
</Page>
</Layout>
</alto>
"""


def _box(el: etree._Element) -> tuple[str, str, str, str]:
    return (el.get("HPOS"), el.get("VPOS"), el.get("WIDTH"), el.get("HEIGHT"))


def _one(root: etree._Element, tag: str) -> etree._Element:
    return next(root.iter(f"{{*}}{tag}"))


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "page.xml"
        self.path.write_text(V3_ALTO, encoding="utf-8")

    def _convert(self, **kw) -> etree._Element:
        convert_to_institution_format(
            self.path, kw.pop("page_index", 3), dpi=kw.pop("dpi", 300),
            source_filename=kw.pop("source_filename", "scan 003.tif"), **kw,
        )
        return etree.parse(str(self.path)).getroot()

    def test_v3_pixels_become_institution_v2_mm10(self) -> None:
        root = self._convert()
        self.assertEqual(etree.QName(root).namespace, ALTO_NS_V2)
        self.assertIn(ALTO_NS_V2, root.get(
            "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"))
        self.assertEqual(_one(root, "MeasurementUnit").text, "mm10")
        self.assertEqual(_one(root, "fileName").text, "./scan 003.tif")

        page = _one(root, "Page")
        self.assertEqual(page.get("ID"), "P3")
        self.assertEqual(page.get("PHYSICAL_IMG_NR"), "3")
        # 2400px @300dpi -> 2032 mm10; 3000 -> 2540.
        self.assertEqual((page.get("WIDTH"), page.get("HEIGHT")), ("2032", "2540"))

        # PrintSpace shrinks to the content bbox: (300,600,1500,900) px.
        ps = _one(root, "PrintSpace")
        self.assertEqual(_box(ps), ("254", "508", "1270", "762"))

        # Word geometry converts; WC survives untouched; PC survives.
        w0 = next(root.iter("{*}String"))
        self.assertEqual(_box(w0), ("254", "508", "381", "51"))
        self.assertEqual(w0.get("WC"), "0.87")
        self.assertEqual(page.get("PC"), "0.71")

        # Sequential, type-prefixed IDs.
        self.assertEqual(ps.get("ID"), "P3_PS00001")
        self.assertEqual(_one(root, "TextBlock").get("ID"), "P3_TB00001")
        self.assertEqual(_one(root, "TextLine").get("ID"), "P3_TL00001")
        self.assertEqual([s.get("ID") for s in root.iter("{*}String")],
                         ["P3_ST00001", "P3_ST00002"])
        self.assertEqual(_one(root, "SP").get("ID"), "P3_SP00001")

    def test_margins_derived_from_content_bbox(self) -> None:
        root = self._convert()
        margins = {etree.QName(el).localname: el for el in root.iter()
                   if etree.QName(el).localname.endswith("Margin")}
        self.assertEqual(set(margins), {"TopMargin", "LeftMargin",
                                        "RightMargin", "BottomMargin"})
        self.assertEqual(_box(margins["TopMargin"]), ("0", "0", "2032", "508"))
        self.assertEqual(_box(margins["LeftMargin"]), ("0", "508", "254", "762"))
        # Content ends at x=1800px (1524 mm10); right margin spans to page edge.
        self.assertEqual(_box(margins["RightMargin"]), ("1524", "508", "508", "762"))
        self.assertEqual(_box(margins["BottomMargin"]), ("0", "1270", "2032", "1270"))
        self.assertEqual(margins["TopMargin"].get("ID"), "P3_TM00001")

    def test_reference_attribute_order(self) -> None:
        root = self._convert()
        w0 = next(root.iter("{*}String"))
        self.assertEqual(list(w0.attrib)[:7],
                         ["ID", "HPOS", "VPOS", "WIDTH", "HEIGHT", "CONTENT", "WC"])
        page = _one(root, "Page")
        self.assertEqual(list(page.attrib),
                         ["ID", "PHYSICAL_IMG_NR", "HEIGHT", "WIDTH", "PC"])

    def test_idempotent_second_pass_is_byte_identical(self) -> None:
        self._convert()
        first = self.path.read_bytes()
        self._convert()
        self.assertEqual(self.path.read_bytes(), first)

    def test_content_leaking_past_page_edge_is_clamped(self) -> None:
        # Tesseract sometimes emits blocks past the page edge on noisy scans;
        # margins must clamp to zero-width, never go negative.
        leaked = (V3_ALTO
                  .replace('HPOS="300" VPOS="600" WIDTH="1500" HEIGHT="900"',
                           'HPOS="2200" VPOS="600" WIDTH="400" HEIGHT="900"'))
        self.path.write_text(leaked, encoding="utf-8")
        root = self._convert()
        for el in root.iter():
            for attr in ("HPOS", "VPOS", "WIDTH", "HEIGHT"):
                v = el.get(attr)
                if v is not None:
                    self.assertGreaterEqual(int(v), 0,
                                            f"{etree.QName(el).localname}@{attr}")
        rm = _one(root, "RightMargin")
        self.assertEqual(rm.get("WIDTH"), "0")

    def test_px_to_mm10_math(self) -> None:
        self.assertEqual(px_to_mm10(300, 300), 254)
        self.assertEqual(px_to_mm10(60, 300), 51)   # 50.8 rounds up
        self.assertEqual(px_to_mm10(0, 300), 0)
        self.assertEqual(px_to_mm10(100, 0), 100, "bad dpi passes through")


if __name__ == "__main__":
    unittest.main()
