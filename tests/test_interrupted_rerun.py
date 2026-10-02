"""A re-run that dies part way must not lock the object against the next run.

A re-run overwrites ALTO pages in place one at a time and rebuilds the METS
only at the end. Before the fix, a run killed after page 1 (Ctrl+C, crash, the
GUI closed mid-object) left rewritten pages that no longer matched the old
METS checksums, so the next run was refused as "changed outside this tool" —
and the GUI has no force override, so the user was stuck. Worse, running
Repackage first would have stamped those engine-written pages as reviewed.

``process_ref_id`` now removes the previous METS once the guard has passed and
before the first page is rewritten, so an interrupted run leaves no stale
fingerprint. A genuine external edit is still caught: the guard runs first.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_interrupted_rerun
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from lxml import etree  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr._util import atomic_write_text  # noqa: E402
from src.alto_ocr.pipeline import process_ref_id  # noqa: E402
from src.alto_ocr.remediate import (  # noqa: E402
    has_review_provenance,
    pages_edited_since_mets,
    repackage_object,
)
from src.alto_ocr.walker import RefIdInputs, SourceFile  # noqa: E402

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"


class Engine:
    """Writes ALTO whose content names the run; optionally dies on a page."""

    name = "fake"

    def __init__(self, label: str, die_on: int | None = None) -> None:
        self.label = label
        self.die_on = die_on

    def adjust_preprocess(self, config):
        return config

    def ocr(self, image, output_base, *, dpi, page_index, source_filename=None):
        if page_index == self.die_on:
            raise KeyboardInterrupt
        base = Path(output_base)
        alto, txt = base.with_suffix(".xml"), base.with_suffix(".txt")
        atomic_write_text(
            alto,
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<alto xmlns="{ALTO_NS}">'
            "<Description><MeasurementUnit>mm10</MeasurementUnit>"
            "</Description>"
            '<Layout><Page ID="P1" HEIGHT="100" WIDTH="100" PC="0.90">'
            '<PrintSpace><TextBlock><TextLine>'
            f'<String ID="W1" CONTENT="{self.label}" WC="0.90" '
            'HPOS="1" VPOS="1" WIDTH="9" HEIGHT="4"/>'
            "</TextLine></TextBlock></PrintSpace></Page></Layout></alto>\n")
        atomic_write_text(txt, f"{self.label}\n")
        return alto, txt


class InterruptedRerunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        src = root / "src" / "RefA"
        src.mkdir(parents=True)
        sources = []
        for n in (1, 2, 3):
            tif = src / f"page{n}.tif"
            Image.fromarray(np.full((80, 60), 255, np.uint8)).save(
                tif, dpi=(300, 300))
            sources.append(SourceFile(path=tif, page_count=1))
        self.inputs = RefIdInputs(ref_id="RefA", source_dir=src,
                                  sources=tuple(sources))
        self.out_root = root / "out"
        self.obj = self.out_root / "RefA"
        self.mets = self.obj / "RefA.mets.xml"
        r1 = process_ref_id(self.inputs, self.out_root, engine=Engine("alpha"))
        self.assertTrue(r1.ok, msg=str(r1.failed_pages))
        self.assertTrue(self.mets.is_file())

    def _interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            process_ref_id(self.inputs, self.out_root,
                           engine=Engine("beta", die_on=2))

    def _content(self, stem: str) -> str:
        return (self.obj / "ALTO" / f"{stem}.xml").read_text(encoding="utf-8")

    def test_interrupted_run_leaves_no_drift(self) -> None:
        self._interrupt()
        self.assertIn("beta", self._content("page1"),
                      "precondition: page 1 was rewritten before the crash")
        self.assertEqual(pages_edited_since_mets(self.obj), [])

    def test_next_run_after_interruption_succeeds(self) -> None:
        self._interrupt()
        r3 = process_ref_id(self.inputs, self.out_root, engine=Engine("gamma"))
        self.assertTrue(r3.ok, msg=str(r3.failed_pages))
        for stem in ("page1", "page2", "page3"):
            self.assertIn("gamma", self._content(stem))
        self.assertTrue(self.mets.is_file(), "METS rebuilt at the end")

    def test_repackage_after_interruption_stamps_nothing(self) -> None:
        self._interrupt()
        repackage_object(self.obj)
        for stem in ("page1", "page2", "page3"):
            self.assertFalse(
                has_review_provenance(self.obj / "ALTO" / f"{stem}.xml"),
                f"{stem}: engine output must not be stamped as reviewed")

    def test_real_external_edit_still_refused(self) -> None:
        alto = self.obj / "ALTO" / "page2.xml"
        tree = etree.parse(str(alto))
        tree.getroot().find(".//{*}String").set("CONTENT", "corrected")
        tree.write(str(alto), xml_declaration=True, encoding="UTF-8")
        r = process_ref_id(self.inputs, self.out_root, engine=Engine("gamma"))
        self.assertFalse(r.ok)
        self.assertIn("outside this tool", r.pages[0].error)
        self.assertIn("corrected", self._content("page2"))
        self.assertTrue(self.mets.is_file(),
                        "a refused run must not remove the fingerprint")


if __name__ == "__main__":
    unittest.main()
