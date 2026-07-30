"""Provenance must never lie: review counts don't double, METS dates persist.

M2: ``ReviewPage.save`` folds the session's edit count into the cumulative
``<Processing ID="review">`` total; a failed txt write followed by a retried
save used to fold the same edits in twice. M3: ``repackage_object`` rebuilds
the METS, which used to reset ``CREATEDATE`` to "now" on every run — the
original creation date now survives and each rebuild stamps ``LASTMODDATE``.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_provenance_dates
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

import src.alto_ocr.remediate as remediate  # noqa: E402
from src.alto_ocr.mets import METS_NS, PageEntry, build_mets, write_mets  # noqa: E402
from src.alto_ocr.remediate import ReviewPage, repackage_object  # noqa: E402

ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description><MeasurementUnit>mm10</MeasurementUnit></Description>
\t<Layout>
\t\t<Page ID="P1" HEIGHT="2794" WIDTH="2159">
\t\t\t<PrintSpace ID="PS1" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="100">
\t\t\t\t\t<TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<String ID="S1" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="One" WC="0.40"/>
\t\t\t\t\t\t<String ID="S2" HPOS="320" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Two" WC="0.40"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""


def _review_count(alto_path: Path) -> int:
    root = etree.parse(str(alto_path)).getroot()
    for proc in root.iter("{*}Processing"):
        if proc.get("ID") == "review":
            step = next(proc.iter("{*}processingStepDescription"))
            m = re.search(r"(\d+) word", step.text or "")
            return int(m.group(1)) if m else -1
    return -1


class ProvenanceCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        obj = Path(self._tmp.name) / "RefA"
        (obj / "ALTO").mkdir(parents=True)
        (obj / "Text").mkdir()
        self.alto_path = obj / "ALTO" / "p1.xml"
        self.alto_path.write_text(ALTO, encoding="utf-8")

    def test_second_save_does_not_double_count(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        page.edit_word(page.words[0], "Fixed")
        page.save()
        self.assertEqual(_review_count(self.alto_path), 1)
        page.save()  # nothing new edited
        self.assertEqual(_review_count(self.alto_path), 1)
        page.edit_word(page.words[1], "Also")
        page.save()
        self.assertEqual(_review_count(self.alto_path), 2)

    def test_failed_txt_write_then_retry_counts_once(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        page.edit_word(page.words[0], "Fixed")
        real = remediate.atomic_write_text
        state = {"failed": False}

        def flaky(path, text, **kw):
            if not state["failed"] and str(path).endswith(".txt"):
                state["failed"] = True
                raise OSError("network blip")
            return real(path, text, **kw)

        with mock.patch.object(remediate, "atomic_write_text", side_effect=flaky):
            with self.assertRaises(OSError):
                page.save()  # ALTO written, txt failed
            page.save()  # retry succeeds
        self.assertEqual(_review_count(self.alto_path), 1,
                         "a retried save must not re-add the same edits")


class MetsDateTests(unittest.TestCase):
    def test_repackage_preserves_createdate_and_stamps_lastmod(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            obj = Path(td) / "RefA"
            (obj / "ALTO").mkdir(parents=True)
            (obj / "Text").mkdir()
            (obj / "ALTO" / "p1.xml").write_text(ALTO, encoding="utf-8")
            mets_path = obj / "RefA.mets.xml"
            write_mets(build_mets("RefA", obj, [PageEntry(stem="p1", order=1)]),
                       mets_path)

            # Age the header so preservation is observable.
            root = etree.parse(str(mets_path)).getroot()
            hdr = root.find(f"{{{METS_NS}}}metsHdr")
            hdr.set("CREATEDATE", "2020-01-01T00:00:00+00:00")
            mets_path.write_bytes(etree.tostring(
                root.getroottree(), xml_declaration=True, encoding="UTF-8",
                pretty_print=True))

            r = repackage_object(obj)
            self.assertTrue(r.mets_written)
            hdr = etree.parse(str(mets_path)).getroot().find(f"{{{METS_NS}}}metsHdr")
            self.assertEqual(hdr.get("CREATEDATE"), "2020-01-01T00:00:00+00:00",
                             "rebuilds must not reset the creation date")
            self.assertIsNotNone(hdr.get("LASTMODDATE"))
            self.assertNotEqual(hdr.get("LASTMODDATE"), hdr.get("CREATEDATE"))


if __name__ == "__main__":
    unittest.main()
