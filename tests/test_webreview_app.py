"""Wiring checks for the browser review server that need FastAPI itself.

FastAPI is not a test dependency (it lives in ``requirements-web.txt``), so
this module skips where it is absent. What it guards are regressions that
would otherwise ship silently:

* **Save and preview must not be coroutines.** FastAPI runs an ``async def``
  handler on the event loop and a plain ``def`` on a worker thread. These two
  decode scans, parse ALTO and write over SMB; as coroutines, one corrector's
  save froze every other corrector's requests (audit 2026-10-02).
* **Two saves of one page must serialise.** Off the event loop, two posts
  with the same token could both pass the token check before either wrote.
* **A working-copy cache hit must not prune the cache.** Pruning lists the
  whole render cache; it was running on every page image and word crop.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_webreview_app
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import fastapi  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    fastapi = None

from PIL import Image  # noqa: E402

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"
ALTO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<alto xmlns="{ALTO_NS}"><Description>'
    "<MeasurementUnit>pixel</MeasurementUnit></Description>"
    '<Layout><Page ID="P1" HEIGHT="400" WIDTH="300" PC="0.50">'
    '<PrintSpace HPOS="0" VPOS="0" WIDTH="300" HEIGHT="400">'
    '<TextBlock ID="B1" HPOS="10" VPOS="10" WIDTH="100" HEIGHT="20">'
    '<TextLine ID="L1" HPOS="10" VPOS="10" WIDTH="100" HEIGHT="20">'
    '<String ID="S1" HPOS="10" VPOS="10" WIDTH="40" HEIGHT="20" CONTENT="Helo" WC="0.40"/>'
    "</TextLine></TextBlock></PrintSpace></Page></Layout></alto>\n"
)


@unittest.skipIf(fastapi is None, "fastapi not installed (requirements-web.txt)")
class AppWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        from src.alto_ocr.webreview.app import create_app
        from src.alto_ocr.webreview.config import ReviewServerConfig

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        alto_dir = root / "out" / "RefA" / "ALTO"
        alto_dir.mkdir(parents=True)
        self.alto = alto_dir / "p1.xml"
        self.alto.write_text(ALTO, encoding="utf-8")
        scans = root / "scans" / "RefA"
        scans.mkdir(parents=True)
        Image.new("L", (300, 400), 230).save(scans / "p1.tif")
        self.app = create_app(ReviewServerConfig(
            output_root=root / "out", source_root=root / "scans",
            cache_dir=root / "cache"))

    def _endpoint(self, path: str, method: str):
        for route in self.app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
                return route.endpoint
        self.fail(f"no {method} route {path}")

    def test_save_and_preview_run_off_the_event_loop(self) -> None:
        for path in ("/api/objects/{ref_id}/pages/{stem}",
                     "/api/objects/{ref_id}/pages/{stem}/preview"):
            with self.subTest(path=path):
                self.assertFalse(
                    inspect.iscoroutinefunction(self._endpoint(path, "POST")),
                    "a coroutine handler blocks every other request while it "
                    "decodes, parses and writes")

    def test_save_applies_a_batch(self) -> None:
        from src.alto_ocr.webreview import ops

        save = self._endpoint("/api/objects/{ref_id}/pages/{stem}", "POST")
        body = {"token": ops.load_token(self.alto),
                "ops": [{"op": "edit", "index": 0, "text": "Hello"}]}
        result = save("RefA", "p1", body)
        self.assertTrue(result["saved"])
        self.assertIn('CONTENT="Hello"', self.alto.read_text(encoding="utf-8"))

    def test_concurrent_saves_on_one_page_cannot_both_win(self) -> None:
        """Saves now run on worker threads, so without the per-page lock two
        posts carrying the same token could both pass the token check before
        either wrote, and the second would silently overwrite the first."""
        import threading

        from fastapi import HTTPException

        from src.alto_ocr.webreview import ops

        save = self._endpoint("/api/objects/{ref_id}/pages/{stem}", "POST")
        token = ops.load_token(self.alto)
        real_apply = ops.apply_operations
        gate = threading.Barrier(2, timeout=5)

        def slow_apply(page, operations):
            try:
                gate.wait(timeout=0.5)   # let the other request catch up
            except threading.BrokenBarrierError:
                pass
            return real_apply(page, operations)

        outcomes: list[object] = []

        def post(text: str) -> None:
            body = {"token": token, "ops": [{"op": "edit", "index": 0, "text": text}]}
            try:
                outcomes.append(save("RefA", "p1", body)["saved"])
            except HTTPException as e:
                outcomes.append(e.status_code)

        with mock.patch.object(ops, "apply_operations", side_effect=slow_apply):
            threads = [threading.Thread(target=post, args=(t,)) for t in ("One", "Two")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertCountEqual(outcomes, [True, 409])

    def test_non_object_body_is_a_400(self) -> None:
        from fastapi import HTTPException

        for path in ("/api/objects/{ref_id}/pages/{stem}",
                     "/api/objects/{ref_id}/pages/{stem}/preview"):
            with self.subTest(path=path), self.assertRaises(HTTPException) as cm:
                self._endpoint(path, "POST")("RefA", "p1", ["not", "an", "object"])
            self.assertEqual(cm.exception.status_code, 400)

    def test_cache_hit_does_not_prune(self) -> None:
        from src.alto_ocr.webreview import imaging

        image = self._endpoint("/api/objects/{ref_id}/pages/{stem}/image", "GET")
        with mock.patch.object(imaging, "prune_cache", return_value=0) as prune:
            image("RefA", "p1")          # first touch renders -> prunes
            image("RefA", "p1")          # cache hit -> must not
            image("RefA", "p1")
        self.assertEqual(prune.call_count, 1)


if __name__ == "__main__":
    unittest.main()
