"""Tests for the atomic write helpers (``_util.atomic_write_text/_bytes``).

Canonical outputs (ALTO, Text, METS — including review-GUI saves and
``repackage`` rewrites) go through temp-file + ``os.replace`` so a crash,
disk-full, or network hiccup mid-write can never truncate an existing file.
That is load-bearing for hand-corrected ALTO, where truncation destroys
human labor. These tests lock in: content correctness, byte-parity with the
``Path.write_text`` calls the helpers replaced (platform newline translation
included), and the failure contract — destination untouched and no temp
litter when a write blows up mid-flight or before it starts.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_atomic_write
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr._util import atomic_write_bytes, atomic_write_text  # noqa: E402


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.dest = self.dir / "page001.xml"

    def _dir_entries(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def test_creates_and_overwrites(self) -> None:
        atomic_write_text(self.dest, "first\n")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "first\n")
        atomic_write_text(self.dest, "second\n")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "second\n")
        self.assertEqual(self._dir_entries(), ["page001.xml"],
                         "no temp files may be left behind")

    def test_byte_parity_with_write_text(self) -> None:
        # The same multi-line content written via Path.write_text and via the
        # atomic helper must land byte-identical (platform newline translation
        # included) — adopting the helper changes durability, not output.
        content = '<?xml version="1.0" encoding="UTF-8"?>\n<alto>\n\t<Page/>\n</alto>\n'
        plain = self.dir / "plain.xml"
        plain.write_text(content, encoding="utf-8")
        atomic_write_text(self.dest, content)
        self.assertEqual(self.dest.read_bytes(), plain.read_bytes())

    def test_bytes_roundtrip(self) -> None:
        atomic_write_bytes(self.dest, b"\x00\xffraw bytes, no translation\n")
        self.assertEqual(self.dest.read_bytes(), b"\x00\xffraw bytes, no translation\n")

    def test_unicode_content(self) -> None:
        atomic_write_text(self.dest, "½ → naïve — transcript\n")
        self.assertEqual(self.dest.read_text(encoding="utf-8"),
                         "½ → naïve — transcript\n")

    def test_failed_replace_leaves_destination_intact(self) -> None:
        self.dest.write_text("precious corrected ALTO", encoding="utf-8")
        with mock.patch("src.alto_ocr._util.os.replace",
                        side_effect=OSError("network drive vanished")):
            with self.assertRaises(OSError):
                atomic_write_text(self.dest, "half-written garbage")
        self.assertEqual(self.dest.read_text(encoding="utf-8"),
                         "precious corrected ALTO",
                         "a failed write must not touch the existing file")
        self.assertEqual(self._dir_entries(), ["page001.xml"],
                         "the temp file must be cleaned up after a failure")

    def test_encode_failure_touches_nothing(self) -> None:
        self.dest.write_text("precious", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            atomic_write_text(self.dest, "naïve →", encoding="ascii")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "precious")
        self.assertEqual(self._dir_entries(), ["page001.xml"],
                         "an encode error must not even create a temp file")


if __name__ == "__main__":
    unittest.main()
