"""The Review tab's Repackage status line.

`Save page` writes the edited page's transcript itself, and the Repackage
button offers to save a dirty page before running — so the transcript count
this summary sees is essentially always zero when driven from the GUI. The old
wording ("0 transcript(s) updated") therefore reported apparent failure on
every healthy run, which read as a bug and was reported as one. Zero must say
"already current"; a non-zero count means ALTO and text genuinely drifted (a
CLI OCR run, an external XML editor) and is named as a re-sync.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_repackage_summary
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.remediate import RepackageResult  # noqa: E402


def _summary(results):
    # Imported lazily: review.py pulls in tkinter at module scope, which is
    # absent on headless machines. The function under test is pure.
    from src.alto_ocr.review import repackage_summary
    return repackage_summary(results)


def _result(*, pages, txt_written, mets_written=True, ref_id="RefA"):
    return RepackageResult(
        ref_id=ref_id, object_dir=Path("/tmp") / ref_id, pages=pages,
        txt_written=txt_written, mets_written=mets_written, problems=[],
    )


HAS_TKINTER = importlib.util.find_spec("tkinter") is not None


@unittest.skipUnless(HAS_TKINTER, "review.py imports tkinter at module scope")
class RepackageSummaryTests(unittest.TestCase):
    def test_zero_transcripts_reads_as_healthy_not_as_failure(self) -> None:
        msg = _summary([_result(pages=4, txt_written=0)])
        self.assertIn("all 4 transcript(s) already current", msg)
        self.assertNotIn("0 transcript", msg)

    def test_drift_is_named_as_a_resync(self) -> None:
        msg = _summary([_result(pages=4, txt_written=3)])
        self.assertIn("3 of 4 transcript(s) re-synced from ALTO", msg)

    def test_mets_counted_across_objects(self) -> None:
        msg = _summary([_result(pages=2, txt_written=0),
                        _result(pages=5, txt_written=0, ref_id="RefB")])
        self.assertIn("Repackaged 2 object(s)", msg)
        self.assertIn("2 METS refreshed", msg)
        self.assertIn("all 7 transcript(s) already current", msg)

    def test_no_mets_is_stated_plainly(self) -> None:
        # Objects OCR'd with --no-mets have none to refresh; saying
        # "0 METS refreshed" would repeat the original mistake.
        msg = _summary([_result(pages=3, txt_written=0, mets_written=False)])
        self.assertIn("no METS written", msg)


if __name__ == "__main__":
    unittest.main()
