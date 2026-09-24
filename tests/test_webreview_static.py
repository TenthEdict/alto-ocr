"""Consistency checks on the review UI's static assets.

There is no browser in CI, so the front end cannot be exercised the way the
Python layers are. These tests cover the failure that a browserless build
would otherwise ship silently: a renamed element in ``index.html`` that
``review.js`` still looks up, which fails at runtime as a null dereference
the moment a corrector opens the page.

Also asserted here: the page pulls nothing from the network. The review
server may run on a host with no outbound internet, and a CDN reference
would turn "the internet is down" into "correctors cannot work".

Run from the repo root:

    python -m unittest tests.test_webreview_static
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATIC = Path(__file__).resolve().parent.parent / "src" / "alto_ocr" / "webreview" / "static"


class StaticAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.js = (STATIC / "review.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "review.css").read_text(encoding="utf-8")

    def test_every_id_the_script_looks_up_exists(self) -> None:
        present = set(re.findall(r'\bid="([^"]+)"', self.html))
        # Both accessors the script uses: the global getElementById and the
        # per-view `$` shorthand inside renderPage.
        wanted = (set(re.findall(r"getElementById\('([^']+)'\)", self.js))
                  | set(re.findall(r"\$\('([^']+)'\)", self.js)))
        self.assertTrue(wanted, "found no element lookups — the regex has drifted")
        self.assertEqual(sorted(wanted - present), [],
                         "review.js looks up element ids that index.html does not define")

    def test_every_declared_id_is_actually_used(self) -> None:
        """Catches the other direction: markup left behind after a rewrite."""
        present = set(re.findall(r'\bid="([^"]+)"', self.html))
        templates = set(re.findall(r'<template id="([^"]+)"', self.html))
        wanted = (set(re.findall(r"getElementById\('([^']+)'\)", self.js))
                  | set(re.findall(r"\$\('([^']+)'\)", self.js)))
        self.assertEqual(sorted(present - wanted - templates), [])

    def test_templates_match(self) -> None:
        declared = set(re.findall(r'<template id="([^"]+)"', self.html))
        used = set(re.findall(r"template\('([^']+)'\)", self.js))
        self.assertEqual(declared, used)

    def test_no_external_resources(self) -> None:
        """The host may have no outbound internet; a CDN reference would make
        the internet a dependency of correcting a transcript."""
        for asset, text in (("index.html", self.html), ("review.js", self.js),
                            ("review.css", self.css)):
            with self.subTest(asset=asset):
                self.assertNotRegex(text, r"https?://(?!www\.w3\.org)",
                                    "external URL in a page that must work offline")

    def test_asset_links_are_relative(self) -> None:
        """The server is meant to sit behind Apache at a path prefix
        (``/ocr-review/``), so absolute ``/static/...`` links would 404 there."""
        for match in re.findall(r'(?:src|href)="([^"#]+)"', self.html):
            with self.subTest(link=match):
                self.assertFalse(match.startswith("/"),
                                 f"{match} is absolute and breaks under a path prefix")

    def test_api_calls_are_relative(self) -> None:
        for call in re.findall(r"""(?:api|postJSON)\(\s*[`'"]([^`'"]+)""", self.js):
            with self.subTest(call=call):
                self.assertFalse(call.startswith("/"),
                                 f"{call} is absolute and breaks under a path prefix")

    def test_operation_names_match_the_server(self) -> None:
        """The client must not be able to invent an op the server rejects."""
        from src.alto_ocr.webreview import ops
        sent = set(re.findall(r"op:\s*'([a-z]+)'", self.js))
        self.assertTrue(sent)
        self.assertEqual(sorted(sent - set(ops._ALL_OPS)), [])

    def test_symbol_keyboard_comes_from_the_server(self) -> None:
        """Correctors move between the two UIs; the characters they cannot
        type must be the same keys in both. So neither UI hard-codes them:
        the server hands the browser the one layout (``symbols.py``) that
        the Tk GUI renders. This checks the wiring at both ends; the layout
        itself is tested in test_symbols."""
        self.assertNotRegex(self.js, r"const\s+GLYPHS\b",
                            "review.js hard-codes a symbol palette again")
        self.assertIn("CONFIG.symbols", self.js)
        app_src = (STATIC.parent / "app.py").read_text(encoding="utf-8")
        self.assertRegex(app_src, r'"symbols":\s*keyboard_payload\(\)')

    def test_confidence_is_shown_as_percentages(self) -> None:
        """Both UIs show confidence as whole percentages (the Tk tab's
        ``review.pct``), floored so the number agrees with the flag rule."""
        self.assertNotRegex(self.js, r"\bfmt\(", "a decimal formatter is back")
        self.assertIn("Math.floor(Number(n) * 100 + 1e-9)", self.js)


if __name__ == "__main__":
    unittest.main()
