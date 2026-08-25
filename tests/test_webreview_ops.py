"""Tests for the browser review server's batch-application layer (webreview/ops.py).

Covers: strict validation of a client's operation list (unknown ops, bad
indexes, duplicate word targets, empty edit text, malformed add boxes,
clear combined with per-word ops), index-to-Word resolution *before* any
mutation runs, per-op application and counting, the already-verified case
being reported without inflating provenance, batch atomicity (a failing op
leaves the file on disk untouched), the load-to-post concurrency token, and
the JSON payload's coordinate frame.

Run from the repo root:

    python -m unittest tests.test_webreview_ops
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.remediate import ConcurrentEditError, ReviewPage  # noqa: E402
from src.alto_ocr.webreview import ops  # noqa: E402

# Institution-style ALTO v2 in mm10 -- the frame that misplaces boxes in
# Goobi's own editor and must come out right here. Page 2159x2794 against a
# 2550x3300 px image.
_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="0.70">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="200">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Board" WC="0.96"/>
\t\t\t\t\t\t<SP ID="P1_SP00001" HPOS="300" VPOS="100" WIDTH="20"/>
\t\t\t\t\t\t<String ID="P1_ST00002" HPOS="320" VPOS="100" WIDTH="150" HEIGHT="60" CONTENT="cf" WC="0.40"/>
\t\t\t\t\t\t<SP ID="P1_SP00002" HPOS="470" VPOS="100" WIDTH="20"/>
\t\t\t\t\t\t<String ID="P1_ST00003" HPOS="490" VPOS="100" WIDTH="180" HEIGHT="60" CONTENT="rnlnutes" WC="0.31"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""

_IMG_SIZE = (2550, 3300)


class _PageCase(unittest.TestCase):
    """One ALTO on disk in an object layout, reloadable between assertions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alto_dir = root / "OBJ" / "ALTO"
        self.alto_dir.mkdir(parents=True)
        (root / "OBJ" / "Text").mkdir()
        self.alto_path = self.alto_dir / "page_0001.xml"
        self.alto_path.write_text(_ALTO, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self) -> ReviewPage:
        return ReviewPage(self.alto_path, _IMG_SIZE)


class ParseOperationsTests(unittest.TestCase):
    def test_rejects_non_list(self) -> None:
        with self.assertRaises(ops.OperationError):
            ops.parse_operations({"op": "edit"})

    def test_rejects_unknown_op(self) -> None:
        with self.assertRaises(ops.OperationError):
            ops.parse_operations([{"op": "rotate", "index": 0}])

    def test_rejects_non_object_entry(self) -> None:
        with self.assertRaises(ops.OperationError):
            ops.parse_operations(["edit"])

    def test_rejects_bad_index(self) -> None:
        for bad in (-1, "0", None, True, 1.5):
            with self.subTest(index=bad), self.assertRaises(ops.OperationError):
                ops.parse_operations([{"op": "verify", "index": bad}])

    def test_rejects_duplicate_word_target(self) -> None:
        # Two ops on one word would each count toward the provenance total,
        # overstating how many words a human changed.
        with self.assertRaises(ops.OperationError) as ctx:
            ops.parse_operations([{"op": "edit", "index": 1, "text": "of"},
                                  {"op": "verify", "index": 1}])
        self.assertIn("already has an operation", str(ctx.exception))

    def test_rejects_empty_edit_text(self) -> None:
        for bad in ("", "   ", None, 7):
            with self.subTest(text=bad), self.assertRaises(ops.OperationError):
                ops.parse_operations([{"op": "edit", "index": 0, "text": bad}])

    def test_rejects_bad_add_box(self) -> None:
        for box in (None, [1, 2, 3], [1, 2, 3, "4"], [1, 2, 0, 5], [1, 2, 5, -1]):
            with self.subTest(box=box), self.assertRaises(ops.OperationError):
                ops.parse_operations([{"op": "add", "text": "x", "box": box}])

    def test_rejects_clear_with_word_ops(self) -> None:
        with self.assertRaises(ops.OperationError) as ctx:
            ops.parse_operations([{"op": "clear"}, {"op": "verify", "index": 0}])
        self.assertIn("Clear cannot be combined", str(ctx.exception))

    def test_rejects_two_clears(self) -> None:
        with self.assertRaises(ops.OperationError):
            ops.parse_operations([{"op": "clear"}, {"op": "clear"}])

    def test_accepts_clear_then_add(self) -> None:
        # The documented flow for a wholly hallucinated page: reject
        # everything, then restore the handful of words that were real.
        parsed = ops.parse_operations([
            {"op": "clear"},
            {"op": "add", "text": "1927", "box": [100, 100, 80, 40]},
        ])
        self.assertEqual([o.op for o in parsed], ["clear", "add"])

    def test_accepts_mixed_word_batch(self) -> None:
        parsed = ops.parse_operations([
            {"op": "edit", "index": 2, "text": "minutes"},
            {"op": "verify", "index": 1},
            {"op": "delete", "index": 0},
            {"op": "add", "text": "of", "box": [700.5, 100, 90, 60]},
        ])
        self.assertEqual([o.op for o in parsed], ["edit", "verify", "delete", "add"])
        self.assertEqual(parsed[3].box, (700.5, 100.0, 90.0, 60.0))

    def test_edit_text_survives_verbatim(self) -> None:
        parsed = ops.parse_operations([{"op": "edit", "index": 0, "text": " Board "}])
        # Whitespace collapsing is remediate's job, not the parser's -- the
        # parser must not quietly change what the corrector typed.
        self.assertEqual(parsed[0].text, " Board ")


class ResolveTargetsTests(_PageCase):
    def test_out_of_range_index_is_rejected(self) -> None:
        page = self.load()
        with self.assertRaises(ops.OperationError) as ctx:
            ops.resolve_targets(page, ops.parse_operations(
                [{"op": "verify", "index": 99}]))
        self.assertIn("not on this page any more", str(ctx.exception))

    def test_indexes_resolve_before_mutation(self) -> None:
        """A deletion earlier in the batch must not shift a later target.

        This is the property that makes document-order indexes safe as word
        addresses: without up-front resolution, deleting word 0 would slide
        word 2 into slot 1 and the edit would land on the wrong word.
        """
        page = self.load()
        batch = ops.parse_operations([
            {"op": "delete", "index": 0},
            {"op": "edit", "index": 2, "text": "minutes"},
        ])
        ops.apply_operations(page, batch)
        page.save()
        reloaded = self.load()
        self.assertEqual([w.content for w in reloaded.words], ["cf", "minutes"])


class ApplyOperationsTests(_PageCase):
    def test_counts_each_kind(self) -> None:
        page = self.load()
        report = ops.apply_operations(page, ops.parse_operations([
            {"op": "edit", "index": 2, "text": "minutes"},
            {"op": "verify", "index": 1},
            {"op": "add", "text": "of", "box": [700, 100, 90, 60]},
        ]))
        self.assertEqual((report.edited, report.verified, report.added), (1, 1, 1))
        self.assertEqual(report.deleted, 0)
        self.assertTrue(report.changed)
        self.assertIn("1 corrected", report.summary())
        self.assertIn("1 confirmed", report.summary())

    def test_already_verified_is_reported_not_counted(self) -> None:
        """Confirming a word another session confirmed is not an error, and
        must not add to the count of words a human acted on."""
        page = self.load()
        ops.apply_operations(page, ops.parse_operations([{"op": "verify", "index": 1}]))
        page.save()
        page = self.load()
        report = ops.apply_operations(page, ops.parse_operations(
            [{"op": "verify", "index": 1}]))
        self.assertEqual(report.verified, 0)
        self.assertEqual(report.already_verified, 1)
        self.assertFalse(report.changed)
        self.assertEqual(report.summary(), "No changes to save.")

    def test_clear_counts_every_removed_word(self) -> None:
        page = self.load()
        report = ops.apply_operations(page, ops.parse_operations([{"op": "clear"}]))
        self.assertEqual(report.cleared, 3)
        self.assertEqual(page.words, [])
        self.assertIn("page cleared (3 words removed)", report.summary())

    def test_clear_then_add_restores_a_real_word(self) -> None:
        page = self.load()
        ops.apply_operations(page, ops.parse_operations([
            {"op": "clear"},
            {"op": "add", "text": "1927", "box": [120, 120, 200, 60]},
        ]))
        page.save()
        reloaded = self.load()
        self.assertEqual([w.content for w in reloaded.words], ["1927"])

    def test_edit_stamps_verified_confidence(self) -> None:
        page = self.load()
        ops.apply_operations(page, ops.parse_operations(
            [{"op": "edit", "index": 2, "text": "minutes"}]))
        page.save()
        reloaded = self.load()
        word = reloaded.words[2]
        self.assertEqual(word.content, "minutes")
        self.assertEqual(word.wc, 1.0)
        self.assertFalse(word.needs_review(0.5))

    def test_failing_op_leaves_the_file_untouched(self) -> None:
        """Batch atomicity: nothing reaches disk until every op succeeded.

        ``add`` with whitespace-only text is rejected by remediate itself, so
        this exercises the real path -- an op the parser lets through and the
        domain layer refuses.
        """
        before = self.alto_path.read_bytes()
        page = self.load()
        batch = [ops.Operation(ops.EDIT, index=0, text="Boards"),
                 ops.Operation(ops.ADD, text="   ", box=(100, 100, 50, 50))]
        with self.assertRaises(ValueError):
            ops.apply_operations(page, batch)
        self.assertEqual(self.alto_path.read_bytes(), before)


class TokenTests(_PageCase):
    def test_token_fingerprints_the_content_not_the_clock(self) -> None:
        """Content-based on purpose: the filesystem's mtime granularity
        (~4 ms locally, coarser over SMB) lets two quick saves share a
        timestamp, and a digest cannot collide that way."""
        token = ops.load_token(self.alto_path)
        size, _, digest = token.partition("-")
        self.assertEqual(int(size), self.alto_path.stat().st_size)
        self.assertEqual(len(digest), 32)
        # Touching the file without changing a byte must not invalidate the
        # corrector's view -- the bytes they reviewed are still on disk.
        import os
        os.utime(self.alto_path, (0, 0))
        self.assertEqual(ops.load_token(self.alto_path), token)

    def test_token_changes_when_content_changes(self) -> None:
        token = ops.load_token(self.alto_path)
        self.alto_path.write_text(_ALTO.replace("Board", "Boards"), encoding="utf-8")
        self.assertNotEqual(ops.load_token(self.alto_path), token)

    def test_missing_token_is_rejected(self) -> None:
        with self.assertRaises(ops.OperationError):
            ops.check_token(self.alto_path, None)

    def test_matching_token_passes(self) -> None:
        ops.check_token(self.alto_path, ops.load_token(self.alto_path))

    def test_stale_token_raises_concurrent_edit(self) -> None:
        """The load-to-post window: another session saved while this
        corrector was working, so their view is of a version that is gone."""
        stale = ops.load_token(self.alto_path)
        page = self.load()
        ops.apply_operations(page, ops.parse_operations([{"op": "verify", "index": 1}]))
        page.save()
        with self.assertRaises(ConcurrentEditError) as ctx:
            ops.check_token(self.alto_path, stale)
        self.assertIn("changed on disk", str(ctx.exception))

    def test_absent_file_yields_a_token_rather_than_raising(self) -> None:
        self.assertEqual(ops.load_token(self.alto_dir / "nope.xml"), "0")


class PagePayloadTests(_PageCase):
    def test_boxes_are_in_source_image_pixels(self) -> None:
        """The mm10 fix, asserted: box geometry is scaled by image-size /
        Page-dimensions, which is exactly what Goobi's own editor omits."""
        page = self.load()
        payload = ops.page_payload(page, stem="page_0001",
                                   alto_path=self.alto_path, low_wc=0.5)
        self.assertEqual(payload["imageWidth"], 2550)
        self.assertEqual(payload["imageHeight"], 3300)
        first = payload["words"][0]
        sx, sy = 2550 / 2159, 3300 / 2794
        self.assertAlmostEqual(first["box"][0], round(100 * sx, 2), places=2)
        self.assertAlmostEqual(first["box"][1], round(100 * sy, 2), places=2)
        self.assertAlmostEqual(first["box"][2], round(200 * sx, 2), places=2)
        # A box left in ALTO units would start at 100, not ~118 -- the 1.968x
        # error that puts every Goobi highlight up and to the left.
        self.assertNotAlmostEqual(first["box"][0], 100.0, places=1)

    def test_flagged_list_matches_the_threshold(self) -> None:
        page = self.load()
        payload = ops.page_payload(page, stem="page_0001",
                                   alto_path=self.alto_path, low_wc=0.5)
        self.assertEqual(payload["flagged"], [1, 2])
        self.assertEqual([w["text"] for w in payload["words"]],
                         ["Board", "cf", "rnlnutes"])
        self.assertEqual(payload["token"], ops.load_token(self.alto_path))
        self.assertEqual(payload["pc"], "0.70")

    def test_unscored_words_are_flagged(self) -> None:
        """A word no engine attached confidence to needs eyes just as much."""
        self.alto_path.write_text(_ALTO.replace(' WC="0.96"', ""), encoding="utf-8")
        page = self.load()
        payload = ops.page_payload(page, stem="page_0001",
                                   alto_path=self.alto_path, low_wc=0.5)
        self.assertIn(0, payload["flagged"])
        self.assertIsNone(payload["words"][0]["wc"])


if __name__ == "__main__":
    unittest.main()
