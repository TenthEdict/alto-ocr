"""A Tesseract run that never finishes must fail its page, not hang the batch.

``_run_tesseract`` passes ``OCROptions.timeout_s`` to ``subprocess.run``; a
``TimeoutExpired`` becomes a ``RuntimeError`` naming the page, which the
pipeline's per-page isolation turns into a failure stub while the rest of the
object carries on.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_tesseract_timeout
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr import ocr  # noqa: E402
from src.alto_ocr.pipeline import process_ref_id  # noqa: E402
from src.alto_ocr.triage import scan_alto_file  # noqa: E402
from src.alto_ocr.walker import RefIdInputs, SourceFile  # noqa: E402


def _timeout(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))


class TesseractTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.engine = ocr.TesseractEngine(
            binary=self.root / "tesseract.exe",
            options=ocr.OCROptions(timeout_s=7))

    def test_timeout_is_passed_and_reported(self) -> None:
        with mock.patch.object(ocr.subprocess, "run", side_effect=_timeout) as run:
            with self.assertRaisesRegex(RuntimeError, r"timed out after 7s"):
                self.engine.ocr(np.full((20, 20), 255, np.uint8),
                                self.root / "out" / "p1", dpi=300, page_index=1)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(list((self.root / "out").glob(".*.tess.png")), [],
                         "the temporary image is cleaned up on timeout")

    def test_pipeline_writes_a_stub_and_continues(self) -> None:
        src = self.root / "src" / "RefA"
        src.mkdir(parents=True)
        tif = src / "p1.tif"
        Image.fromarray(np.full((80, 60), 255, np.uint8)).save(tif, dpi=(300, 300))
        inputs = RefIdInputs(ref_id="RefA", source_dir=src,
                             sources=(SourceFile(path=tif, page_count=1),))
        with mock.patch.object(ocr.subprocess, "run", side_effect=_timeout):
            result = process_ref_id(inputs, self.root / "out",
                                    engine=self.engine, write_mets_file=False)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.pages[0].error)
        alto = self.root / "out" / "RefA" / "ALTO" / "p1.xml"
        self.assertEqual(scan_alto_file(alto).status, "failed")
        self.assertTrue((self.root / "out" / "RefA" / "Text" / "p1.txt").is_file())


if __name__ == "__main__":
    unittest.main()
