"""The desktop GUI's layout and review-tab behavior (gui.py, review.py,
widgets.py).

Two layers. The pure helpers — Advanced-settings validation, confidence
percentages, geometry parsing — run wherever tkinter imports. The rest
builds the real app on a hidden Tk root and drives it the way a corrector
would, which is the only way to catch a handler wired to a widget that no
longer exists. Those skip where Tk has no display; dialogs are replaced by
stubs that fail the test if an unexpected one appears (a real modal dialog
would hang the run), and the settings file is redirected to a temp dir so
nothing touches the user's real ``~/.alto_ocr.json``.

Locked here (2026-09-24 supervisor review): Advanced settings live in their
own window under Tools, with the Tesseract location; the log and run
controls exist only on the Run tab; the output folder's button reads
Browse…; the Review tab's object picker is labelled Transcript Folder;
confidence shows as percentages; deleting a word asks nothing and
Ctrl+Z undoes it; the page view and the symbol keyboard pop out into
windows of their own and dock back with their state intact.

Run from the repo root (so ``src.alto_ocr`` resolves):

    python -m unittest tests.test_gui
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import tkinter as tk
    from tkinter import ttk

    from src.alto_ocr import gui, review, theme, widgets
except ImportError:  # pragma: no cover - no tkinter on this machine
    tk = None

from lxml import etree  # noqa: E402

_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t</Description>
\t<Layout>
\t\t<Page ID="P1" PHYSICAL_IMG_NR="1" HEIGHT="2794" WIDTH="2159" PC="{pc}">
\t\t\t<PrintSpace ID="P1_PS00001" HPOS="100" VPOS="100" WIDTH="1900" HEIGHT="2500">
\t\t\t\t<TextBlock ID="P1_TB00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="200">
\t\t\t\t\t<TextLine ID="P1_TL00001" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00001" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="60" CONTENT="Board" WC="0.96"/>
\t\t\t\t\t\t<SP ID="P1_SP00001" HPOS="300" VPOS="100" WIDTH="20"/>
\t\t\t\t\t\t<String ID="P1_ST00002" HPOS="320" VPOS="100" WIDTH="150" HEIGHT="60" CONTENT="cf" WC="0.40"/>
\t\t\t\t\t</TextLine>
\t\t\t\t\t<TextLine ID="P1_TL00002" HPOS="100" VPOS="200" WIDTH="400" HEIGHT="80">
\t\t\t\t\t\t<String ID="P1_ST00003" HPOS="100" VPOS="200" WIDTH="180" HEIGHT="60" CONTENT="Trustees" WC="0.30"/>
\t\t\t\t\t</TextLine>
\t\t\t\t</TextBlock>
\t\t\t</PrintSpace>
\t\t</Page>
\t</Layout>
</alto>
"""


def _no_dialog(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"unexpected {name} dialog: {args[:2]}")
    return fail


@unittest.skipIf(tk is None, "tkinter not available")
class PureHelperTests(unittest.TestCase):
    """No Tk root needed: the rules behind the widgets."""

    def _raw(self, **over):
        raw = {"tesseract_path": "", "min_dpi": "300", "do_denoise": True,
               "do_clahe": True, "do_binarize": False, "debug_preprocessed": "",
               "ocr_lang": "eng", "psm": "3", "oem": "3", "tesseract_timeout": "600",
               "glmocr_endpoint": "",
               "glmocr_model": "", "glmocr_timeout": "120", "with_checksums": True}
        raw.update(over)
        return raw

    def test_parse_advanced_builds_job_options(self) -> None:
        adv = gui.parse_advanced(self._raw(psm="1", ocr_lang="  "))
        self.assertIsNone(adv["tesseract_path"])
        self.assertEqual(adv["ocr_options"].psm, 1)
        self.assertEqual(adv["ocr_options"].timeout_s, 600)
        self.assertEqual(adv["ocr_options"].language, "eng")  # blank → default
        self.assertTrue(adv["glmocr_model"])                   # blank → default
        # The two frame-changing stages are never on, whatever was saved.
        self.assertFalse(adv["preprocess_config"].do_deskew)
        self.assertFalse(adv["preprocess_config"].do_border_crop)
        gui.JobConfig(input_dir=Path("in"), output_dir=Path("out"), recurse=False,
                      write_mets=True, write_plaintext=True, **adv)

    def test_parse_advanced_names_the_field_to_fix(self) -> None:
        for over, message in (({"min_dpi": "abc"}, "Min DPI"),
                              ({"min_dpi": "0"}, "Min DPI"),
                              ({"psm": "14"}, "PSM"),
                              ({"oem": "x"}, "OEM"),
                              ({"glmocr_timeout": "-5"}, "GLMOCR timeout"),
                              ({"tesseract_timeout": "0"}, "Tesseract timeout"),
                              ({"tesseract_timeout": "1.5"}, "Tesseract timeout"),
                              ({"tesseract_path": "C:/no/such/tesseract.exe"}, "Tesseract")):
            with self.subTest(over=over):
                with self.assertRaisesRegex(gui.SettingsError, message):
                    gui.parse_advanced(self._raw(**over))

    def test_tesseract_path_must_be_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "tesseract.exe"
            exe.write_bytes(b"")
            adv = gui.parse_advanced(self._raw(tesseract_path=f"  {exe}  "))
            self.assertEqual(adv["tesseract_path"], exe)
            self.assertEqual(gui.advanced_settings(adv)["tesseract_path"], str(exe))

    def test_advanced_settings_drop_auto_detected_path_and_retired_keys(self) -> None:
        saved = gui.advanced_settings(gui.parse_advanced(self._raw()))
        self.assertIsNone(saved["tesseract_path"])  # None = remove the key
        self.assertIsNone(saved["do_deskew"])
        self.assertIsNone(saved["do_border_crop"])
        self.assertEqual(saved["psm"], 3)
        self.assertEqual(saved["tesseract_timeout"], 600)

    def test_confidence_percentages(self) -> None:
        self.assertEqual(review.pct(0.87), "87%")
        self.assertEqual(review.pct(1.0), "100%")
        self.assertEqual(review.pct(0.29), "29%")   # 0.29 * 100 is 28.999…
        self.assertEqual(review.pct(0.499), "49%")  # floored: flagged under 50%
        self.assertEqual(review.pct(None), "—")
        self.assertEqual(review.parse_pct("50%"), 0.5)
        self.assertEqual(review.parse_pct(" 95 "), 0.95)
        self.assertEqual(review.parse_pct("150%"), 1.0)
        self.assertIsNone(review.parse_pct("abc"))
        self.assertEqual(review.threshold_text(0.5), "50%")
        self.assertIn("50%", review.THRESHOLD_CHOICES)
        self.assertTrue(all(c.endswith("%") for c in review.THRESHOLD_CHOICES))

    def test_display_agrees_with_the_flag_rule(self) -> None:
        for threshold_pct in range(0, 101, 5):
            threshold = review.parse_pct(f"{threshold_pct}%")
            for wc_100 in range(0, 101):
                wc = wc_100 / 100
                shown = int(review.pct(wc).rstrip("%"))
                self.assertEqual(wc < threshold, shown < threshold_pct,
                                 f"wc={wc} threshold={threshold}")

    def test_parse_geometry(self) -> None:
        self.assertEqual(widgets.parse_geometry("1200x900+10+20"), (1200, 900, 10, 20))
        self.assertEqual(widgets.parse_geometry("800x600+-1500+40"), (800, 600, -1500, 40))
        self.assertIsNone(widgets.parse_geometry("garbage"))
        self.assertIsNone(widgets.parse_geometry(None))


@unittest.skipIf(tk is None, "tkinter not available")
class _AppFixture(unittest.TestCase):
    """The real app on a hidden root, with dialogs stubbed and the settings
    file redirected to a temp dir."""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as e:  # pragma: no cover - headless machine
            self.skipTest(f"Tk has no display here: {e}")
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings_path = self.tmp / "settings.json"
        for target, value in ((gui, "SETTINGS_PATH"),):
            patcher = mock.patch.object(target, value, self.settings_path)
            patcher.start()
            self.addCleanup(patcher.stop)
        for mod in (gui, review):
            for name in ("showerror", "showwarning", "showinfo",
                         "askyesno", "askyesnocancel"):
                patcher = mock.patch.object(mod.messagebox, name, _no_dialog(name))
                patcher.start()
                self.addCleanup(patcher.stop)
        self.app = gui.AltoOcrApp(self.root)

    def _destroy(self) -> None:
        # Tcl timers belong to the thread, not the interpreter: one left
        # pending (the app's log poll) fires into the *next* test's Tk and
        # prints "invalid command name". Cancel them, then let queued events
        # run — making a frame its own window makes ttk queue a
        # <<ThemeChanged>> that errors if it fires after destroy.
        try:
            for after_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(after_id)
            self.root.update()
            self.root.destroy()
        except tk.TclError:
            pass

    def pump(self) -> None:
        self.root.update_idletasks()


class MainWindowTests(_AppFixture):
    def test_tabs_and_menu(self) -> None:
        nb = self.app._nb
        self.assertEqual([nb.tab(t, "text") for t in nb.tabs()], ["Run", "Review"])
        labels = [self.app.tools_menu.entrycget(i, "label")
                  for i in range(self.app.tools_menu.index("end") + 1)]
        self.assertIn("Advanced settings…", labels)

    def test_log_and_run_controls_live_on_the_run_tab_only(self) -> None:
        run_tab = str(self.app.tab_run)
        for widget in (self.app.log_text, self.app.btn_run, self.app.btn_cancel,
                       self.app.progress):
            self.assertTrue(str(widget).startswith(run_tab + "."), widget)

        def texts(w):
            yield w
            for child in w.winfo_children():
                yield from texts(child)
        # The reading-order transcript panel is the Review tab's one Text.
        self.assertFalse([w for w in texts(self.app.review) if isinstance(w, tk.Text)
                          and w is not self.app.review.txt_order],
                         "the Review tab must not carry a log")

    def test_output_folder_button_says_browse(self) -> None:
        self.assertEqual(self.app.btn_browse_output.cget("text"), "Browse…")

    def test_tesseract_path_is_in_the_advanced_window_not_the_run_tab(self) -> None:
        def bound_to_tesseract(w):
            try:
                return str(w.cget("textvariable")) == str(self.app.var_tesseract)
            except tk.TclError:
                return False

        def walk(w):
            yield w
            for child in w.winfo_children():
                yield from walk(child)
        self.assertFalse(any(bound_to_tesseract(w) for w in walk(self.app.tab_run)))
        self.app._open_advanced()
        win = self.app._adv_win
        self.assertIsInstance(win, tk.Toplevel)
        self.assertTrue(any(bound_to_tesseract(w) for w in walk(win)))
        self.app._open_advanced()  # a second open raises the same window
        self.assertIs(self.app._adv_win, win)

    def test_advanced_window_validates_then_saves_on_close(self) -> None:
        self.app._open_advanced()
        self.app.var_min_dpi.set("abc")
        errors = []
        with mock.patch.object(gui.messagebox, "showerror",
                               lambda *a, **k: errors.append((a, k))):
            self.app._close_advanced()
        self.assertEqual(len(errors), 1)
        self.assertIn("Min DPI", errors[0][0][1])
        self.assertIs(errors[0][1]["parent"], self.app._adv_win)  # over the window
        self.assertIsNotNone(self.app._adv_win)                   # still open

        self.app.var_min_dpi.set("400")
        self.app.var_psm.set("1")
        self.app._close_advanced()
        self.assertIsNone(self.app._adv_win)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["min_dpi"], saved["psm"]), (400, 1))
        self.assertNotIn("tesseract_path", saved)


class ThemeTests(_AppFixture):
    """View ▸ Theme: Light keeps the native look, Dark recolours both kinds
    of widget and swaps in a themed menu bar, Match Windows follows the
    system, and the choice is saved."""

    def choose(self, mode: str) -> None:
        self.app.var_theme.set(mode)
        self.app._on_theme_chosen()

    def test_light_is_the_default_and_native(self) -> None:
        self.assertEqual(self.app.var_theme.get(), "light")
        style = ttk.Style(self.app.root)
        self.assertEqual(style.theme_use(), theme.native_theme(style))
        self.assertEqual(str(self.app.root.cget("menu")), str(self.app.menubar))
        self.assertFalse(self.app.themed_menubar.winfo_manager())

    def test_dark_recolours_and_swaps_the_menu_bar(self) -> None:
        self.choose("dark")
        self.assertEqual(ttk.Style(self.app.root).theme_use(), "clam")
        self.assertEqual(self.app.review.lst_pages.cget("background"), theme.DARK.field)
        self.assertEqual(self.app.review.canvas.cget("background"), theme.DARK.canvas)
        self.assertEqual(self.app.review.txt_order.tag_cget("chosen", "background"),
                         theme.DARK.chosen_bg)
        self.assertEqual(str(self.app.root.cget("menu")), "")
        self.assertEqual(self.app.themed_menubar.winfo_manager(), "pack")
        self.assertEqual(ttk.Style(self.app.root).lookup("Muted.TLabel", "foreground"),
                         theme.DARK.muted)
        # The run log keeps its own dark colours in both themes.
        self.assertEqual(self.app.log_text.cget("background"), "#101418")
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "dark")

    def test_back_to_light_restores_everything(self) -> None:
        self.choose("dark")
        self.choose("light")
        style = ttk.Style(self.app.root)
        self.assertEqual(style.theme_use(), theme.native_theme(style))
        self.assertEqual(self.app.review.lst_pages.cget("background"), theme.LIGHT.field)
        self.assertEqual(str(self.app.root.cget("menu")), str(self.app.menubar))
        self.assertFalse(self.app.themed_menubar.winfo_manager())

    def test_match_windows_follows_the_system(self) -> None:
        with mock.patch.object(theme, "system_prefers_dark", return_value=True):
            self.choose("system")
            self.assertTrue(self.app._theme_dark)
        with mock.patch.object(theme, "system_prefers_dark", return_value=False):
            # Coming back to the window re-checks the system setting.
            self.app._on_activate(mock.Mock(widget=self.app.root))
            self.assertFalse(self.app._theme_dark)

    def test_unknown_saved_mode_falls_back_to_light(self) -> None:
        self.assertEqual(theme.normalize_mode("purple"), "light")
        self.assertEqual(theme.normalize_mode(None), "light")
        self.assertIs(theme.resolve("dark"), theme.DARK)

    def test_both_menu_bars_offer_the_theme_choice(self) -> None:
        labels = [self.app.theme_menu.entrycget(i, "label")
                  for i in range(self.app.theme_menu.index("end") + 1)]
        self.assertEqual(labels, ["Light", "Dark", "Match Windows"])
        buttons = [w.cget("text") for w in self.app.themed_menubar.winfo_children()]
        self.assertEqual(buttons, ["File", "Tools", "View", "Help"])


class ReviewFixture(_AppFixture):
    def setUp(self) -> None:
        super().setUp()
        from PIL import Image
        self.obj = self.tmp / "out" / "RefA"
        (self.obj / "ALTO").mkdir(parents=True)
        (self.obj / "Text").mkdir()
        src = self.tmp / "src"
        src.mkdir()
        for stem, pc in (("p1", "0.55"), ("p2", "0.90"), ("p3", "0.30")):
            (self.obj / "ALTO" / f"{stem}.xml").write_text(
                _ALTO.format(pc=pc), encoding="utf-8")
            # Any image size works: word boxes scale image-px / Page-dims.
            Image.new("L", (432, 559), 230).save(src / f"{stem}.jpg")
        self.rv = self.app.review
        self.rv.var_object.set(str(self.obj))
        self.rv.var_source.set(str(src))
        self.rv.load_object()
        self.app._nb.select(self.rv)

    def open(self, stem: str) -> None:
        self.rv._open_page(stem)
        self.assertIsNotNone(self.rv._page)


class ReviewTabTests(ReviewFixture):
    def test_object_folder_is_labelled_transcript_folder(self) -> None:
        def labels(w):
            if isinstance(w, ttk.Label):
                yield str(w.cget("text"))
            for child in w.winfo_children():
                yield from labels(child)
        found = list(labels(self.rv))
        self.assertIn("Transcript Folder:", found)
        self.assertNotIn("Output object:", found)

    def test_corrupt_alto_lists_as_failed_instead_of_crashing(self) -> None:
        # Truncated ALTO is realistic: Tesseract writes its own ALTO
        # non-atomically, so a crash mid-write leaves half a file.
        (self.obj / "ALTO" / "p4.xml").write_text("<alto><Layout", encoding="utf-8")
        self.rv.load_object()
        self.assertEqual(self.rv._sorted_stems[0], "p4")  # failed sorts first
        self.assertEqual(self.rv.lst_pages.get(0),
                         self.rv._page_label("p4", None, "failed"))
        self.assertEqual(len(self.rv._sorted_stems), 4, "the other pages still list")

    def test_page_list_and_labels_show_percentages(self) -> None:
        self.assertEqual(self.rv._sorted_stems, ["p3", "p1", "p2"])  # worst first
        self.assertEqual(self.rv.lst_pages.get(0), "p3   30%")
        self.open("p3")
        self.rv._goto_flagged(+1)
        self.assertEqual(self.rv._selected.content, "cf")
        self.assertEqual(self.rv.lbl_wc.cget("text"), "Confidence 40%")
        self.assertIn("flagged below 50%", self.rv.var_status.get())

    def test_threshold_dropdown_is_in_percent_and_persists_as_a_fraction(self) -> None:
        self.open("p1")
        self.rv.var_threshold.set("35%")
        self.rv._on_threshold_changed()
        self.assertEqual(self.rv._threshold(), 0.35)
        self.assertEqual([w.content for w in self.rv._flagged_now()], ["Trustees"])
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["review_threshold"], 0.35)

    def test_delete_asks_nothing_and_undo_restores_exactly(self) -> None:
        self.open("p1")
        before = etree.tostring(self.rv._page.root)
        self.rv._goto_flagged(+1)
        word = self.rv._selected
        self.assertEqual(word.content, "cf")
        self.rv._delete_word()  # any dialog would fail the test
        self.assertNotIn(word, self.rv._page.words)
        self.assertIn("Ctrl+Z", self.rv.var_status.get())
        self.assertEqual(str(self.rv.btn_save.cget("state")), "normal")
        self.rv._undo_edit()
        self.assertEqual(etree.tostring(self.rv._page.root), before)
        self.assertIs(self.rv._selected, word)
        # Back exactly as loaded: nothing to save, no prompt on leaving.
        self.assertFalse(self.rv._page.dirty)
        self.assertEqual(str(self.rv.btn_save.cget("state")), "disabled")
        self.rv._goto_page(+1)  # _confirm_discard would raise if it asked

    def test_empty_text_on_enter_deletes_without_asking(self) -> None:
        self.open("p1")
        self.rv._goto_flagged(+1)
        self.rv.var_edit.set("   ")
        self.rv._apply_edit(advance=True)
        self.assertEqual([w.content for w in self.rv._page.words], ["Board", "Trustees"])

    def test_control_character_edit_is_refused_with_a_message(self) -> None:
        self.open("p1")
        self.rv._goto_flagged(+1)
        self.rv.var_edit.set("o\x01f")
        with mock.patch.object(self.rv, "bell"):
            self.rv._apply_edit(advance=False)
        self.assertEqual(self.rv._selected.content, "cf")
        self.assertEqual(self.rv._undo, [])
        self.assertIn("U+0001", self.rv.var_status.get())

    def test_save_keeps_the_list_order_and_applies_typed_text(self) -> None:
        self.open("p3")
        self.rv._goto_flagged(+1)
        self.rv.var_edit.set("of")  # typed, Enter not pressed
        self.rv._save_page()
        self.assertEqual(self.rv._sorted_stems, ["p3", "p1", "p2"])  # not re-ranked
        body = (self.obj / "ALTO" / "p3.xml").read_text(encoding="utf-8")
        self.assertIn('CONTENT="of" WC="1.00"', body)
        self.assertTrue(self.rv.lst_pages.get(0).startswith("p3   "))
        self.assertNotEqual(self.rv.lst_pages.get(0), "p3   30%")  # PC refreshed

    def test_page_navigation_follows_the_list(self) -> None:
        self.rv._goto_page(+1)  # nothing open yet: the first page
        self.assertEqual(self.rv._stem, "p3")
        self.rv._goto_page(+1)
        self.assertEqual(self.rv._stem, "p1")
        self.assertEqual(self.rv.lst_pages.curselection(), (1,))
        self.rv._goto_page(+1)
        self.rv._goto_page(+1)  # past the end: stays, says so
        self.assertEqual(self.rv._stem, "p2")
        self.assertIn("last page", self.rv.var_status.get())

    def test_symbol_keys_type_like_a_keyboard(self) -> None:
        from src.alto_ocr.symbols import SYMBOL_KEYBOARD
        self.assertEqual(list(self.rv.keyboard._pages),
                         [c.name for c in SYMBOL_KEYBOARD])
        self.open("p1")
        self.rv._goto_flagged(+1)  # selects "cf" with its text selected
        self.rv._insert_symbol("½")
        self.assertEqual(self.rv.var_edit.get(), "½")  # replaced the selection
        self.rv.ent_edit.icursor("end")
        self.rv._insert_symbol("°")
        self.assertEqual(self.rv.var_edit.get(), "½°")

    def test_keyboard_category_is_remembered(self) -> None:
        self.rv.keyboard.select_category("Greek")
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["review_symbols_tab"], "Greek")


class ReadingOrderTests(ReviewFixture):
    """Word-level reading order: select a word (Shift extends), click the
    word it should follow; nudges, page start, undo, and the transcript
    panel showing exactly what a save writes."""

    def setUp(self) -> None:
        super().setUp()
        self.open("p1")
        self.rv._toggle_reorder_mode()
        self.words = {w.content: w for w in self.rv._page.words}

    def transcript(self) -> str:
        return self.rv.txt_order.get("1.0", "end-1c")

    def highlighted(self) -> list[str]:
        ranges = self.rv.txt_order.tag_ranges("chosen")
        return [self.rv.txt_order.get(a, b) for a, b in zip(ranges[::2], ranges[1::2])]

    def test_panel_shows_the_transcript_a_save_writes(self) -> None:
        self.assertTrue(self.rv.order_panel.winfo_manager())
        self.assertEqual(self.transcript(), "Board cf\nTrustees\n")
        self.assertEqual([w.content for _a, _b, w in self.rv._order_spans],
                         ["Board", "cf", "Trustees"])
        self.assertIn("drag a box", self.rv.var_order_hint.get())

    def test_transcript_positions_map_to_words(self) -> None:
        at = self.rv._order_word_at
        self.assertIs(at("1.0"), self.words["Board"])
        self.assertIs(at("1.7"), self.words["cf"])
        self.assertIs(at("2.3"), self.words["Trustees"])
        self.assertIsNone(at("end"))  # past the last word

    def test_select_then_click_target_moves_and_undo_restores(self) -> None:
        before = etree.tostring(self.rv._page.root)
        self.rv._order_pick(self.words["Trustees"], shift=False)
        self.assertEqual(self.highlighted(), ["Trustees"])
        self.rv._order_pick(self.words["Board"], shift=False)
        self.assertEqual(self.transcript(), "Board Trustees cf\n")
        self.assertEqual(self.highlighted(), ["Trustees"])  # still selected
        self.assertIn("Moved 1 word(s) after “Board”", self.rv.var_status.get())
        self.assertEqual(str(self.rv.btn_save.cget("state")), "normal")
        self.rv._undo_edit()
        self.assertEqual(etree.tostring(self.rv._page.root), before)
        self.assertEqual(self.transcript(), "Board cf\nTrustees\n")
        self.assertEqual(str(self.rv.btn_save.cget("state")), "disabled")

    def test_shift_extends_and_nudge_moves_the_run(self) -> None:
        self.rv._order_pick(self.words["Board"], shift=False)
        self.rv._order_pick(self.words["cf"], shift=True)
        self.assertEqual(self.highlighted(), ["Board cf"])  # one run, space included
        self.rv._order_nudge(+1)
        self.assertEqual(self.transcript(), "Trustees Board cf\n")
        self.rv._order_nudge(+1)  # already last: nothing happens, it says so
        self.assertIn("already read there", self.rv.var_status.get())

    def test_page_start_and_escape(self) -> None:
        self.rv._order_pick(self.words["Trustees"], shift=False)
        self.rv._order_to_start()
        self.assertEqual(self.transcript(), "Trustees Board cf\n")
        self.rv._on_canvas_escape()
        self.assertEqual(self.rv._order_sel, [])
        self.assertEqual(self.highlighted(), [])

    def test_clicking_inside_the_selection_starts_over(self) -> None:
        self.rv._order_pick(self.words["Board"], shift=False)
        self.rv._order_pick(self.words["cf"], shift=True)
        self.rv._order_pick(self.words["cf"], shift=False)
        self.assertEqual(self.highlighted(), ["cf"])
        self.assertEqual(self.transcript(), "Board cf\nTrustees\n")  # nothing moved

    def test_save_writes_the_new_order_with_provenance(self) -> None:
        self.rv._order_pick(self.words["Trustees"], shift=False)
        self.rv._order_to_start()
        self.rv._save_page()
        self.assertEqual((self.obj / "Text" / "p1.txt").read_text(encoding="utf-8"),
                         "Trustees Board cf\n")
        body = (self.obj / "ALTO" / "p1.xml").read_text(encoding="utf-8")
        self.assertIn("1 reading-order change(s)", body)

    def test_ctrl_click_toggles_words_in_and_out(self) -> None:
        self.rv._order_toggle(self.words["Trustees"])
        self.rv._order_toggle(self.words["Board"])
        # Kept in reading order, whatever order they were picked in.
        self.assertEqual([w.content for w in self.rv._order_sel], ["Board", "Trustees"])
        self.assertEqual(self.highlighted(), ["Board", "Trustees"])
        self.rv._order_toggle(self.words["Board"])
        self.assertEqual([w.content for w in self.rv._order_sel], ["Trustees"])

    def test_box_selects_words_by_center_and_shift_adds(self) -> None:
        def box(word):  # a box snug around one word, in image px
            x, y, w, h = word.box_px
            return (x - 1, y - 1, w + 2, h + 2)
        self.rv._order_box_select(box(self.words["cf"]), add=False)
        self.assertEqual([w.content for w in self.rv._order_sel], ["cf"])
        self.rv._order_box_select(box(self.words["Trustees"]), add=True)
        self.assertEqual([w.content for w in self.rv._order_sel], ["cf", "Trustees"])
        self.rv._order_box_select((0, 0, 1, 1), add=False)  # empty: says so, keeps
        self.assertIn("No words", self.rv.var_status.get())
        self.assertEqual(len(self.rv._order_sel), 2)

    def test_one_selected_word_sits_in_the_editor(self) -> None:
        self.assertEqual(str(self.rv.btn_order_edit.cget("state")), "disabled")
        self.rv._order_pick(self.words["Trustees"], shift=False)
        self.assertIs(self.rv._selected, self.words["Trustees"])
        self.assertEqual(self.rv.var_edit.get(), "Trustees")
        self.assertEqual(str(self.rv.btn_order_edit.cget("state")), "normal")
        self.assertIn("Enter or F2", self.rv.var_order_hint.get())
        # Two words: nothing to edit; none: the editor empties.
        self.rv._order_toggle(self.words["Board"])
        self.assertIsNone(self.rv._selected)
        self.assertEqual(str(self.rv.btn_order_edit.cget("state")), "disabled")
        self.rv._on_canvas_escape()
        self.assertEqual(self.rv.var_edit.get(), "")

    def test_edit_in_reading_order_mode_stays_in_the_mode(self) -> None:
        # The corrector's case: a high-confidence word that is still wrong.
        word = self.words["Board"]
        self.rv._order_pick(word, shift=False)
        self.rv.var_edit.set("Boord")
        self.rv._apply_edit(advance=True)
        self.assertEqual(word.content, "Boord")
        self.assertTrue(self.rv.var_reorder_mode.get())
        self.assertEqual(self.rv._order_sel, [word])  # still there to move
        self.assertIs(self.rv._selected, word)
        self.assertEqual(self.transcript(), "Boord cf\nTrustees\n")
        self.assertEqual(str(self.rv.btn_save.cget("state")), "normal")
        # It can still be moved, and both undo cleanly, newest first.
        self.rv._order_pick(self.words["Trustees"], shift=False)
        self.assertEqual(self.transcript(), "cf\nTrustees Boord\n")
        self.rv._undo_edit()
        self.rv._undo_edit()
        self.assertEqual(self.transcript(), "Board cf\nTrustees\n")
        self.assertEqual(self.rv._order_sel, [word])  # undo re-selects it
        self.assertEqual(self.rv.var_edit.get(), "Board")
        self.assertTrue(self.rv.var_reorder_mode.get())

    def test_escape_in_the_editor_abandons_typing_but_keeps_selection(self) -> None:
        word = self.words["cf"]
        self.rv._order_pick(word, shift=False)
        self.rv.var_edit.set("of")
        self.rv._on_escape()
        self.assertEqual(self.rv.var_edit.get(), "cf")
        self.assertEqual(word.content, "cf")
        self.assertEqual(self.rv._order_sel, [word])

    def test_delete_from_reading_order_mode(self) -> None:
        self.rv._order_pick(self.words["cf"], shift=False)
        self.rv._delete_word()
        self.assertEqual(self.transcript(), "Board\nTrustees\n")
        self.assertEqual(self.rv._order_sel, [])
        self.assertIsNone(self.rv._selected)
        self.rv._undo_edit()
        self.assertEqual(self.transcript(), "Board cf\nTrustees\n")
        self.assertEqual([w.content for w in self.rv._order_sel], ["cf"])

    def test_leaving_the_mode_empties_the_editor(self) -> None:
        self.rv._order_pick(self.words["cf"], shift=False)
        self.rv._toggle_reorder_mode()
        self.assertIsNone(self.rv._selected)
        self.assertEqual(self.rv.var_edit.get(), "")

    def test_path_is_drawn_over_neutral_boxes(self) -> None:
        self.rv._order_pick(self.words["cf"], shift=False)
        c = self.rv.canvas
        items = c.find_withtag(review._OVERLAY)
        kinds = [(c.type(i), c.itemcget(i, "outline" if c.type(i) == "rectangle"
                                       else "fill")) for i in items]
        boxes = [k for k in kinds if k[0] == "rectangle"]
        lines = [k for k in kinds if k[0] == "line"]
        self.assertEqual(sorted(color for _t, color in boxes),
                         sorted([review._COLOR_ORDER_BOX] * 2 + [review._COLOR_EDITED]))
        # Two links (Board→cf, cf→Trustees), each a casing and a path.
        self.assertEqual([color for _t, color in lines],
                         [review._COLOR_ORDER_CASING] * 2 + [review._COLOR_ORDER_PATH] * 2)
        # The selection is on top, the boxes under the path.
        self.assertEqual(kinds[-1], ("rectangle", review._COLOR_EDITED))
        self.assertEqual(kinds[0][0], "rectangle")
        self.assertNotEqual(review._COLOR_ORDER_BOX, review._COLOR_ORDER_PATH)

    def test_links_run_between_words_not_through_them(self) -> None:
        link = review.order_link
        a, b = (100, 100, 50, 20), (170, 102, 40, 20)
        # Along a line: the gap between the two, at mid-height.
        self.assertEqual(link(a, b, True), (150, 110, 170, 112))
        self.assertEqual(link(b, a, True), (170, 112, 150, 110))  # backwards
        # Next line down: bottom-right corner to the next word's top-left.
        self.assertEqual(link(b, (100, 140, 60, 20), False), (210, 122, 100, 140))
        # A jump back up (wrong order, or the next column): top to bottom.
        self.assertEqual(link((100, 140, 60, 20), b, False), (160, 140, 170, 122))
        # Overlapping boxes on one line: centre to centre.
        self.assertEqual(link(a, (120, 100, 50, 20), True), (125, 110, 145, 110))

    def test_add_word_mode_turns_reading_order_off(self) -> None:
        self.rv._order_pick(self.words["Board"], shift=False)
        self.rv._toggle_add_mode()
        self.assertFalse(self.rv.var_reorder_mode.get())
        self.assertFalse(self.rv.order_panel.winfo_manager())
        self.assertEqual(self.rv._order_sel, [])


class PopOutTests(ReviewFixture):
    def test_page_pops_out_and_docks_back_with_its_state(self) -> None:
        self.open("p1")
        self.rv._goto_flagged(+1)
        self.rv._delete_word()
        ws = self.rv.workspace
        self.rv._toggle_page_window()
        self.addCleanup(self.rv._page_window.attach)
        self.assertTrue(self.rv._page_window.detached)
        self.assertEqual(str(ws.winfo_toplevel()), str(ws))  # its own window
        self.assertIn("p1", tk.Wm.wm_title(ws))
        self.assertIn("Dock", self.rv.btn_popout.cget("text"))
        # Dialogs about the page go over the page's window.
        self.assertEqual(str(self.rv._ws_parent()), str(ws))
        # State rode along: the deletion is still undoable from out here.
        self.rv._undo_edit()
        self.assertEqual([w.content for w in self.rv._page.words],
                         ["Board", "cf", "Trustees"])

        self.rv._toggle_page_window()
        self.assertFalse(self.rv._page_window.detached)
        self.assertEqual(str(ws.winfo_toplevel()), str(self.root))
        self.assertIn("Pop out", self.rv.btn_popout.cget("text"))
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertIn("geometry", saved["review_page_window"])

    def test_keyboard_pops_out_and_types_into_the_editor(self) -> None:
        self.open("p1")
        self.rv._goto_flagged(+1)
        self.rv._toggle_keyboard_window()
        self.addCleanup(self.rv._kb_window.attach)
        self.assertTrue(self.rv._kb_window.detached)
        holder = self.rv.kb_holder
        self.assertEqual(str(holder.winfo_toplevel()), str(holder))
        with mock.patch.object(self.rv.ent_edit, "focus_force") as force:
            self.rv._insert_symbol("β")
        force.assert_called_once()  # typing handed back to the editor's window
        self.assertEqual(self.rv.var_edit.get(), "β")
        # Popping the page out while the keyboard floats keeps both working.
        self.rv._toggle_page_window()
        self.addCleanup(self.rv._page_window.attach)
        self.rv._toggle_page_window()
        self.rv._toggle_keyboard_window()
        self.assertFalse(self.rv._kb_window.detached)
        self.assertEqual(str(holder.winfo_toplevel()), str(self.root))

    def test_hiding_the_keyboard_docks_it_first(self) -> None:
        self.rv._toggle_keyboard_window()
        self.rv.var_symbols.set(False)
        self.rv._on_symbols_toggled()
        self.assertFalse(self.rv._kb_window.detached)
        self.assertFalse(self.rv.kb_holder.winfo_manager())  # hidden
        self.rv._toggle_keyboard_window()  # pop out shows it again
        self.addCleanup(self.rv._kb_window.attach)
        self.assertTrue(self.rv.var_symbols.get())
        self.assertTrue(self.rv._kb_window.detached)

    def test_off_screen_geometry_is_not_restored(self) -> None:
        self.rv._settings["review_page_window"] = {"geometry": "900x700+-40000+-40000"}
        self.rv._toggle_page_window()
        self.addCleanup(self.rv._page_window.attach)
        self.pump()
        w, h, x, y = widgets.parse_geometry(tk.Wm.wm_geometry(self.rv.workspace))
        self.assertGreater(x, -40000)
        self.assertGreater(y, -40000)


if __name__ == "__main__":
    unittest.main()
