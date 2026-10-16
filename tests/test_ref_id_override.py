"""Explicit Ref ID override for a single-object run (``process --ref-id``).

The Ref ID is normally the input folder's *resolved* basename. That breaks
down whenever the folder is named for its role rather than the object it
holds — Goobi workflow hands the pipeline ``images/<title>_media`` while the
object's identity is ``<title>``, so without an override every output folder,
METS OBJID and log line would carry the ``_media`` suffix.

Locked in here: the override steers identity only (output folder name, METS
OBJID/LABEL, the SkippedRefId label) and never discovery; the default stays
the resolved basename; and a value that isn't one usable path component is
rejected before any work happens — it would otherwise land as a stray
directory or escape the output root entirely.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_ref_id_override
"""

from __future__ import annotations

import functools
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from src.alto_ocr.pipeline import process_ref_id  # noqa: E402
from src.alto_ocr.walker import (  # noqa: E402
    RefIdInputs,
    SkippedRefId,
    discover_ref_id,
    validate_ref_id,
)

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"



@functools.lru_cache(maxsize=1)
def _symlinks_available() -> bool:
    """Whether this process may create symlinks.

    Windows grants that privilege only under Developer Mode or elevation;
    without either, symlink creation raises ``OSError`` (WinError 1314). That
    is a limit of the environment, not a failure of the code under test, so
    the symlink-layout tests below SKIP rather than error -- otherwise every
    run on an ordinary Windows workstation reports failures that say nothing
    about the change being made, and the pre-push hook blocks every push.
    CI still exercises them (Linux, and Windows runners hold the privilege).

    Probed by actually creating one, not by checking the platform: the
    privilege is per-process and per-configuration, not per-OS.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()
        try:
            os.symlink(target, Path(tmp) / "link", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_NEEDS_SYMLINKS = unittest.skipUnless(
    _symlinks_available(),
    "symlink creation not permitted here (Windows needs Developer Mode or elevation)",
)


class FakeEngine:
    """Minimal OCREngine writing one trivial ALTO + txt per page."""

    name = "fake"

    def adjust_preprocess(self, config):
        return config

    def ocr(self, image, output_base, *, dpi, page_index, source_filename=None):
        output_base = Path(output_base)
        alto = output_base.with_suffix(".xml")
        txt = output_base.with_suffix(".txt")
        alto.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<alto xmlns="{ALTO_NS}">'
            "<Description><MeasurementUnit>mm10</MeasurementUnit></Description>"
            '<Layout><Page ID="P1" HEIGHT="10" WIDTH="10"/></Layout></alto>\n',
            encoding="utf-8",
        )
        txt.write_text("fake\n", encoding="utf-8")
        return alto, txt


def _write_tif(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.full((80, 60), 255, np.uint8)).save(path, dpi=(300, 300))


class ValidateRefIdTests(unittest.TestCase):
    def test_plain_name_accepted_unchanged(self) -> None:
        self.assertEqual(validate_ref_id("MSP00074_14698"), "MSP00074_14698")

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_ref_id("")

    def test_surrounding_whitespace_rejected_not_trimmed(self) -> None:
        # Trimming would file the object under a name nobody typed.
        with self.assertRaises(ValueError):
            validate_ref_id(" MSP00074 ")

    def test_path_separators_rejected(self) -> None:
        for value in ("a/b", "../escape", "a\\b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ref_id(value)

    def test_dot_names_rejected(self) -> None:
        for value in (".", ".."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ref_id(value)


class DiscoverRefIdOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src_dir = self.root / "images" / "MSP00074_media"
        _write_tif(self.src_dir / "0001.tif")

    def test_default_is_resolved_basename(self) -> None:
        found = discover_ref_id(self.src_dir)
        assert isinstance(found, RefIdInputs)
        self.assertEqual(found.ref_id, "MSP00074_media")

    def test_override_replaces_identity_only(self) -> None:
        plain = discover_ref_id(self.src_dir)
        overridden = discover_ref_id(self.src_dir, ref_id="MSP00074")
        assert isinstance(plain, RefIdInputs)
        assert isinstance(overridden, RefIdInputs)
        self.assertEqual(overridden.ref_id, "MSP00074")
        # Discovery itself is untouched: same folder, same pages.
        self.assertEqual(overridden.source_dir, plain.source_dir)
        self.assertEqual([s.path for s in overridden.sources],
                         [s.path for s in plain.sources])

    @_NEEDS_SYMLINKS
    def test_override_survives_a_symlinked_input(self) -> None:
        # The resolved basename is what the default uses, so a symlink named
        # for the object can't carry identity on its own — the override must.
        link = self.root / "MSP00074"
        link.symlink_to(self.src_dir, target_is_directory=True)
        self.assertEqual(discover_ref_id(link).ref_id, "MSP00074_media")
        self.assertEqual(discover_ref_id(link, ref_id="MSP00074").ref_id, "MSP00074")

    def test_bad_override_raises_before_discovery(self) -> None:
        with self.assertRaises(ValueError):
            discover_ref_id(self.src_dir, ref_id="a/b")

    def test_empty_folder_still_returns_none(self) -> None:
        empty = self.root / "empty_media"
        empty.mkdir()
        self.assertIsNone(discover_ref_id(empty, ref_id="MSP00074"))

    def test_override_labels_a_skipped_object(self) -> None:
        # Two sources colliding on one output stem: the skip report must be
        # filed under the override, or the object is unfindable in a summary.
        collide = self.root / "images" / "MSP00075_media"
        _write_tif(collide / "a" / "0001.tif")
        _write_tif(collide / "b" / "0001.tif")
        found = discover_ref_id(collide, ref_id="MSP00075")
        assert isinstance(found, SkippedRefId)
        self.assertEqual(found.ref_id, "MSP00075")


class PipelineHonoursOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src_dir = self.root / "images" / "MSP00074_media"
        _write_tif(self.src_dir / "0001.tif")
        self.out_root = self.root / "out"

    def test_outputs_and_mets_carry_the_override(self) -> None:
        inputs = discover_ref_id(self.src_dir, ref_id="MSP00074")
        assert isinstance(inputs, RefIdInputs)
        result = process_ref_id(inputs, self.out_root, engine=FakeEngine())
        self.assertTrue(result.ok, msg=str(result.failed_pages))

        obj_dir = self.out_root / "MSP00074"
        self.assertEqual(result.output_dir, obj_dir.resolve())
        self.assertTrue((obj_dir / "ALTO" / "0001.xml").is_file())
        self.assertTrue((obj_dir / "Text" / "0001.txt").is_file())
        self.assertFalse((self.out_root / "MSP00074_media").exists())

        mets = obj_dir / "MSP00074.mets.xml"
        self.assertTrue(mets.is_file())
        self.assertIn('OBJID="MSP00074"', mets.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
