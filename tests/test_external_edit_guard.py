"""Protecting corrections made by editors that leave no provenance.

The re-OCR guard originally recognised one signal: ``<Processing ID="review">``,
which only this tool's own review GUI writes. Goobi workflow's built-in ALTO
editor (``AltoSaver.saveAltoChanges``) sets ``CONTENT`` and nothing else — no
Processing element, and ``WC`` left at its original value — so a page corrected
there was indistinguishable from an untouched one and the next OCR run
destroyed the work silently.

The second signal is METS checksum drift: the METS records the MD5 of every
ALTO file the pipeline packaged, so bytes that no longer match were changed by
something else. Because ``repackage`` refreshes those checksums, drift alone
would protect a page only until the next repackage — so repackage converts what
it finds into a durable provenance stamp.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_external_edit_guard
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from lxml import etree  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr.pipeline import process_ref_id  # noqa: E402
from src.alto_ocr.remediate import (  # noqa: E402
    has_review_provenance,
    mets_alto_checksums,
    pages_edited_since_mets,
    repackage_object,
    stamp_external_review,
)
from src.alto_ocr.walker import RefIdInputs, SourceFile  # noqa: E402

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"


class FakeEngine:
    """Writes ALTO with the Description/Page structure the stamp needs."""

    name = "fake"

    def adjust_preprocess(self, config):
        return config

    def ocr(self, image, output_base, *, dpi, page_index, source_filename=None):
        base = Path(output_base)
        alto, txt = base.with_suffix(".xml"), base.with_suffix(".txt")
        alto.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<alto xmlns="{ALTO_NS}">'
            "<Description><MeasurementUnit>mm10</MeasurementUnit>"
            "</Description>"
            '<Layout><Page ID="P1" HEIGHT="100" WIDTH="100" PC="0.90">'
            '<PrintSpace><TextBlock><TextLine>'
            '<String ID="W1" CONTENT="original" WC="0.90" '
            'HPOS="1" VPOS="1" WIDTH="9" HEIGHT="4"/>'
            "</TextLine></TextBlock></PrintSpace></Page></Layout></alto>\n",
            encoding="utf-8")
        txt.write_text("original\n", encoding="utf-8")
        return alto, txt


class ExternalEditGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.src = root / "src" / "RefA"
        self.src.mkdir(parents=True)
        tif = self.src / "page1.tif"
        Image.fromarray(np.full((80, 60), 255, np.uint8)).save(tif, dpi=(300, 300))
        self.inputs = RefIdInputs(
            ref_id="RefA", source_dir=self.src,
            sources=(SourceFile(path=tif, page_count=1),))
        self.out_root = root / "out"
        self.obj = self.out_root / "RefA"
        # A first run, WITH a METS — that is the fingerprint store.
        r = process_ref_id(self.inputs, self.out_root, engine=FakeEngine())
        self.assertTrue(r.ok, msg=str(r.failed_pages))
        self.alto = self.obj / "ALTO" / "page1.xml"

    def _goobi_style_edit(self) -> None:
        """CONTENT only: exactly what Goobi's AltoSaver writes."""
        tree = etree.parse(str(self.alto))
        tree.getroot().find(".//{*}String").set("CONTENT", "corrected")
        tree.write(str(self.alto), xml_declaration=True, encoding="UTF-8")

    def _rerun(self):
        return process_ref_id(self.inputs, self.out_root, engine=FakeEngine())

    # -- the fingerprint itself ------------------------------------------
    def test_mets_records_alto_checksums(self) -> None:
        sums = mets_alto_checksums(self.obj / "RefA.mets.xml")
        self.assertEqual(list(sums), ["page1"])
        self.assertRegex(sums["page1"], r"^[0-9a-f]{32}$")

    def test_untouched_output_shows_no_drift(self) -> None:
        self.assertEqual(pages_edited_since_mets(self.obj), [])

    def test_edit_is_detected(self) -> None:
        self._goobi_style_edit()
        self.assertEqual(pages_edited_since_mets(self.obj), ["page1"])

    def test_no_mets_means_no_detection(self) -> None:
        # Honest degradation: --no-mets runs have no fingerprint to compare.
        (self.obj / "RefA.mets.xml").unlink()
        self._goobi_style_edit()
        self.assertEqual(pages_edited_since_mets(self.obj), [])

    # -- the guard -------------------------------------------------------
    def test_guard_refuses_a_provenance_less_edit(self) -> None:
        self._goobi_style_edit()
        self.assertFalse(has_review_provenance(self.alto),
                         "precondition: a Goobi edit leaves no provenance")
        result = self._rerun()
        self.assertFalse(result.ok)
        self.assertIn("outside this tool", result.pages[0].error)
        self.assertIn("corrected", self.alto.read_text(encoding="utf-8"))

    def test_force_reocr_still_overrides(self) -> None:
        self._goobi_style_edit()
        result = process_ref_id(self.inputs, self.out_root, engine=FakeEngine(),
                                force_reocr=True)
        self.assertTrue(result.ok)
        self.assertNotIn("corrected", self.alto.read_text(encoding="utf-8"))

    # -- durability across repackage -------------------------------------
    def test_repackage_converts_drift_into_provenance(self) -> None:
        self._goobi_style_edit()
        r = repackage_object(self.obj)
        self.assertEqual(r.externally_edited, ["page1"])
        self.assertTrue(has_review_provenance(self.alto))
        # METS refreshed, so the byte-level signal is gone by design...
        self.assertEqual(pages_edited_since_mets(self.obj), [])
        # ...but protection survives on the stamp.
        self.assertFalse(self._rerun().ok)
        self.assertIn("corrected", self.alto.read_text(encoding="utf-8"))

    def test_repackage_does_not_stamp_untouched_pages(self) -> None:
        # Repackage runs on clean objects too (canonicalising transcripts);
        # stamping everything would block all future re-OCR.
        r = repackage_object(self.obj)
        self.assertEqual(r.externally_edited, [])
        self.assertFalse(has_review_provenance(self.alto))
        self.assertTrue(self._rerun().ok)

    def test_stamp_leaves_our_own_provenance_alone(self) -> None:
        # Our GUI's record carries an edit count; the inferred one cannot.
        self._goobi_style_edit()
        self.assertTrue(stamp_external_review(self.alto))
        before = self.alto.read_text(encoding="utf-8")
        self.assertFalse(stamp_external_review(self.alto))
        self.assertEqual(self.alto.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
