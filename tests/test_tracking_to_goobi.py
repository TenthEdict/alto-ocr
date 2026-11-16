"""integration/goobi/tracking_to_goobi.py: tracking sheet -> Goobi import file.

The import file is consumed by a Java plugin we don't run here, so these
tests pin the properties it depends on: one sheet, the header names the
plugin config maps, every cell a text cell, and the process title
<collection>_<RefID>. Also the rows that must never become processes.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

from lxml import etree

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

KIT = Path(__file__).resolve().parent.parent / "integration" / "goobi"


def _load():
    spec = importlib.util.spec_from_file_location("tracking_to_goobi", KIT / "tracking_to_goobi.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


t2g = _load()

HASH_A = "b71fec945f83f7959d6ac87b5aca6fb2"
HASH_B = "630de4e13e677cc94046d3219d41bc49"
HASH_C = "0755957d10b031f6865344cc10b781be"


def _sheet(path: Path, header, rows, title="ProcessTracking", extra_first=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    if extra_first:
        ws.title = extra_first
        ws = wb.create_sheet(title)
    else:
        ws.title = title
    ws.append(header)
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows():
        for c in row:  # keep "=..." as typed text, as a person typing it would
            if isinstance(c.value, str) and c.value.startswith("="):
                c.data_type = "s"
    wb.save(path)
    return path


@unittest.skipIf(openpyxl is None, "openpyxl not installed")
class TrackingToGoobiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.meta = self.tmp / "metadata"  # empty: nothing exists in Goobi

    def run_main(self, *args):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = t2g.main([*map(str, args), "--goobi-metadata-root", str(self.meta)])
        return code, out.getvalue()

    def read_out(self, path):
        ws = openpyxl.load_workbook(path).worksheets[0]
        rows = list(ws.iter_rows())
        header = [c.value for c in rows[0]]
        return header, [{h: c.value for h, c in zip(header, r)} for r in rows[1:]], rows

    def test_layout_variants_fold_into_one_header_set(self):
        a = _sheet(self.tmp / "a.xlsx",
                   ["TN", "Folder", "Box Number", "Series", "Ref ID", "Title", "Notes", "Date Digitized"],
                   [[6920, 1, 103, "Family Papers", HASH_A, "Letters", "fragile", None]])
        b = _sheet(self.tmp / "b.xlsx",
                   ["TN", "Box", "Ref ID", "Title", "Archivist's Notes", "Date Digitized"],
                   [[7386, 108.0, HASH_B, "A.E. 310", "bound", None]])
        out = self.tmp / "out.xlsx"
        code, log = self.run_main(a, b, "--collection", "MSA00005", "-o", out)
        self.assertEqual(code, 0, log)
        header, rows, _ = self.read_out(out)
        self.assertEqual(header, list(t2g.FIELDS))
        self.assertEqual(rows[0]["ProcessTitle"], f"MSA00005_{HASH_A}")
        self.assertEqual(rows[0]["Box"], "103")
        self.assertEqual(rows[0]["Notes"], "fragile")
        self.assertEqual(rows[1]["Box"], "108")  # 108.0 loses its ".0"
        self.assertEqual(rows[1]["Notes"], "bound")
        self.assertEqual(rows[1]["Collection"], "MSA00005")
        self.assertIn("Folder", log)  # sheet b lacks it: reported, left blank

    def test_every_cell_is_text_even_formula_lookalikes(self):
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title"],
                   [[16354, "=SUM(A1)"], [HASH_A, "plain"], [HASH_B, "x"], [HASH_C, "y"]])
        out = self.tmp / "out.xlsx"
        self.assertEqual(self.run_main(a, "--collection", "MSA00005", "-o", out)[0], 0)
        _, rows, cells = self.read_out(out)
        self.assertEqual(rows[0]["Title"], "=SUM(A1)")
        self.assertEqual(rows[0]["RefID"], "16354")
        for row in cells[1:]:
            for c in row:
                if c.value is not None:
                    self.assertEqual(c.data_type, "s", c.coordinate)

    def test_rows_that_must_not_become_processes(self):
        meta = self.meta / "10" / "images" / f"MSA00005_{HASH_C}_master"
        meta.mkdir(parents=True)
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title", "Image Count", "Date Digitized"], [
            [HASH_A, "new", None, None],
            [HASH_B, "digitized", 74, dt.datetime(2026, 5, 22)],
            [HASH_C, "in goobi", None, None],
            [HASH_A, "repeat", None, None],
            [None, None, 1535, None],            # totals row
            ["bad/../id", "unsafe", None, None],
            ["abc", None, None, None],           # no title
        ])
        out = self.tmp / "out.xlsx"
        code, log = self.run_main(a, "--collection", "MSA00005", "-o", out)
        self.assertEqual(code, 0, log)
        _, rows, _ = self.read_out(out)
        self.assertEqual([r["RefID"] for r in rows], [HASH_A])
        for phrase in ("already a Goobi process", "already digitized", "Ref ID repeated",
                       "no Ref ID", "not usable as a folder name", "no Title"):
            self.assertIn(phrase, log)

        code, _ = self.run_main(a, "--collection", "MSA00005", "-o", out, "--include-started")
        _, rows, _ = self.read_out(out)
        self.assertEqual([r["RefID"] for r in rows], [HASH_A, HASH_B])

    def test_existing_title_read_from_meta_xml(self):
        proc = self.meta / "18"
        proc.mkdir(parents=True)
        (proc / "meta.xml").write_text(
            f'<x><goobi:metadata name="CatalogIDDigital">MSA00005_{HASH_A}</goobi:metadata></x>')
        self.assertIn(f"MSA00005_{HASH_A}", t2g.existing_titles(self.meta))

    def test_odd_shaped_ref_id_is_flagged_not_dropped(self):
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title"],
                   [[16354, "a"], [HASH_A, "b"], [HASH_B, "c"], [HASH_C, "d"]])
        out = self.tmp / "out.xlsx"
        code, log = self.run_main(a, "--collection", "MSA00005", "-o", out)
        self.assertEqual(code, 0)
        self.assertIn("CHECK", log)
        self.assertIn("16354", log.split("CHECK")[1])
        self.assertEqual(len(self.read_out(out)[1]), 4)

    def test_named_sheet_preferred_over_first(self):
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title"], [[HASH_A, "x"]], extra_first="Notes")
        out = self.tmp / "out.xlsx"
        self.assertEqual(self.run_main(a, "--collection", "MSA00005", "-o", out)[0], 0)

    def test_collection_must_be_a_prefix(self):
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title"], [[HASH_A, "x"]])
        for bad in ("MSA_5", "../x", ""):
            with self.assertRaises(SystemExit):
                self.run_main(a, "--collection", bad)

    def test_nothing_importable_exits_1_and_writes_nothing(self):
        a = _sheet(self.tmp / "a.xlsx", ["Ref ID", "Title", "Date Digitized"],
                   [[HASH_A, "x", dt.datetime(2026, 1, 1)]])
        out = self.tmp / "out.xlsx"
        code, _ = self.run_main(a, "--collection", "MSA00005", "-o", out)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


class PluginConfigTests(unittest.TestCase):
    """The plugin config maps exactly the columns the converter writes."""

    def test_header_names_match_converter(self):
        tree = etree.parse(str(KIT / "config" / "plugin_intranda_import_excel.xml"))
        headers = set(tree.xpath("//metadata/@headerName"))
        self.assertEqual(headers, set(t2g.FIELDS))
        self.assertEqual(tree.findtext(".//processTitleRule"), "ProcessTitle")
        self.assertEqual(tree.findtext(".//useOpac"), "false")
        self.assertEqual(tree.findtext(".//replaceExistingProcesses"), "false")

    def test_title_also_lands_as_property(self):
        # Mass import skips the creation form, which is what normally copies
        # TitleDocMain into the "Title" property shown in the Properties box.
        tree = etree.parse(str(KIT / "config" / "plugin_intranda_import_excel.xml"))
        title = tree.xpath("//metadata[@headerName='Title']")[0]
        self.assertEqual(title.get("ugh"), "TitleDocMain")
        self.assertEqual(title.get("property"), "Title")


if __name__ == "__main__":
    unittest.main()
