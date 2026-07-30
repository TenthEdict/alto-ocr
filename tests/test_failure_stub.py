"""Failure stubs must be well-formed XML no matter what the error text was.

The stub reason comes from ``str(exception)`` — Tesseract stderr and OS
errors routinely contain ``<``, ``>``, ``&`` — and used to be interpolated
raw, producing malformed stub ALTO that crashed the Review tab. The reason
is now XML-escaped, and ``ReviewPage`` turns an unparseable file into a
``StubPageError`` (actionable "re-run this page") instead of leaking an
``XMLSyntaxError`` through the GUI.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_failure_stub
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.pipeline import _write_failure_stubs  # noqa: E402
from src.alto_ocr.remediate import ReviewPage, StubPageError  # noqa: E402
from src.alto_ocr.triage import scan_alto_file  # noqa: E402

NASTY_REASON = 'Tesseract failed (rc=1) on <page 3>.tif: expected "img" & got <blob>'


class FailureStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.alto = d / "page.xml"
        self.txt = d / "page.txt"

    def test_markup_in_reason_still_yields_wellformed_stub(self) -> None:
        _write_failure_stubs(self.alto, self.txt, reason=NASTY_REASON)
        root = etree.parse(str(self.alto)).getroot()  # must not raise
        step = next(root.iter("{*}processingStepDescription"))
        self.assertEqual(step.text, f"OCR failed: {NASTY_REASON}",
                         "escaping must round-trip the reason verbatim")
        self.assertEqual(self.txt.read_text(encoding="utf-8"), "")

    def test_stub_still_classifies_as_failed_in_triage(self) -> None:
        _write_failure_stubs(self.alto, None, reason=NASTY_REASON)
        self.assertEqual(scan_alto_file(self.alto).status, "failed")

    def test_malformed_alto_raises_stub_page_error(self) -> None:
        self.alto.write_text("<alto unclosed", encoding="utf-8")
        with self.assertRaises(StubPageError):
            ReviewPage(self.alto, (800, 1000))


if __name__ == "__main__":
    unittest.main()
