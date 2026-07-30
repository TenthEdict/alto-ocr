"""Prefix-namespaced ALTO must behave exactly like default-namespace ALTO.

External editors (the eScriptorium growth path) may legally emit ALTO whose
elements carry a namespace *prefix* (``<a:alto xmlns:a=…>``) instead of a
default namespace. The old ``root.nsmap.get(None)`` lookup returned ``None``
for such files, so ``alto_to_text`` found zero words — and ``repackage``
would then have overwritten every transcript with an empty file. These tests
lock in the fix: ``alto_to_text`` iterates namespace-agnostically and
``ReviewPage`` resolves the namespace from the root's own qualified name.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_alto_namespace
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree  # noqa: E402

from src.alto_ocr.remediate import (  # noqa: E402
    ReviewPage,
    alto_to_text,
    has_review_provenance,
    repackage_object,
)

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v2#"

PREFIXED_ALTO = f"""<?xml version="1.0" encoding="UTF-8"?>
<a:alto xmlns:a="{ALTO_NS}">
\t<a:Description><a:MeasurementUnit>mm10</a:MeasurementUnit></a:Description>
\t<a:Layout>
\t\t<a:Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159">
\t\t\t<a:PrintSpace ID="PS1" HPOS="0" VPOS="0" WIDTH="2159" HEIGHT="2794">
\t\t\t\t<a:TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="100">
\t\t\t\t\t<a:TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="900" HEIGHT="80">
\t\t\t\t\t\t<a:String ID="S1" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Board" WC="0.44"/>
\t\t\t\t\t\t<a:SP ID="SP1" HPOS="300" VPOS="100" WIDTH="20"/>
\t\t\t\t\t\t<a:String ID="S2" HPOS="320" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Minutes" WC="0.90"/>
\t\t\t\t\t</a:TextLine>
\t\t\t\t</a:TextBlock>
\t\t\t</a:PrintSpace>
\t\t</a:Page>
\t</a:Layout>
</a:alto>
"""


class AltoToTextNamespaceTests(unittest.TestCase):
    def test_prefixed_namespace_renders(self) -> None:
        root = etree.fromstring(PREFIXED_ALTO.encode())
        self.assertEqual(alto_to_text(root), "Board Minutes\n")

    def test_no_namespace_renders(self) -> None:
        bare = (PREFIXED_ALTO.replace("a:", "")
                .replace(f' xmlns="{ALTO_NS}"', "")
                .replace(f'<alto xmlns:a="{ALTO_NS}">', "<alto>"))
        root = etree.fromstring(bare.encode())
        self.assertEqual(alto_to_text(root), "Board Minutes\n")


class PrefixedObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.obj = Path(self._tmp.name) / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir()
        self.alto_path = self.obj / "ALTO" / "page1.xml"
        self.alto_path.write_text(PREFIXED_ALTO, encoding="utf-8")

    def test_review_page_finds_and_edits_words(self) -> None:
        page = ReviewPage(self.alto_path, (800, 1000))
        self.assertEqual(len(page.words), 2,
                         "prefixed ALTO must expose its words for review")
        page.edit_word(page.words[0], "Trustees")
        txt = page.save()
        self.assertIsNotNone(txt)
        self.assertEqual(txt.read_text(encoding="utf-8"), "Trustees Minutes\n")
        # The saved file is still parseable and carries review provenance.
        self.assertTrue(has_review_provenance(self.alto_path))

    def test_repackage_renders_real_transcript_not_blank(self) -> None:
        stale = self.obj / "Text" / "page1.txt"
        stale.write_text("stale transcript", encoding="utf-8")
        r = repackage_object(self.obj)
        self.assertTrue(r.ok)
        self.assertEqual(r.txt_written, 1)
        self.assertEqual(stale.read_text(encoding="utf-8"), "Board Minutes\n",
                         "prefixed ALTO must never blank the transcript")


if __name__ == "__main__":
    unittest.main()
