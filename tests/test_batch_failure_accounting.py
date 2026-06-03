"""Regression tests for batch failure accounting (code-review findings #7, #8).

#8 — an aborted Ref ID must report its FULL page count as failed, not 1.
#7 — a BrokenProcessPool must not flag an already-finished Ref ID as un-run.

Run from the repo root:

    python -m unittest tests.test_batch_failure_accounting
"""

from __future__ import annotations

import sys
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.main import _classify_broken_pool_futures, _summary_dict  # noqa: E402
from src.alto_ocr.pipeline import PageResult, RefIdResult  # noqa: E402


def _aborted(ref_id: str, n_pages: int) -> RefIdResult:
    """A whole-Ref-ID abort: expected_page_count set, one synthetic failed page."""
    return RefIdResult(
        ref_id=ref_id,
        output_dir=Path("out") / ref_id,
        expected_page_count=n_pages,
        pages=[PageResult(stem="<aborted>", order=0, ok=False, error="boom")],
    )


def _processed(ref_id: str, n_ok: int, n_failed: int) -> RefIdResult:
    pages = [PageResult(stem=f"p{i}", order=i, ok=True) for i in range(n_ok)]
    pages += [PageResult(stem=f"f{i}", order=n_ok + i, ok=False, error="x")
              for i in range(n_failed)]
    return RefIdResult(ref_id=ref_id, output_dir=Path("out") / ref_id, pages=pages)


class FailedPageCountTests(unittest.TestCase):
    """#8: page_count and failed_page_count must stay consistent on both paths."""

    def test_aborted_ref_counts_all_pages_failed(self):
        r = _aborted("A", 50)
        self.assertEqual(r.page_count, 50)
        self.assertEqual(r.failed_page_count, 50)      # not 1
        self.assertEqual(len(r.failed_pages), 1)        # detail list stays compact
        self.assertFalse(r.ok)

    def test_processed_ref_counts_only_failed_pages(self):
        r = _processed("B", n_ok=2, n_failed=1)
        self.assertEqual(r.page_count, 3)
        self.assertEqual(r.failed_page_count, 1)
        self.assertFalse(r.ok)

    def test_all_ok_ref_has_zero_failed(self):
        r = _processed("C", n_ok=4, n_failed=0)
        self.assertEqual(r.page_count, 4)
        self.assertEqual(r.failed_page_count, 0)
        self.assertTrue(r.ok)

    def test_succeeded_invariant_holds(self):
        # succeeded == page_count - failed_page_count on every path.
        for r in (_aborted("A", 50), _processed("B", 2, 1), _processed("C", 4, 0)):
            succeeded = sum(1 for p in r.pages if p.ok)
            self.assertEqual(succeeded, r.page_count - r.failed_page_count)

    def test_summary_dict_totals_and_field(self):
        results = [_aborted("A", 50), _processed("B", 2, 1)]
        s = _summary_dict(results)
        self.assertEqual(s["failed_page_total"], 51)            # 50 + 1, not 1 + 1
        by_id = {r["ref_id"]: r for r in s["ref_ids"]}
        self.assertEqual(by_id["A"]["failed_page_count"], 50)
        self.assertEqual(by_id["A"]["page_count"], 50)
        self.assertEqual(len(by_id["A"]["failed_pages"]), 1)    # one descriptive error
        self.assertEqual(by_id["B"]["failed_page_count"], 1)


class BrokenPoolClassifyTests(unittest.TestCase):
    """#7: harvest finished futures; only truly un-run ones are flagged to re-run."""

    def _build(self):
        # done + real result -> completed
        fa = Future()
        fa.set_result(RefIdResult(ref_id="A", output_dir=Path("out/A")))
        # done + BrokenProcessPool -> not_run (killed by the pool death)
        fb = Future()
        fb.set_exception(BrokenProcessPool("simulated pool death"))
        # done + real per-Ref-ID error -> failed
        fc = Future()
        fc.set_exception(ValueError("a genuine bad object"))
        # never started -> not_run
        fd = Future()
        # cancelled -> not_run
        fe = Future()
        fe.cancel()
        # already harvested by as_completed before the break -> skipped
        ff = Future()
        ff.set_result(RefIdResult(ref_id="F", output_dir=Path("out/F")))
        futures = {
            fa: SimpleNamespace(ref_id="A", page_count=3),
            fb: SimpleNamespace(ref_id="B", page_count=3),
            fc: SimpleNamespace(ref_id="C", page_count=3),
            fd: SimpleNamespace(ref_id="D", page_count=3),
            fe: SimpleNamespace(ref_id="E", page_count=3),
            ff: SimpleNamespace(ref_id="F", page_count=3),
        }
        return futures

    def test_partition(self):
        futures = self._build()
        completed, failed, not_run = _classify_broken_pool_futures(futures, harvested_ids={"F"})

        self.assertEqual({r.ref_id for r in completed}, {"A"})           # F skipped, not re-harvested
        self.assertEqual([inp.ref_id for inp, _ in failed], ["C"])
        self.assertIsInstance(failed[0][1], ValueError)
        self.assertEqual({inp.ref_id for inp in not_run}, {"B", "D", "E"})

    def test_finished_object_is_not_marked_for_rerun(self):
        # The core of #7: a future that finished successfully must NOT appear in
        # not_run (which would tell the operator to needlessly re-OCR it).
        futures = self._build()
        _, _, not_run = _classify_broken_pool_futures(futures, harvested_ids=set())
        self.assertNotIn("A", {inp.ref_id for inp in not_run})


if __name__ == "__main__":
    unittest.main()
