"""Guards that protect hand-corrected ALTO from being silently destroyed.

Two independent guards, one theme — corrected ALTO is the most expensive
artifact this tool produces (human hours per page):

* **Re-OCR guard** (``pipeline.process_ref_id``): an object whose existing
  output contains ALTO with ``<Processing ID="review">`` provenance (stamped
  by ``ReviewPage.save``) is refused — returned as a failed result with the
  reason, no file touched — unless ``force_reocr=True`` (CLI
  ``--force-reocr``). Failure stubs (``Processing ID="failed"``) must NOT
  trip it: re-running failed pages is the normal workflow.
* **Concurrent-edit guard** (``ReviewPage.save``): raises
  ``ConcurrentEditError`` when the ALTO changed on disk after the page was
  loaded (second review session / external editor), so a save can't silently
  discard the other party's work; ``save(force=True)`` overrides, and a
  successful save re-baselines the check.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_overwrite_guards
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr.pipeline import process_ref_id  # noqa: E402
from src.alto_ocr.remediate import (  # noqa: E402
    ConcurrentEditError,
    ReviewPage,
    has_review_provenance,
)
from src.alto_ocr.walker import RefIdInputs, SourceFile  # noqa: E402

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"

REVIEWED_ALTO = f"""<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="{ALTO_NS}">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t\t<Processing ID="review">
\t\t\t<processingDateTime>2026-07-30T00:00:00+00:00</processingDateTime>
\t\t\t<processingAgency>alto_ocr</processingAgency>
\t\t\t<processingStepDescription>human review: 3 word(s) corrected</processingStepDescription>
\t\t</Processing>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="100">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Corrected" WC="1.00"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

PLAIN_ALTO = f"""<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="{ALTO_NS}">
\t<Description><MeasurementUnit>mm10</MeasurementUnit></Description>
\t<Layout>
\t\t<Page ID="P1" HEIGHT="2794" WIDTH="2159">
\t\t\t<PrintSpace ID="PS1" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="100">
\t\t\t\t\t<TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<String ID="S1" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Original" WC="0.40"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

FAILED_STUB_ALTO = f"""<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="{ALTO_NS}">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t\t<Processing ID="failed">
\t\t\t<processingStepDescription>OCR failed: boom</processingStepDescription>
\t\t</Processing>
\t</Description>
\t<Layout/>
</alto>
"""

PREFIXED_REVIEWED_ALTO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<a:alto xmlns:a="{ALTO_NS}"><a:Description>'
    '<a:Processing ID="review"/></a:Description><a:Layout/></a:alto>\n'
)


class FakeEngine:
    """Minimal OCREngine that writes a distinctive ALTO + txt per page."""

    name = "fake"
    MARKER = "FAKE-OCR-OUTPUT"

    def adjust_preprocess(self, config):
        return config

    def ocr(self, image, output_base, *, dpi, page_index, source_filename=None):
        output_base = Path(output_base)
        alto = output_base.with_suffix(".xml")
        txt = output_base.with_suffix(".txt")
        alto.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<alto xmlns="{ALTO_NS}"><!-- {self.MARKER} -->'
            "<Description><MeasurementUnit>mm10</MeasurementUnit></Description>"
            '<Layout><Page ID="P1" HEIGHT="10" WIDTH="10"/></Layout></alto>\n',
            encoding="utf-8",
        )
        txt.write_text(f"{self.MARKER}\n", encoding="utf-8")
        return alto, txt


class HasReviewProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, content: str) -> Path:
        p = self.dir / "page.xml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_reviewed_alto_detected(self) -> None:
        self.assertTrue(has_review_provenance(self._write(REVIEWED_ALTO)))

    def test_plain_alto_not_detected(self) -> None:
        self.assertFalse(has_review_provenance(self._write(PLAIN_ALTO)))

    def test_failure_stub_not_detected(self) -> None:
        # Re-running failed pages is the normal workflow — must not guard.
        self.assertFalse(has_review_provenance(self._write(FAILED_STUB_ALTO)))

    def test_prefixed_namespace_detected(self) -> None:
        # External editors may emit prefixed ALTO; corrections still count.
        self.assertTrue(has_review_provenance(self._write(PREFIXED_REVIEWED_ALTO)))

    def test_unreadable_file_is_not_guarded(self) -> None:
        # A corrupt file's cure is exactly a re-run.
        self.assertFalse(has_review_provenance(self._write("<alto unclosed")))


class ReocrGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.src_dir = root / "src" / "RefA"
        self.src_dir.mkdir(parents=True)
        self.out_root = root / "out"
        self.alto_dir = self.out_root / "RefA" / "ALTO"
        self.alto_dir.mkdir(parents=True)

    def _inputs(self, *, real_tif: bool) -> RefIdInputs:
        tif = self.src_dir / "scan1.tif"
        if real_tif:
            Image.fromarray(np.full((80, 60), 255, np.uint8)).save(
                tif, dpi=(300, 300))
        return RefIdInputs(
            ref_id="RefA", source_dir=self.src_dir,
            sources=(SourceFile(path=tif, page_count=1),),
        )

    def test_reviewed_object_is_refused_untouched(self) -> None:
        reviewed = self.alto_dir / "scan1.xml"
        reviewed.write_text(REVIEWED_ALTO, encoding="utf-8")
        # The source TIF doesn't even need to exist: the guard fires first.
        result = process_ref_id(
            self._inputs(real_tif=False), self.out_root,
            engine=FakeEngine(), write_mets_file=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_page_count, 1,
                         "guard must report the object's full page count")
        self.assertIn("review", result.pages[0].error)
        self.assertIn("--force-reocr", result.pages[0].error)
        self.assertEqual(reviewed.read_text(encoding="utf-8"), REVIEWED_ALTO,
                         "the corrected ALTO must not be touched")

    def test_force_reocr_overwrites(self) -> None:
        reviewed = self.alto_dir / "scan1.xml"
        reviewed.write_text(REVIEWED_ALTO, encoding="utf-8")
        result = process_ref_id(
            self._inputs(real_tif=True), self.out_root,
            engine=FakeEngine(), write_mets_file=False, force_reocr=True,
        )
        self.assertTrue(result.ok)
        content = reviewed.read_text(encoding="utf-8")
        self.assertIn(FakeEngine.MARKER, content)
        self.assertNotIn('Processing ID="review"', content)

    def test_unreviewed_object_proceeds_without_force(self) -> None:
        stale = self.alto_dir / "scan1.xml"
        stale.write_text(PLAIN_ALTO, encoding="utf-8")
        result = process_ref_id(
            self._inputs(real_tif=True), self.out_root,
            engine=FakeEngine(), write_mets_file=False,
        )
        self.assertTrue(result.ok)
        self.assertIn(FakeEngine.MARKER, stale.read_text(encoding="utf-8"))

    def test_failure_stubs_do_not_block_rerun(self) -> None:
        (self.alto_dir / "scan1.xml").write_text(FAILED_STUB_ALTO, encoding="utf-8")
        result = process_ref_id(
            self._inputs(real_tif=True), self.out_root,
            engine=FakeEngine(), write_mets_file=False,
        )
        self.assertTrue(result.ok)


class ConcurrentEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        obj = Path(self._tmp.name) / "RefA"
        (obj / "ALTO").mkdir(parents=True)
        self.alto_path = obj / "ALTO" / "page1.xml"
        self.alto_path.write_text(PLAIN_ALTO, encoding="utf-8")

    def _touch_externally(self) -> None:
        """Simulate another session writing the file: new content + a
        guaranteed-different mtime (ticks can be coarse on some shares)."""
        self.alto_path.write_text(
            PLAIN_ALTO.replace("Original", "SomeoneElse"), encoding="utf-8")
        st = self.alto_path.stat()
        os.utime(self.alto_path,
                 ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    def test_save_detects_external_change(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        page.edit_word(page.words[0], "Mine")
        self._touch_externally()
        with self.assertRaises(ConcurrentEditError):
            page.save()
        self.assertIn("SomeoneElse", self.alto_path.read_text(encoding="utf-8"),
                      "a refused save must leave the other version in place")
        self.assertTrue(page.dirty, "edits must survive the refused save")

    def test_force_save_overwrites(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        page.edit_word(page.words[0], "Mine")
        self._touch_externally()
        page.save(force=True)
        content = self.alto_path.read_text(encoding="utf-8")
        self.assertIn('CONTENT="Mine"', content)
        self.assertNotIn("SomeoneElse", content)
        self.assertFalse(page.dirty)

    def test_save_rebaselines_no_false_positive(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        page.edit_word(page.words[0], "First")
        page.save()
        page.edit_word(page.words[0], "Second")
        page.save()  # must not raise: the first save re-baselined the mtime
        self.assertIn('CONTENT="Second"',
                      self.alto_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
