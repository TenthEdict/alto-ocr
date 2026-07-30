"""The final rename retries transient Windows/SMB access-denied failures.

``os.replace`` onto an existing file intermittently fails on SMB shares when
something else briefly holds a handle on the destination (antivirus, the Search
indexer, an oplock break in flight). Windows reports that as ERROR_ACCESS_DENIED
(5) just as often as ERROR_SHARING_VIOLATION (32), so errno alone cannot tell it
apart from a real permission problem — only retrying can.

Observed 2026-07-30 writing ALTO to \\\\wl.mydata.purdue.edu: 3 of 41 pages in one
object failed the confidence merge's write while pages written seconds either
side succeeded. Each lost ``Page/@PC``, which silently removed them from the
review queue.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_atomic_write_retry
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr import _util  # noqa: E402


def _winerr(code: int) -> PermissionError:
    e = PermissionError("Access is denied")
    e.winerror = code  # type: ignore[attr-defined]
    return e


class TestReplaceRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dest = self.dir / "page.xml"
        self.dest.write_text("old", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_succeeds_after_transient_access_denied(self):
        real = _util.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _winerr(5)
            return real(src, dst)

        with mock.patch.object(_util.os, "replace", flaky), \
                mock.patch.object(_util.time, "sleep"):
            _util.atomic_write_text(self.dest, "new")

        self.assertEqual(self.dest.read_text(encoding="utf-8"), "new")
        self.assertEqual(calls["n"], 3)

    def test_sharing_violation_is_also_retried(self):
        real = _util.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _winerr(32)
            return real(src, dst)

        with mock.patch.object(_util.os, "replace", flaky), \
                mock.patch.object(_util.time, "sleep"):
            _util.atomic_write_text(self.dest, "new")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "new")

    def test_persistent_denial_still_raises_and_leaves_original(self):
        """A real ACL problem must not be swallowed — and must not truncate."""
        with mock.patch.object(_util.os, "replace", side_effect=_winerr(5)), \
                mock.patch.object(_util.time, "sleep"):
            with self.assertRaises(PermissionError):
                _util.atomic_write_text(self.dest, "new")

        self.assertEqual(self.dest.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.dir.glob("*.tmp")), [], "temp file leaked")

    def test_non_transient_oserror_is_not_retried(self):
        calls = {"n": 0}

        def boom(src, dst):
            calls["n"] += 1
            e = OSError("no space")
            e.winerror = 112  # ERROR_DISK_FULL
            raise e

        with mock.patch.object(_util.os, "replace", boom), \
                mock.patch.object(_util.time, "sleep"):
            with self.assertRaises(OSError):
                _util.atomic_write_text(self.dest, "new")
        self.assertEqual(calls["n"], 1, "should fail fast, not retry")

    def test_posix_style_oserror_without_winerror_is_not_retried(self):
        calls = {"n": 0}

        def boom(src, dst):
            calls["n"] += 1
            raise OSError(13, "Permission denied")

        with mock.patch.object(_util.os, "replace", boom), \
                mock.patch.object(_util.time, "sleep"):
            with self.assertRaises(OSError):
                _util.atomic_write_text(self.dest, "new")
        self.assertEqual(calls["n"], 1)

    def test_happy_path_replaces_once(self):
        real = _util.os.replace
        calls = {"n": 0}

        def counted(src, dst):
            calls["n"] += 1
            return real(src, dst)

        with mock.patch.object(_util.os, "replace", counted):
            _util.atomic_write_text(self.dest, "new")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
