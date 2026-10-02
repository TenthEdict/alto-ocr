"""Tests for the confidence triage report (worst-first remediation queue).

Covers: per-page classification (scored / unscored / empty / failed) from
real-shaped ALTO, exact word-weighted object aggregation, worst-first
ordering at both object and page level, CSV emission, and the batch-summary
integration (``_scan_confidence`` / ``_summary_dict``).

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_triage
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.pipeline import ALTO_FAILURE_STUB, PageResult, RefIdResult  # noqa: E402
from src.alto_ocr.triage import (  # noqa: E402
    scan_alto_file,
    scan_alto_file_safe,
    scan_object,
    scan_output_root,
    worst_pages,
    write_csvs,
)

_NS = 'xmlns="http://www.loc.gov/standards/alto/ns-v2#"'


def _alto(page_attrs: str, strings: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alto {_NS}>
\t<Description><MeasurementUnit>mm10</MeasurementUnit></Description>
\t<Layout>
\t\t<Page ID="P1" {page_attrs} HEIGHT="2794" WIDTH="2159">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20">
{strings}
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""


def _string(content: str, wc: float | None) -> str:
    wc_attr = f' WC="{wc:.2f}"' if wc is not None else ""
    return (f'\t\t\t\t\t\t<String ID="s" HPOS="0" VPOS="0" WIDTH="10" '
            f'HEIGHT="10" CONTENT="{content}"{wc_attr}/>')


def _write_tree(root: Path) -> None:
    """RefA: 2 scored pages (one good, one bad) + 1 empty + 1 failed stub.
    RefB: 1 unscored (GLMOCR-style, no WC/PC). RefC: no ALTO dir."""
    a = root / "RefA" / "ALTO"
    a.mkdir(parents=True)
    (a / "page001.xml").write_text(_alto(
        'PHYSICAL_IMG_NR="1" PC="0.95"',
        "\n".join([_string("Board", 0.96), _string("of", 0.97), _string("Minutes", 0.94)]),
    ), encoding="utf-8")
    (a / "page002.xml").write_text(_alto(
        'PHYSICAL_IMG_NR="2" PC="0.40"',
        "\n".join([_string("garble", 0.35), _string("noise", 0.45)]),
    ), encoding="utf-8")
    # Empty page: Page element present, zero Strings (blank-page ALTO).
    (a / "page003.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<alto {_NS}>
\t<Description><MeasurementUnit>mm10</MeasurementUnit></Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="3" HEIGHT="2794" WIDTH="2159"/>
\t</Layout>
</alto>
""", encoding="utf-8")
    # Failure stub exactly as the pipeline writes it.
    (a / "page004.xml").write_text(
        ALTO_FAILURE_STUB.format(when="2026-07-30T00:00:00+00:00", reason="boom"),
        encoding="utf-8")

    b = root / "RefB" / "ALTO"
    b.mkdir(parents=True)
    (b / "page001.xml").write_text(_alto(
        'PHYSICAL_IMG_NR="1"',
        "\n".join([_string("hand", None), _string("written", None)]),
    ), encoding="utf-8")

    (root / "RefC").mkdir()  # no ALTO dir — must be skipped
    (root / "stray.txt").write_text("not an object", encoding="utf-8")


class ScanAltoFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write_tree(self.root)

    def test_page_classification(self) -> None:
        a = self.root / "RefA" / "ALTO"
        self.assertEqual(scan_alto_file(a / "page001.xml").status, "scored")
        self.assertEqual(scan_alto_file(a / "page003.xml").status, "empty")
        self.assertEqual(scan_alto_file(a / "page004.xml").status, "failed")
        b = self.root / "RefB" / "ALTO"
        self.assertEqual(scan_alto_file(b / "page001.xml").status, "unscored")

    def test_safe_scan_classifies_unreadable_alto_as_failed(self) -> None:
        bad = self.root / "RefA" / "ALTO" / "truncated.xml"
        bad.write_text("<alto><Layout", encoding="utf-8")
        p = scan_alto_file_safe(bad)
        self.assertEqual((p.stem, p.status, p.pc, p.words), ("truncated", "failed", None, 0))
        good = self.root / "RefA" / "ALTO" / "page002.xml"
        self.assertEqual(scan_alto_file_safe(good), scan_alto_file(good))

    def test_scored_page_facts(self) -> None:
        p = scan_alto_file(self.root / "RefA" / "ALTO" / "page002.xml")
        self.assertEqual(p.pc, 0.40)
        self.assertEqual(p.words, 2)
        self.assertEqual(p.scored_words, 2)
        self.assertEqual(p.low_words, 2)  # both < 0.50
        self.assertEqual(p.pct_low, 100.0)

    def test_low_wc_threshold_is_a_dial(self) -> None:
        p = scan_alto_file(self.root / "RefA" / "ALTO" / "page002.xml", low_wc=0.40)
        self.assertEqual(p.low_words, 1)  # only 0.35 is below 0.40


class ObjectAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write_tree(self.root)

    def test_object_aggregates(self) -> None:
        o = scan_object(self.root / "RefA")
        self.assertEqual((o.page_count, o.scored_pages, o.empty_pages, o.failed_pages),
                         (4, 2, 1, 1))
        self.assertEqual(o.total_words, 5)
        self.assertEqual(o.low_words, 2)
        # Word-weighted: (0.96+0.97+0.94+0.35+0.45)/5, not mean of page PCs.
        self.assertAlmostEqual(o.mean_wc, 0.734, places=6)
        self.assertEqual(o.min_pc, 0.40)
        self.assertEqual(o.min_pc_stem, "page002")
        self.assertEqual([p.stem for p in o.review_pages], ["page002"])

    def test_object_without_alto_dir_is_none(self) -> None:
        self.assertIsNone(scan_object(self.root / "RefC"))

    def test_output_root_scan_sorts_worst_first_and_skips_non_objects(self) -> None:
        objects = scan_output_root(self.root)
        # RefA (mean 0.734) ranks before RefB (no data); RefC/stray skipped.
        self.assertEqual([o.ref_id for o in objects], ["RefA", "RefB"])
        self.assertEqual(objects[1].unscored_pages, 1)
        self.assertIsNone(objects[1].mean_wc)

    def test_single_object_root(self) -> None:
        objects = scan_output_root(self.root / "RefA")
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].page_count, 4)

    def test_worst_pages_ordering(self) -> None:
        rows = worst_pages(scan_output_root(self.root))
        self.assertEqual(
            [(r, p.stem, p.status) for r, p in rows],
            [
                ("RefA", "page004", "failed"),    # stub first: no transcript at all
                ("RefA", "page002", "scored"),    # then scored by PC ascending
                ("RefA", "page001", "scored"),
                ("RefB", "page001", "unscored"),  # GLMOCR pages after scored
                ("RefA", "page003", "empty"),     # blanks last
            ],
        )


class CsvAndSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write_tree(self.root)

    def test_write_csvs(self) -> None:
        objects = scan_output_root(self.root)
        obj_csv, pages_csv = write_csvs(objects, self.root)

        with obj_csv.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][0], "ref_id")
        self.assertEqual(rows[1][0], "RefA")  # worst-first
        self.assertEqual(rows[1][9], "0.734")  # mean_wc column

        with pages_csv.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][:3], ["RefA", "page004", "failed"])
        self.assertEqual(len(rows), 1 + 5)  # header + every page

    def test_batch_summary_integration(self) -> None:
        from src.alto_ocr.main import _scan_confidence, _summary_dict

        results = [
            RefIdResult(ref_id="RefA", output_dir=self.root / "RefA",
                        pages=[PageResult(stem="page001", order=1, ok=True)]),
            RefIdResult(ref_id="RefB", output_dir=self.root / "RefB",
                        pages=[PageResult(stem="page001", order=1, ok=True)]),
            RefIdResult(ref_id="RefC", output_dir=self.root / "RefC",
                        pages=[PageResult(stem="x", order=1, ok=False, error="nope")]),
        ]
        conf = _scan_confidence(results)
        self.assertIsNone(conf["RefC"])

        s = _summary_dict(results, conf)
        self.assertEqual(s["mean_wc"], 0.734)
        self.assertEqual(s["review_page_total"], 1)
        self.assertEqual(s["unscored_page_total"], 1)
        by_ref = {e["ref_id"]: e for e in s["ref_ids"]}
        self.assertEqual(by_ref["RefA"]["confidence"]["review_pages"], 1)
        self.assertEqual(by_ref["RefA"]["confidence"]["min_page"], "page002")
        self.assertIsNone(by_ref["RefC"]["confidence"])

    def test_summary_dict_without_conf_is_unchanged(self) -> None:
        from src.alto_ocr.main import _summary_dict

        results = [RefIdResult(ref_id="RefA", output_dir=self.root / "RefA",
                               pages=[PageResult(stem="p", order=1, ok=True)])]
        s = _summary_dict(results)
        self.assertNotIn("mean_wc", s)
        self.assertNotIn("confidence", s["ref_ids"][0])


if __name__ == "__main__":
    unittest.main()
