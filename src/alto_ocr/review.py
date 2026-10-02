"""Review tab for the Alto OCR GUI: correct transcripts over the page image.

Layout: pick the **Transcript Folder** (an object's OCR output — the one
containing ``ALTO/``) plus the matching *source image* folder (TIFs are deliberately not in the
output tree — the preservation folder is the canonical copy). The page list
shows per-page confidence (``PC``) and can sort worst-first, mirroring the
``triage`` command's queue. The page view shows the scan with word boxes
colored by ``WC``; clicking a word (or stepping with Next/Prev) opens it in
the editor bar with a magnified snippet of the actual scan region.

Confidence is shown as a **percentage** throughout this tab (the ALTO keeps
``0``–``1``). Percentages are floored, so the display agrees with the flag
rule: under "Flag below 50%" a word shown as 49% is flagged and one shown as
50% is not.

**Pop out page** (toolbar) moves the whole page view — toolbars, scan,
editor, symbol keyboard — into a window of its own, for a second monitor or
a maximized window; closing that window docks it back. Nothing is rebuilt
(``widgets.Detachable``), so unsaved edits and the undo history ride along.
◀ Page / Page ▶ (Ctrl+Page Up/Down) step through the page list from inside
the view, so a popped-out page needs no trips back to the main window. The
list keeps its order while you work; re-tick "Worst first" or reload the
object to re-rank it.

Keyboard flow for correctors: **Enter** commits the edit and jumps to the
next flagged word, **Escape** deselects, **n/p** step through flagged words
(canvas focus), **Ctrl+Z** undoes the last change, **Ctrl+S** saves. Enter
on a word left *unchanged* confirms it — the OCR was right and only the
confidence was low — stamping ``WC="1.00"`` so it leaves the queue for good
(``remediate.ReviewPage.verify_word``, counted apart from corrections in the
page's provenance). Stepping is *positional* — the next flagged word below
the one in hand, wrapping (with a status note) at the page end — so a
correction never costs the corrector their place on the page
(``remediate.ReviewPage.step_from``). **Deleting** a hallucinated word
happens at once, with no confirmation prompt; Ctrl+Z puts it back exactly
(``remediate.ReviewPage.undo_delete``). On the canvas, plain scroll pans
vertically, **Shift+scroll** horizontally, and **Ctrl+scroll** zooms about
the mouse cursor.

**Clear page** (toolbar, confirm-gated) bulk-deletes every word on a page
whose OCR is wholly hallucinated — covers and photographs where fabric or
paper texture was read as thousands of garbage words. The page saves as an
empty transcript and re-classifies as ``empty`` in triage
(``remediate.ReviewPage.clear_words``).

**Repackage** (next to "Load object") refreshes everything derived from
the corrected ALTO — METS sizes/checksums, drifted transcripts — via
``remediate.repackage_object``, so correctors never need the CLI. Runs on
a worker thread (checksumming a big object over SMB takes seconds) with
results polled back on the Tk main loop, mirroring ``gui.py``'s model.

**Add word** mode (toolbar toggle, or ``a`` with canvas focus) covers words
the OCR missed entirely: drag a box around the word on the scan, type it,
press Enter — the word is inserted into the ALTO at the drawn position
(``remediate.ReviewPage.add_word`` picks the line/block) with ``WC="1.00"``.
Ctrl+Z removes it again.

The **symbol keyboard** under the editor types characters correctors can't
reach on a US keyboard — math, Greek, super/subscripts and fractions,
units and apothecary signs, typographic punctuation, arrows, accented
letters — replacing selected text the way a key press would. Its layout,
and the rules that keep every key storable in ALTO, live in the headless
``symbols`` module (shared with the web review client). It can be hidden,
or popped out into a small window of its own that stays above the page.

**Reading order** mode (toolbar toggle, or ``r`` with canvas focus; mutually
exclusive with Add word) fixes transcripts whose words are right but ordered
wrong — wrong column sequence, captions or margin notes read into the body.
It works on **words**: select the words out of place — click one, Shift+click
to extend to a run, Ctrl+click to add or drop a word, or drag a box around a
column or caption (Tesseract's lines often run across both columns, so a
column is never a contiguous run) — then click the word they should come
after; "Page start" puts them first, Ctrl+Up/Ctrl+Down nudge them one word. The scan shows the
reading path (word to word, an arrow at each line break) and a side panel
shows the transcript exactly as a save will write it — clicks there work
like clicks on the scan. ``remediate.ReviewPage.move_words``/``nudge_words``
do the moving (word boxes untouched); Ctrl+Z undoes, interleaved with word
edits.

All ALTO mutation/save semantics live in ``remediate.py`` (headless,
unit-tested); this module is only Tk plumbing.
"""

from __future__ import annotations

import bisect
import logging
import math
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from . import theme
from .remediate import (
    ConcurrentEditError,
    Removal,
    RepackageResult,
    ReviewPage,
    StubPageError,
    Word,
    WordMove,
    index_source_images,
    iter_object_dirs,
    repackage_object,
    transcript_spans,
)
from .triage import DEFAULT_LOW_WC, STATUS_RANK, scan_alto_file_safe
from .walker import natural_chunks, natural_key
from .widgets import Detachable, SymbolKeyboard, Tooltip

# Archival scans are huge (hundreds of megapixels), so raise Pillow's
# decompression-bomb guard far past any real scan — but keep a ceiling so a
# corrupt file can't demand unbounded memory (warn at 1 GP, error at 2 GP).
Image.MAX_IMAGE_PIXELS = 1_000_000_000

# Cap on rendered canvas pixels: 200% zoom on a 600-DPI scan would build a
# multi-hundred-MB PhotoImage and can MemoryError — clamp the scale instead.
_MAX_RENDER_PX = 40_000_000

logger = logging.getLogger("alto_ocr.review")

# "Fit" shows the whole page; "Fit width" fills the view's width and
# scrolls vertically — the useful one in a wide or popped-out window.
_ZOOMS: tuple[str, ...] = ("Fit", "Fit width", "50%", "75%", "100%", "150%", "200%")
_FIT_ZOOMS = ("Fit", "Fit width")

# "Flag below" choices: whole percentages, the way the tab shows confidence.
THRESHOLD_CHOICES: tuple[str, ...] = tuple(f"{p}%" for p in range(0, 101, 5))

# WC → outline color. Blue marks words edited this session (WC becomes 1.00
# on disk, but the corrector should still see what they touched).
_COLOR_EDITED = "#1f6feb"
_COLOR_UNSCORED = "#c026d3"  # no WC (GLMOCR output): needs eyes everywhere
_COLOR_ORDER = "#7c3aed"     # reading-order mode: word outlines + reading path

# Canvas items redrawn on every render carry this tag; the scan image does
# not, so a re-render that keeps the zoom reuses it.
_OVERLAY = "overlay"


# Undo-stack entries. One interleaved stack so a single Ctrl+Z always
# reverses the most recent action, whatever kind it was.
@dataclass
class _EditEntry:
    word: Word
    old_content: str
    old_wc: float | None


@dataclass
class _VerifyEntry:
    word: Word
    old_wc: float | None


@dataclass
class _AddEntry:
    word: Word


@dataclass
class _DeleteEntry:
    word: Word
    removal: Removal
    added: bool  # the word was added this session: undo re-adds it (redo_add)


@dataclass
class _MoveEntry:
    move: WordMove  # opaque here; remediate.undo_move_words replays it


def pct(value: float | None) -> str:
    """A confidence (``0``–``1``) as a whole percentage: ``0.87`` → ``"87%"``.

    Floored, not rounded, so the display agrees with the flag rule
    (``wc < threshold``): under a 50% threshold a word shown as 49% is
    flagged and one shown as 50% is not, where rounding would show 0.496 as
    "50%" and flag it anyway. The epsilon absorbs float error — 0.29 * 100
    is 28.999…, which must still read as 29%.
    """
    if value is None:
        return "—"
    return f"{math.floor(value * 100 + 1e-9)}%"


def threshold_text(value: float) -> str:
    """A flag threshold for the "Flag below" box: ``0.5`` → ``"50%"``."""
    return f"{round(max(0.0, min(1.0, value)) * 100)}%"


def parse_pct(text: str) -> float | None:
    """``"50%"`` (or ``"50"``) → ``0.5``, clamped to ``0``–``1``; None when
    the text is not a number."""
    try:
        value = float(str(text).strip().rstrip("%").strip())
    except ValueError:
        return None
    if math.isnan(value):
        return None
    return max(0.0, min(1.0, value / 100.0))


def _wc_color(wc: float | None) -> str:
    if wc is None:
        return _COLOR_UNSCORED
    if wc < 0.50:
        return "#d1242f"
    if wc < 0.70:
        return "#e16f24"
    if wc < 0.90:
        return "#b58900"
    return "#2da44e"


def repackage_summary(results: list[RepackageResult]) -> str:
    """One-line status for a finished Repackage.

    Deliberately does not report a bare "N transcript(s) updated" count.
    ``Save page`` writes the edited page's transcript itself, and the
    Repackage button offers to save a dirty page before running — so from
    this tab the count is essentially always zero, and phrasing zero as an
    un-met outcome reads as a failure when it is the healthy case. A non-zero
    count means ALTO and text had genuinely drifted (a CLI OCR run, or an
    external XML editor), which is worth naming as a re-sync.
    """
    n_obj = len(results)
    pages = sum(r.pages for r in results)
    mets = sum(1 for r in results if r.mets_written)
    txt = sum(r.txt_written for r in results)

    parts = [f"Repackaged {n_obj} object(s)"]
    parts.append(f"{mets} METS refreshed" if mets else "no METS written")
    if txt:
        parts.append(f"{txt} of {pages} transcript(s) re-synced from ALTO")
    else:
        parts.append(f"all {pages} transcript(s) already current")
    return ", ".join(parts) + "."


class ReviewFrame(ttk.Frame):
    """The Review tab. ``settings`` is the app's persisted dict; ``persist``
    writes it to disk (called after path/threshold/window changes)."""

    def __init__(self, master: tk.Misc, settings: dict,
                 persist: Callable[[], None]) -> None:
        super().__init__(master, padding=8)
        self._settings = settings
        self._persist = persist

        self._pages: list[tuple[str, float | None, str]] = []  # (stem, pc, status)
        self._sorted_stems: list[str] = []  # listbox row index -> stem
        self._img_index: dict[str, Path] = {}
        self._page: Optional[ReviewPage] = None
        self._stem: Optional[str] = None
        self._pil: Optional[Image.Image] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._photo_size: tuple[int, int] = (0, 0)
        self._snippet_photo: Optional[ImageTk.PhotoImage] = None
        self._scale = 1.0
        self._rect_by_word: dict[int, int] = {}   # id(Word) -> canvas item
        self._selected: Optional[Word] = None
        self._undo: list[_EditEntry | _VerifyEntry | _AddEntry
                         | _DeleteEntry | _MoveEntry] = []
        # Reading-order mode state: the words selected to move (reading
        # order), the word a Shift+click extends from, and the transcript
        # panel's (start, end, word) character spans.
        self._order_sel: list[Word] = []
        self._order_anchor: Optional[Word] = None
        self._order_spans: list[tuple[int, int, Word]] = []
        self._order_buttons: list[ttk.Button] = []
        # A press on the scan in reading-order mode: (canvas x, y, modifier)
        # until release decides whether it was a click or a box drag.
        self._order_press_at: Optional[tuple[float, float, str]] = None
        self._order_rect: Optional[int] = None
        # Add-word mode state: the drag in progress and the finalized box
        # (image-pixel space) awaiting its text.
        self._drag_start: Optional[tuple[float, float]] = None
        self._drag_item: Optional[int] = None
        self._pending_box: Optional[tuple[float, float, float, float]] = None
        # A window resize fires <Configure> in bursts; re-fit once it settles.
        self._fit_after: Optional[str] = None
        # Repackage runs on a worker thread; the result list comes back
        # through this queue, polled on the Tk main loop (gui.py's model).
        self._repack_worker: Optional[threading.Thread] = None
        self._repack_queue: "queue.Queue[list[RepackageResult]]" = queue.Queue()

        self.var_object = tk.StringVar(value=str(settings.get("review_object_dir", "")))
        self.var_source = tk.StringVar(value=str(settings.get("review_source_dir", "")))
        self.var_worst_first = tk.BooleanVar(value=bool(settings.get("review_worst_first", True)))
        try:
            saved_threshold = float(settings.get("review_threshold", DEFAULT_LOW_WC))
        except (TypeError, ValueError):
            saved_threshold = DEFAULT_LOW_WC
        self.var_threshold = tk.StringVar(value=threshold_text(saved_threshold))
        self.var_show_all = tk.BooleanVar(value=True)
        self.var_add_mode = tk.BooleanVar(value=False)
        self.var_reorder_mode = tk.BooleanVar(value=False)
        self.var_zoom = tk.StringVar(value="Fit")
        self.var_symbols = tk.BooleanVar(value=bool(settings.get("review_symbols", True)))
        self.var_status = tk.StringVar(value="Pick a transcript folder and its source images.")
        self.var_order_hint = tk.StringVar()
        self.var_edit = tk.StringVar()

        self._build_ui()

    # ---------- UI construction ------------------------------------------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # -- Row 0: paths + load
        paths = ttk.Frame(self)
        paths.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="Transcript Folder:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(paths, textvariable=self.var_object).grid(row=0, column=1, sticky="ew")
        ttk.Button(paths, text="Browse…", command=self._browse_object).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Label(paths, text="Source images:").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=(4, 0))
        ttk.Entry(paths, textvariable=self.var_source).grid(
            row=1, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(paths, text="Browse…", command=self._browse_source).grid(
            row=1, column=2, padx=(6, 0), pady=(4, 0))
        ttk.Button(paths, text="Load object", command=self.load_object).grid(
            row=0, column=3, rowspan=2, padx=(10, 0), sticky="ns")
        self.btn_repackage = ttk.Button(paths, text="Repackage",
                                        command=self._repackage)
        self.btn_repackage.grid(row=0, column=4, rowspan=2, padx=(6, 0), sticky="ns")

        # -- Row 1: page list | page view
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        left.rowconfigure(1, weight=1)
        ttk.Checkbutton(left, text="Worst first", variable=self.var_worst_first,
                        command=self._on_worst_first_toggled).grid(row=0, column=0, sticky="w")
        self.lst_pages = tk.Listbox(left, width=30, exportselection=False)
        self.lst_pages.grid(row=1, column=0, sticky="ns")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst_pages.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.lst_pages.configure(yscrollcommand=sb.set)
        self.lst_pages.bind("<<ListboxSelect>>", self._on_page_selected)

        # The page view is a classic frame: only those can become a window
        # of their own (widgets.Detachable).
        self.workspace = tk.Frame(body)
        self.workspace.grid(row=0, column=1, sticky="nsew")
        self._build_workspace(self.workspace)

        placeholder = ttk.Frame(body, padding=24)
        ttk.Label(placeholder, text="The page is open in its own window.",
                  font=("Segoe UI", 11)).pack(pady=(40, 10))
        ttk.Button(placeholder, text="Bring the page back here",
                   command=self._toggle_page_window).pack()
        ttk.Label(placeholder, textvariable=self.var_status, style="Muted.TLabel",
                  wraplength=460, justify="center").pack(pady=(18, 0))
        self._page_window = Detachable(
            self.workspace, title="Review — Alto OCR", placeholder=placeholder,
            settings=self._settings, geometry_key="review_page_window",
            persist=self._persist, default_size=(1200, 900),
            on_change=self._on_page_window_changed)

    def _build_workspace(self, ws: tk.Frame) -> None:
        ws.columnconfigure(0, weight=1)
        ws.rowconfigure(2, weight=1)

        # -- Toolbar row 1: getting around (pages, zoom, what to flag)
        nav = ttk.Frame(ws)
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        b = ttk.Button(nav, text="◀ Page", command=lambda: self._goto_page(-1))
        b.pack(side="left")
        Tooltip(b, "Previous page in the list (Ctrl+Page Up)")
        b = ttk.Button(nav, text="Page ▶", command=lambda: self._goto_page(+1))
        b.pack(side="left", padx=(4, 0))
        Tooltip(b, "Next page in the list (Ctrl+Page Down)")
        ttk.Separator(nav, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(nav, text="Zoom:").pack(side="left")
        cb = ttk.Combobox(nav, textvariable=self.var_zoom, values=_ZOOMS,
                          width=9, state="readonly")
        cb.pack(side="left", padx=(2, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._render())
        ttk.Label(nav, text="Flag below:").pack(side="left")
        th = ttk.Combobox(nav, textvariable=self.var_threshold, values=THRESHOLD_CHOICES,
                          width=5, state="readonly")
        th.pack(side="left", padx=(2, 10))
        th.bind("<<ComboboxSelected>>", lambda _e: self._on_threshold_changed())
        Tooltip(th, "Words the OCR was less confident about than this are flagged for review")
        ttk.Checkbutton(nav, text="All words", variable=self.var_show_all,
                        command=self._render).pack(side="left")
        self.btn_popout = ttk.Button(nav, text="⧉ Pop out page",
                                     command=self._toggle_page_window)
        self.btn_popout.pack(side="right")
        Tooltip(self.btn_popout, "Move the page into a window of its own for more "
                                 "room, and back again")

        # -- Toolbar row 2: correcting
        bar = ttk.Frame(ws)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        b = ttk.Button(bar, text="◀ Prev", command=lambda: self._goto_flagged(-1))
        b.pack(side="left")
        Tooltip(b, "Previous flagged word (p)")
        b = ttk.Button(bar, text="Next ▶", command=lambda: self._goto_flagged(+1))
        b.pack(side="left", padx=(4, 10))
        Tooltip(b, "Next flagged word (n)")
        b = ttk.Button(bar, text="Undo", command=self._undo_edit)
        b.pack(side="left")
        Tooltip(b, "Undo the last change on this page (Ctrl+Z)")
        ttk.Checkbutton(bar, text="Add word (a)", variable=self.var_add_mode,
                        command=self._on_add_mode_toggled).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(bar, text="Reading order (r)", variable=self.var_reorder_mode,
                        command=self._on_reorder_toggled).pack(side="left", padx=(10, 0))
        self.btn_save = ttk.Button(bar, text="Save page", command=self._save_page,
                                   state="disabled")
        self.btn_save.pack(side="right")
        Tooltip(self.btn_save, "Write this page's ALTO and transcript (Ctrl+S)")
        ttk.Button(bar, text="Clear page…", command=self._clear_page).pack(
            side="right", padx=(0, 6))

        # -- Row 2: the scan, its scrollbars, and the reading-order panel
        area = ttk.Frame(ws)
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, background=theme.current().canvas,
                                highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(area, orient="horizontal", command=self.canvas.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self._bind_canvas()
        self._build_order_panel(area)

        # -- Row 3: editor bar (+ symbol keyboard), row 4: status
        self._build_editor(ws)
        ttk.Label(ws, textvariable=self.var_status, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=(4, 0))

        for widget in (self.canvas, self.ent_edit, self.txt_order):
            widget.bind("<Control-z>", lambda _e: self._undo_edit())
            widget.bind("<Control-s>", lambda _e: self._save_page() or "break")
            widget.bind("<Control-Prior>", lambda _e: self._goto_page(-1) or "break")
            widget.bind("<Control-Next>", lambda _e: self._goto_page(+1) or "break")

    def _bind_canvas(self) -> None:
        c = self.canvas
        c.bind("<Button-1>", self._on_canvas_click)
        c.bind("<Shift-Button-1>", lambda e: self._on_canvas_modified_click(e, "shift"))
        c.bind("<Control-Button-1>", lambda e: self._on_canvas_modified_click(e, "ctrl"))
        c.bind("<B1-Motion>", self._on_canvas_drag)
        c.bind("<ButtonRelease-1>", self._on_canvas_release)
        c.bind("<Configure>", self._on_canvas_configure)
        # Windows and macOS report the wheel as <MouseWheel> with a delta.
        c.bind("<MouseWheel>", lambda e: self._on_wheel(e.delta))
        c.bind("<Shift-MouseWheel>", lambda e: self._on_wheel_h(e.delta))
        c.bind("<Control-MouseWheel>", lambda e: self._on_wheel_zoom(e, e.delta))
        if c.tk.call("tk", "windowingsystem") == "x11":
            # X11 reports it as buttons 4 (up) and 5 (down) instead. (On
            # Windows, 4/5 are the mouse's back/forward side buttons.)
            for button, delta in (("4", 120), ("5", -120)):
                c.bind(f"<Button-{button}>", lambda e, d=delta: self._on_wheel(d))
                c.bind(f"<Shift-Button-{button}>", lambda e, d=delta: self._on_wheel_h(d))
                c.bind(f"<Control-Button-{button}>",
                       lambda e, d=delta: self._on_wheel_zoom(e, d))
        c.bind("<Key-n>", lambda _e: self._goto_flagged(+1))
        c.bind("<Key-p>", lambda _e: self._goto_flagged(-1))
        c.bind("<Key-a>", lambda _e: self._toggle_add_mode())
        c.bind("<Key-r>", lambda _e: self._toggle_reorder_mode())
        c.bind("<Escape>", lambda _e: self._on_canvas_escape())
        c.bind("<Control-Up>", lambda _e: self._order_nudge(-1) or "break")
        c.bind("<Control-Down>", lambda _e: self._order_nudge(+1) or "break")

    def _build_order_panel(self, area: ttk.Frame) -> None:
        # Reading-order side panel: hidden until the mode is toggled on.
        panel = self.order_panel = ttk.Frame(area)
        panel.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(8, 0))
        panel.rowconfigure(4, weight=1)
        ttk.Label(panel, text="Reading order", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(panel, textvariable=self.var_order_hint, style="Muted.TLabel",
                  wraplength=300, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))
        btns = ttk.Frame(panel)
        btns.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for label, tip, command in (
            ("▲ Earlier", "Move the selection one word earlier (Ctrl+Up)",
             lambda: self._order_nudge(-1)),
            ("▼ Later", "Move the selection one word later (Ctrl+Down)",
             lambda: self._order_nudge(+1)),
            ("⤒ Page start", "Make the selection the first words on the page",
             self._order_to_start),
            ("Clear", "Clear the selection (Esc)",
             lambda: self._set_order_selection([], None)),
        ):
            b = ttk.Button(btns, text=label, command=command)
            b.pack(side="left", padx=(0, 4))
            Tooltip(b, tip)
            self._order_buttons.append(b)
        ttk.Label(panel, text="Transcript — as it will be saved").grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.txt_order = tk.Text(panel, width=42, wrap="word", cursor="arrow",
                                 font=("Segoe UI", 10), state="disabled",
                                 padx=6, pady=4)
        self.txt_order.grid(row=4, column=0, sticky="ns")
        pal = theme.current()
        self.txt_order.tag_configure("chosen", background=pal.chosen_bg,
                                     foreground=pal.chosen_fg)
        sb = ttk.Scrollbar(panel, orient="vertical", command=self.txt_order.yview)
        sb.grid(row=4, column=1, sticky="ns")
        self.txt_order.configure(yscrollcommand=sb.set)
        t = self.txt_order
        t.bind("<Button-1>", lambda e: self._on_order_text_click(e, shift=False))
        t.bind("<Shift-Button-1>", lambda e: self._on_order_text_click(e, shift=True))
        t.bind("<Control-Button-1>", lambda e: self._on_order_text_toggle(e))
        for seq in ("<B1-Motion>", "<Double-Button-1>", "<Triple-Button-1>"):
            t.bind(seq, lambda _e: "break")
        t.bind("<Escape>", lambda _e: self._on_canvas_escape())
        t.bind("<Control-Up>", lambda _e: self._order_nudge(-1) or "break")
        t.bind("<Control-Down>", lambda _e: self._order_nudge(+1) or "break")
        panel.grid_remove()

    def _build_editor(self, ws: tk.Frame) -> None:
        editor = ttk.LabelFrame(ws, text="Selected word", padding=6)
        editor.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        editor.columnconfigure(2, weight=1)
        self.lbl_snippet = ttk.Label(editor)
        self.lbl_snippet.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.lbl_wc = ttk.Label(editor, text="—", width=24)
        self.lbl_wc.grid(row=0, column=1, sticky="w")
        self.ent_edit = ttk.Entry(editor, textvariable=self.var_edit, font=("Consolas", 12))
        self.ent_edit.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8))
        self.ent_edit.bind("<Return>", lambda _e: self._apply_edit(advance=True))
        self.ent_edit.bind("<Escape>", lambda _e: self._on_escape())
        btns = ttk.Frame(editor)
        btns.grid(row=1, column=3, sticky="e")
        ttk.Button(btns, text="Apply / Confirm (Enter)",
                   command=lambda: self._apply_edit(advance=True)).pack(side="left")
        b = ttk.Button(btns, text="Delete word", command=self._delete_word)
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, "Remove a word the OCR made up (Ctrl+Z puts it back)")
        ttk.Checkbutton(btns, text="Symbols", variable=self.var_symbols,
                        command=self._on_symbols_toggled).pack(side="left", padx=(10, 0))

        # The keyboard sits in a classic frame so it can become a small
        # window of its own; it types into self.ent_edit wherever it is.
        self.kb_holder = tk.Frame(editor)
        self.kb_holder.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.keyboard = SymbolKeyboard(
            self.kb_holder, self._insert_symbol, header=self._keyboard_header,
            initial=str(self._settings.get("review_symbols_tab", "")),
            on_category=self._on_symbol_category)
        self.keyboard.pack(fill="x", padx=4, pady=(0, 4))
        kb_placeholder = ttk.Frame(editor)
        ttk.Label(kb_placeholder, text="The symbol keyboard is in its own window.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(kb_placeholder, text="Dock it here",
                   command=self._toggle_keyboard_window).pack(side="left", padx=(8, 0))
        self._kb_window = Detachable(
            self.kb_holder, title="Symbols — Alto OCR", placeholder=kb_placeholder,
            settings=self._settings, geometry_key="review_symbols_window",
            persist=self._persist, default_size=None,
            # A palette: it stays above whichever window holds the editor.
            owner=lambda: self.workspace.winfo_toplevel(),
            on_change=self._on_keyboard_window_changed)
        if not self.var_symbols.get():
            self.kb_holder.grid_remove()

    def _keyboard_header(self, strip: ttk.Frame) -> None:
        self.btn_kb_popout = ttk.Button(strip, text="⧉ Pop out",
                                        command=self._toggle_keyboard_window)
        self.btn_kb_popout.pack(side="right")
        Tooltip(self.btn_kb_popout, "Put the symbol keyboard in a small window of its own")

    # ---------- windows: page view + symbol keyboard ----------------------

    def _toggle_page_window(self) -> None:
        self._page_window.toggle()

    def _on_page_window_changed(self, detached: bool) -> None:
        self.btn_popout.configure(text="⇲ Dock page" if detached else "⧉ Pop out page")
        # Docked, the tab's padding frames it; on its own it needs a margin.
        margin = 8 if detached else 0
        self.workspace.configure(padx=margin, pady=margin)
        # The keyboard palette follows the window that now holds the editor.
        self._kb_window.reown()
        self._update_window_title()
        if detached:
            self.canvas.focus_set()

    def _update_window_title(self) -> None:
        ref = Path(self.var_object.get().strip()).name
        parts = [p for p in (self._stem, ref) if p]
        self._page_window.set_title(" — ".join(parts + ["Review — Alto OCR"]))

    def _on_symbols_toggled(self) -> None:
        show = self.var_symbols.get()
        self._settings["review_symbols"] = show
        self._persist()
        if show:
            self.kb_holder.grid()
        else:
            self._kb_window.attach()
            self.kb_holder.grid_remove()

    def _toggle_keyboard_window(self) -> None:
        if not self._kb_window.detached and not self.var_symbols.get():
            self.var_symbols.set(True)
            self._on_symbols_toggled()
        self._kb_window.toggle()

    def _on_keyboard_window_changed(self, detached: bool) -> None:
        self.btn_kb_popout.configure(text="⇲ Dock" if detached else "⧉ Pop out")

    def _on_symbol_category(self, name: str) -> None:
        if self._settings.get("review_symbols_tab") != name:
            self._settings["review_symbols_tab"] = name
            self._persist()

    def _insert_symbol(self, ch: str) -> None:
        """Type a keyboard symbol into the word editor the way a key press
        would: replacing any selected text, leaving the caret after it."""
        ent = self.ent_edit
        if ent.selection_present():
            ent.delete("sel.first", "sel.last")
        ent.insert("insert", ch)
        if self._kb_window.detached:
            # The click activated the keyboard's window; hand typing back to
            # the window with the editor.
            ent.focus_force()
        else:
            ent.focus_set()

    def _ws_parent(self) -> tk.Misc:
        """Where dialogs about the page belong: the window holding the page
        view, which is not the main window while it is popped out."""
        return self.workspace.winfo_toplevel()

    # ---------- object / page loading ------------------------------------

    def _browse_object(self) -> None:
        d = filedialog.askdirectory(title="Transcript folder: an object's OCR output (contains ALTO/)",
                                    parent=self.winfo_toplevel())
        if d:
            self.var_object.set(d)

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(title="Source image folder for this object",
                                    parent=self.winfo_toplevel())
        if d:
            self.var_source.set(d)

    def load_object(self) -> None:
        main = self.winfo_toplevel()
        if not self._confirm_discard(parent=main):
            return
        obj = Path(self.var_object.get().strip())
        src = Path(self.var_source.get().strip())
        alto_dir = obj / "ALTO"
        if not alto_dir.is_dir():
            messagebox.showerror("Review", f"No ALTO folder in:\n{obj}", parent=main)
            return
        if not src.is_dir():
            messagebox.showerror("Review", f"Source image folder not found:\n{src}",
                                 parent=main)
            return

        self._img_index = index_source_images(src)
        self._pages = []
        missing = 0
        for xml in sorted(alto_dir.glob("*.xml"), key=natural_key):
            info = scan_alto_file_safe(xml)
            self._pages.append((xml.stem, info.pc, info.status))
            if xml.stem not in self._img_index:
                missing += 1

        self._settings["review_object_dir"] = str(obj)
        self._settings["review_source_dir"] = str(src)
        self._persist()

        self._clear_view()
        self._stem = None
        self._refresh_page_list()
        self._update_window_title()
        msg = f"{len(self._pages)} page(s) loaded."
        if missing:
            msg += f"  {missing} page(s) have no matching source image and can't be reviewed."
        self.var_status.set(msg)

    def _page_sort_key(self, entry: tuple[str, float | None, str]) -> tuple:
        stem, pc, status = entry
        if self.var_worst_first.get():
            rank = STATUS_RANK.get(status, 4)
            return (rank, pc if pc is not None else 2.0, natural_chunks(stem))
        return (natural_chunks(stem),)

    @staticmethod
    def _page_label(stem: str, pc: float | None, status: str) -> str:
        if status == "scored" and pc is not None:
            return f"{stem}   {pct(pc)}"
        return f"{stem}   [{status}]"

    def _on_worst_first_toggled(self) -> None:
        self._settings["review_worst_first"] = self.var_worst_first.get()
        self._persist()
        self._refresh_page_list()

    def _refresh_page_list(self) -> None:
        """Rebuild the list in sort order. Only on load and on toggling the
        sort: re-ranking after every save would move the page being worked
        on out from under the corrector (and scramble ◀ Page / Page ▶)."""
        self.lst_pages.delete(0, "end")
        self._sorted_stems = []
        for stem, pc, status in sorted(self._pages, key=self._page_sort_key):
            self.lst_pages.insert("end", self._page_label(stem, pc, status))
            self._sorted_stems.append(stem)
        self._select_list_row(self._stem)

    def _select_list_row(self, stem: Optional[str]) -> None:
        self.lst_pages.selection_clear(0, "end")
        if stem in self._sorted_stems:
            i = self._sorted_stems.index(stem)
            self.lst_pages.selection_set(i)
            self.lst_pages.see(i)

    def _on_page_selected(self, _event: object) -> None:
        sel = self.lst_pages.curselection()
        if not sel or sel[0] >= len(self._sorted_stems):
            return
        # Index into the parallel stem list — never parse the display label
        # back apart (stems can legally contain the label's separator).
        stem = self._sorted_stems[sel[0]]
        if stem == self._stem:
            return
        if not self._confirm_discard(parent=self.winfo_toplevel()):
            self._select_list_row(self._stem)
            return
        self._open_page(stem)

    def _goto_page(self, delta: int) -> None:
        """Previous/next page in the list, from inside the page view."""
        if not self._sorted_stems:
            self.var_status.set("Load an object first.")
            return
        if self._stem in self._sorted_stems:
            i = self._sorted_stems.index(self._stem) + delta
        else:
            i = 0 if delta > 0 else len(self._sorted_stems) - 1
        if not 0 <= i < len(self._sorted_stems):
            self.var_status.set("That was the last page in the list." if delta > 0
                                else "That was the first page in the list.")
            return
        if not self._confirm_discard(parent=self._ws_parent()):
            return
        stem = self._sorted_stems[i]
        self._open_page(stem)
        self._select_list_row(stem)

    def _confirm_discard(self, parent: Optional[tk.Misc] = None) -> bool:
        """True → proceed (saved or discarded); False → stay on the page."""
        if self._page is None or not self._page.dirty:
            return True
        parent = parent or self.winfo_toplevel()
        ans = messagebox.askyesnocancel(
            "Review", f"Save changes to {self._stem} first?", parent=parent)
        if ans is None:
            return False
        if ans:
            self._save_page(parent=parent)
            # A failed save (the error dialog came from _save_page) leaves
            # the page dirty — stay on it instead of discarding the edits.
            return not self._page.dirty
        return True

    def _clear_view(self) -> None:
        """Forget the open page: nothing on the canvas, nothing to undo."""
        self._page = None
        self._pil = None
        self._photo = None
        self._photo_size = (0, 0)
        self._selected = None
        self._undo.clear()
        self._cancel_pending_add()
        self._order_sel = []
        self._order_anchor = None
        if self.var_reorder_mode.get():
            self._refresh_order_panel()
        self.canvas.delete("all")
        self.btn_save.configure(state="disabled")
        self._update_editor()

    def _open_page(self, stem: str) -> None:
        # Whatever happens below, this is now the current page — so ◀ Page /
        # Page ▶ move on from it even when it cannot be reviewed.
        self._clear_view()
        self._stem = stem
        self._update_window_title()
        img_path = self._img_index.get(stem)
        if img_path is None:
            self.var_status.set(f"{stem}: no matching source image in the source folder.")
            return
        try:
            pil = Image.open(img_path).convert("RGB")
        except OSError as e:
            self.var_status.set(f"{stem}: could not open {img_path.name}: {e}")
            return
        alto_path = Path(self.var_object.get().strip()) / "ALTO" / f"{stem}.xml"
        try:
            page = ReviewPage(alto_path, pil.size)
        except StubPageError as e:
            self.var_status.set(str(e))
            return
        except OSError as e:
            self.var_status.set(f"{stem}: could not read {alto_path.name}: {e}")
            return

        self._page = page
        self._pil = pil
        if self.var_reorder_mode.get():
            self._refresh_order_panel()
        self._render()
        self._update_editor()
        self._update_status()

    # ---------- rendering -------------------------------------------------

    def _zoom_scale(self) -> float:
        z = self.var_zoom.get()
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if z == "Fit":
            scale = min(cw / self._pil.width, ch / self._pil.height)
        elif z == "Fit width":
            scale = cw / self._pil.width
        else:
            scale = int(z.rstrip("%")) / 100.0
        max_scale = (_MAX_RENDER_PX / (self._pil.width * self._pil.height)) ** 0.5
        return min(max(scale, 0.02), max_scale)

    def _on_canvas_configure(self, _event: object = None) -> None:
        if self._pil is None or self.var_zoom.get() not in _FIT_ZOOMS:
            return
        if self._fit_after is not None:
            self.after_cancel(self._fit_after)
        self._fit_after = self.after(80, self._fit_now)

    def _fit_now(self) -> None:
        self._fit_after = None
        self._render()

    def _render(self) -> None:
        if self._pil is None or self._page is None:
            return
        scale = self._zoom_scale()
        w = max(int(self._pil.width * scale), 1)
        h = max(int(self._pil.height * scale), 1)
        if self._photo is None or (w, h) != self._photo_size:
            # Resampling a scan of hundreds of megapixels is the slow part;
            # overlay-only changes (threshold, edits, modes) reuse the image.
            resample = Image.LANCZOS if scale < 1 else Image.NEAREST
            self._photo = ImageTk.PhotoImage(self._pil.resize((w, h), resample))
            self._photo_size = (w, h)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, w, h))
        else:
            self.canvas.delete(_OVERLAY)
        self._scale = scale

        self._rect_by_word.clear()
        if self.var_reorder_mode.get():
            # Order view: every word, the selection, and the reading path.
            self._draw_order_overlays()
            self._drag_item = None
            return
        threshold = self._threshold()
        edited = self._touched_ids()
        for word in self._page.words:
            flagged = word.needs_review(threshold)
            if not (flagged or self.var_show_all.get() or id(word) in edited):
                continue
            x, y, bw, bh = word.box_px
            color = _COLOR_EDITED if id(word) in edited else _wc_color(word.wc)
            item = self.canvas.create_rectangle(
                x * scale, y * scale, (x + bw) * scale, (y + bh) * scale,
                outline=color, width=3 if word is self._selected else 2,
                tags=(_OVERLAY,),
            )
            self._rect_by_word[id(word)] = item

        # A pending add-word box survives re-renders (zoom, threshold
        # changes) — redraw it; the overlay sweep above dropped the item.
        if self._pending_box is not None:
            x, y, bw, bh = self._pending_box
            self._drag_item = self.canvas.create_rectangle(
                x * scale, y * scale, (x + bw) * scale, (y + bh) * scale,
                outline=_COLOR_EDITED, width=2, dash=(4, 2), tags=(_OVERLAY,),
            )
        else:
            self._drag_item = None

    def _threshold(self) -> float:
        value = parse_pct(self.var_threshold.get())
        return DEFAULT_LOW_WC if value is None else value

    def _on_threshold_changed(self) -> None:
        self._settings["review_threshold"] = self._threshold()
        self._persist()
        self._render()
        self._update_status()

    def _on_wheel(self, delta: int) -> None:
        self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")

    def _on_wheel_h(self, delta: int) -> None:
        self.canvas.xview_scroll(-1 if delta > 0 else 1, "units")

    def _on_wheel_zoom(self, event: tk.Event, delta: int) -> None:
        """Ctrl+scroll: zoom about the mouse cursor, ~20% per notch.
        Steps the zoom percentage (leaving "Fit" mode) and re-anchors the
        view so the image point under the cursor stays put."""
        if self._pil is None:
            return
        ix = self.canvas.canvasx(event.x) / self._scale
        iy = self.canvas.canvasy(event.y) / self._scale
        factor = 1.2 if delta > 0 else 1 / 1.2
        pct_zoom = max(5, min(400, round(self._scale * factor * 100)))
        if pct_zoom == round(self._scale * 100):
            return  # already at the zoom limit in this direction
        self.var_zoom.set(f"{pct_zoom}%")
        self._render()
        w = max(int(self._pil.width * self._scale), 1)
        h = max(int(self._pil.height * self._scale), 1)
        self.canvas.xview_moveto(max(0.0, min(1.0, (ix * self._scale - event.x) / w)))
        self.canvas.yview_moveto(max(0.0, min(1.0, (iy * self._scale - event.y) / h)))

    # ---------- selection / editing --------------------------------------

    def _on_canvas_click(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self._page is None:
            return
        if self.var_reorder_mode.get():
            # Reorder mode: a click selects words or places the selection; a
            # drag boxes words. Release decides which it was.
            self._order_press(event, "plain")
            return
        if self.var_add_mode.get():
            # Add mode: the click starts a rubber-band drag, not a selection.
            self._cancel_pending_add()
            self._drag_start = (self.canvas.canvasx(event.x),
                                self.canvas.canvasy(event.y))
            return
        x = self.canvas.canvasx(event.x) / self._scale
        y = self.canvas.canvasy(event.y) / self._scale
        self._select_word(self._page.word_at(x, y))

    # ---------- add-word mode ---------------------------------------------

    def _toggle_add_mode(self) -> None:
        self.var_add_mode.set(not self.var_add_mode.get())
        self._on_add_mode_toggled()

    def _on_add_mode_toggled(self) -> None:
        on = self.var_add_mode.get()
        if on and self.var_reorder_mode.get():
            # The two canvas modes are mutually exclusive.
            self.var_reorder_mode.set(False)
            self._teardown_reorder()
        self.canvas.configure(cursor="crosshair" if on else "")
        if not on:
            self._cancel_pending_add()
        elif self._page is not None:
            self.var_status.set(
                "Add word: drag a box around the missed word on the scan, "
                "type the word, press Enter.")

    # ---------- reading-order mode ----------------------------------------

    def _toggle_reorder_mode(self) -> None:
        self.var_reorder_mode.set(not self.var_reorder_mode.get())
        self._on_reorder_toggled()

    def _on_reorder_toggled(self) -> None:
        if not self.var_reorder_mode.get():
            self._teardown_reorder()
            return
        if self.var_add_mode.get():
            self.var_add_mode.set(False)
            self._on_add_mode_toggled()
        self._select_word(None)
        self._cancel_pending_add()
        self.order_panel.grid()
        self._refresh_order_panel()
        self._render()
        self.canvas.focus_set()

    def _teardown_reorder(self) -> None:
        self._order_sel = []
        self._order_anchor = None
        self.order_panel.grid_remove()
        self._render()

    def _order_hint(self) -> str:
        if not self._order_sel:
            return ("Select the words that are out of place: click one (on the "
                    "scan or in the transcript below), Shift+click to extend to "
                    "a run, Ctrl+click to add or remove a word, or drag a box "
                    "around a whole column or caption on the scan.")
        n = len(self._order_sel)
        return (f"{n} word(s) selected. Now click the word they should come "
                "after (“Page start” puts them first). Ctrl+Up/Ctrl+Down nudge "
                "one word; Esc clears; Ctrl+Z undoes a move.")

    def _refresh_order_panel(self) -> None:
        """Re-render the transcript (exactly what a save will write), mark
        the selection in it, and refresh the hint and buttons."""
        page = self._page
        words = {id(w.elem): w for w in page.words} if page is not None else {}
        # Words deleted or cleared since they were selected drop out.
        self._order_sel = [w for w in self._order_sel if id(w.elem) in words]
        if self._order_anchor is not None and id(self._order_anchor.elem) not in words:
            self._order_anchor = self._order_sel[0] if self._order_sel else None
        self.var_order_hint.set(self._order_hint())
        state = "normal" if self._order_sel else "disabled"
        for b in self._order_buttons:
            b.configure(state=state)

        txt = self.txt_order
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        self._order_spans = []
        if page is not None:
            text, spans = transcript_spans(page.root)
            txt.insert("1.0", text or "(no words on this page)")
            self._order_spans = [(a, b, words[id(el)]) for el, a, b in spans
                                 if id(el) in words]
            chosen = {id(w) for w in self._order_sel}
            first = None
            prev_end = None  # end of the previous word, when it was chosen too
            for a, b, w in self._order_spans:
                if id(w) in chosen:
                    start = prev_end if prev_end is not None else a
                    txt.tag_add("chosen", f"1.0+{start}c", f"1.0+{b}c")
                    prev_end = b
                    if first is None:
                        first = a
                else:
                    prev_end = None
            if first is not None:
                txt.see(f"1.0+{first}c")
        txt.configure(state="disabled")

    def _order_word_in_text(self, event: tk.Event) -> Optional[Word]:
        """The word under a click in the transcript panel, if any."""
        return self._order_word_at(f"@{event.x},{event.y}")

    def _order_word_at(self, index: str) -> Optional[Word]:
        """The word at a transcript-panel text index (a click lands between
        characters, so a word's end counts as on it)."""
        if not self._order_spans:
            return None
        offset = self.txt_order.count("1.0", index, "chars")
        offset = offset[0] if offset else 0
        i = bisect.bisect_right([a for a, _b, _w in self._order_spans], offset) - 1
        if i < 0:
            return None
        a, b, word = self._order_spans[i]
        return word if a <= offset <= b else None

    def _on_order_text_click(self, event: tk.Event, shift: bool) -> str:
        self._order_pick(self._order_word_in_text(event), shift)
        return "break"  # no text selection / insert cursor in the panel

    def _on_order_text_toggle(self, event: tk.Event) -> str:
        self._order_toggle(self._order_word_in_text(event))
        return "break"

    def _order_pick(self, word: Optional[Word], shift: bool) -> None:
        """One click in reading-order mode, on the scan or in the transcript.

        Nothing selected: the word becomes the selection. Shift: the
        selection becomes every word from the first one picked to this one.
        A selection and a plain click elsewhere: the selection moves to read
        right after the clicked word. A plain click inside the selection
        starts over from that word."""
        if self._page is None:
            return
        if word is None:
            if not shift and self._order_sel:
                self._set_order_selection([], None)
            return
        if shift and self._order_anchor is not None:
            i = self._page.doc_index(self._order_anchor)
            j = self._page.doc_index(word)
            if i is not None and j is not None:
                lo, hi = sorted((i, j))
                self._set_order_selection(self._page.words[lo:hi + 1], self._order_anchor)
                return
        if not self._order_sel or shift or any(w is word for w in self._order_sel):
            self._set_order_selection([word], word)
            return
        self._order_apply(lambda: self._page.move_words(self._order_sel, word),
                          f"after “{word.content}”")

    def _order_toggle(self, word: Optional[Word]) -> None:
        """Ctrl+click: add a word to the selection, or take it out."""
        if word is None or self._page is None:
            return
        if any(w is word for w in self._order_sel):
            rest = [w for w in self._order_sel if w is not word]
            anchor = self._order_anchor if self._order_anchor is not word else None
            self._set_order_selection(rest, anchor or (rest[0] if rest else None))
        else:
            self._set_order_selection(self._order_sel + [word], word)

    def _order_box_select(self, box: tuple[float, float, float, float], add: bool) -> None:
        """Select the words whose centers fall inside an image-px box — a
        column or a caption in one drag, where Tesseract's lines interleave
        it with the text beside it. ``add`` keeps the current selection."""
        x, y, bw, bh = box
        hits = [w for w in self._page.words
                if x <= w.box_px[0] + w.box_px[2] / 2 <= x + bw
                and y <= w.box_px[1] + w.box_px[3] / 2 <= y + bh]
        if add:
            have = {id(w) for w in self._order_sel}
            hits = self._order_sel + [w for w in hits if id(w) not in have]
        if not hits:
            self.var_status.set("No words inside that box.")
            return
        self._set_order_selection(hits, self._order_anchor if add and self._order_anchor
                                  else hits[0])

    def _set_order_selection(self, words: list[Word], anchor: Optional[Word]) -> None:
        # Kept in reading order, which is the order a move carries them in.
        pos = {id(w): i for i, w in enumerate(self._page.words)} if self._page else {}
        self._order_sel = sorted(words, key=lambda w: pos.get(id(w), 1 << 30))
        self._order_anchor = anchor
        self._refresh_order_panel()
        self._render()

    def _order_nudge(self, delta: int) -> None:
        if self.var_reorder_mode.get() and self._order_sel:
            self._order_apply(lambda: self._page.nudge_words(self._order_sel, delta),
                              "one word earlier" if delta < 0 else "one word later")

    def _order_to_start(self) -> None:
        if self._order_sel:
            self._order_apply(lambda: self._page.move_words(self._order_sel, None),
                              "to the start of the page")

    def _order_apply(self, do_move: Callable[[], Optional[WordMove]], where: str) -> None:
        """Run one move of the selection; keep the selection so it can be
        nudged again."""
        if self._page is None or not self._order_sel:
            return
        try:
            move = do_move()
        except ValueError as e:
            self.var_status.set(str(e))
            self.bell()
            return
        if move is None:
            self.var_status.set("Nothing to move — the words are already read there.")
            return
        self._undo.append(_MoveEntry(move))
        self.btn_save.configure(state="normal")
        self._refresh_order_panel()
        self._render()
        self._update_status(f"Moved {len(self._order_sel)} word(s) {where}. "
                            "Ctrl+Z undoes.")

    def _on_canvas_modified_click(self, event: tk.Event, mode: str) -> None:
        """Shift/Ctrl+press on the scan: a modified pick or an additive box
        in reading-order mode; an ordinary click anywhere else."""
        if not self.var_reorder_mode.get():
            self._on_canvas_click(event)
            return
        self.canvas.focus_set()
        if self._page is not None:
            self._order_press(event, mode)

    def _order_press(self, event: tk.Event, mode: str) -> None:
        self._order_press_at = (self.canvas.canvasx(event.x),
                                self.canvas.canvasy(event.y), mode)
        self._order_rect = None

    def _order_drag(self, event: tk.Event) -> None:
        x0, y0, _mode = self._order_press_at
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self._order_rect is None:
            if abs(x1 - x0) < 5 and abs(y1 - y0) < 5:
                return  # still a click: hands wobble
            self._order_rect = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=_COLOR_EDITED, width=2, dash=(4, 2),
                tags=(_OVERLAY,))
        else:
            self.canvas.coords(self._order_rect, x0, y0, x1, y1)

    def _order_release(self, event: tk.Event) -> None:
        x0, y0, mode = self._order_press_at
        self._order_press_at = None
        if self._page is None:
            return
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self._order_rect is not None:
            self.canvas.delete(self._order_rect)
            self._order_rect = None
            s = self._scale
            self._order_box_select((min(x0, x1) / s, min(y0, y1) / s,
                                    abs(x1 - x0) / s, abs(y1 - y0) / s),
                                   add=mode != "plain")
            return
        word = self._page.word_at(x0 / self._scale, y0 / self._scale)
        if mode == "ctrl":
            self._order_toggle(word)
        else:
            self._order_pick(word, shift=mode == "shift")

    def _on_canvas_escape(self) -> None:
        if self.var_reorder_mode.get() and self._order_sel:
            self._set_order_selection([], None)
        else:
            self._on_escape()

    def _draw_order_overlays(self) -> None:
        """Every word outlined, the selection heavy, and the reading path:
        a thin line from word to word through each line, and an arrow from
        the end of one line to the start of the next — so a wrong order is
        visible at a glance. Redrawn from state inside _render."""
        s = self._scale
        chosen = {id(w) for w in self._order_sel}
        prev = None
        for word in self._page.words:
            x, y, bw, bh = (v * s for v in word.box_px)
            center = (x + bw / 2, y + bh / 2)
            if prev is not None:
                same_line = prev[0].elem.getparent() is word.elem.getparent()
                self.canvas.create_line(
                    *prev[1], *center, fill=_COLOR_ORDER,
                    width=1 if same_line else 2,
                    dash=() if same_line else (6, 3),
                    arrow="none" if same_line else "last",
                    tags=(_OVERLAY,))
            prev = (word, center)
        for word in self._page.words:
            x, y, bw, bh = (v * s for v in word.box_px)
            selected = id(word) in chosen
            self.canvas.create_rectangle(
                x, y, x + bw, y + bh,
                outline=_COLOR_EDITED if selected else _COLOR_ORDER,
                width=3 if selected else 1, tags=(_OVERLAY,))

    def _cancel_pending_add(self) -> None:
        self._drag_start = None
        if self._drag_item is not None:
            self.canvas.delete(self._drag_item)
            self._drag_item = None
        if self._pending_box is not None:
            self._pending_box = None
            self._update_editor()

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._order_press_at is not None:
            self._order_drag(event)
            return
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self._drag_item is None:
            self._drag_item = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=_COLOR_EDITED, width=2, dash=(4, 2),
                tags=(_OVERLAY,))
        else:
            self.canvas.coords(self._drag_item, x0, y0, x1, y1)

    def _on_canvas_release(self, event: tk.Event) -> None:
        if self._order_press_at is not None:
            self._order_release(event)
            return
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self._drag_start = None
        box = (min(x0, x1) / self._scale, min(y0, y1) / self._scale,
               abs(x1 - x0) / self._scale, abs(y1 - y0) / self._scale)
        if box[2] < 3 or box[3] < 3:  # a stray click, not a drawn box
            self._cancel_pending_add()
            return
        self._pending_box = box
        self._selected = None
        self._update_editor()
        self.var_status.set("Type the word for the drawn box, then press Enter "
                            "(Escape cancels).")
        self.ent_edit.focus_set()

    def _commit_add(self) -> None:
        if self._page is None or self._pending_box is None:
            return
        text = self.var_edit.get()
        if not text.strip():
            self._cancel_pending_add()
            self.var_status.set("Add word cancelled (no text).")
            return
        try:
            word = self._page.add_word(self._pending_box, text)
        except ValueError as e:
            self.var_status.set(str(e))
            self.bell()
            return
        self._undo.append(_AddEntry(word))
        self._cancel_pending_add()
        self.btn_save.configure(state="normal")
        self._render()
        self._select_word(word)
        self._update_status()

    def _on_escape(self) -> None:
        if self._pending_box is not None:
            self._cancel_pending_add()
            self.canvas.focus_set()
        else:
            self._select_word(None)

    # ---------- selection -------------------------------------------------

    def _select_word(self, word: Optional[Word]) -> None:
        if word is not None and self._pending_box is not None:
            # Selecting a real word supersedes an un-committed add box.
            self._pending_box = None
            if self._drag_item is not None:
                self.canvas.delete(self._drag_item)
                self._drag_item = None
        prev = self._selected
        self._selected = word
        for w, width in ((prev, 2), (word, 3)):
            if w is not None:
                item = self._rect_by_word.get(id(w))
                if item is not None:
                    self.canvas.itemconfigure(item, width=width)
        self._update_editor()
        if word is not None:
            self._scroll_to_word(word)
            self.ent_edit.focus_set()
            self.ent_edit.selection_range(0, "end")

    def _scroll_to_word(self, word: Word) -> None:
        x, y, bw, bh = word.box_px
        w = max(int(self._pil.width * self._scale), 1)
        h = max(int(self._pil.height * self._scale), 1)
        # Center the word in the visible area, clamped to [0, 1].
        cx = ((x + bw / 2) * self._scale - self.canvas.winfo_width() / 2) / w
        cy = ((y + bh / 2) * self._scale - self.canvas.winfo_height() / 2) / h
        self.canvas.xview_moveto(max(0.0, min(1.0, cx)))
        self.canvas.yview_moveto(max(0.0, min(1.0, cy)))

    def _set_snippet(self, x: float, y: float, bw: float, bh: float) -> None:
        """Show a magnified crop of the scan region in the editor bar."""
        pad = 8
        crop = self._pil.crop((
            max(0, int(x - pad)), max(0, int(y - pad)),
            min(self._pil.width, int(x + bw + pad)),
            min(self._pil.height, int(y + bh + pad)),
        ))
        target_h = 48
        scale = target_h / max(crop.height, 1)
        crop = crop.resize((max(int(crop.width * scale), 1), target_h), Image.LANCZOS)
        self._snippet_photo = ImageTk.PhotoImage(crop)
        self.lbl_snippet.configure(image=self._snippet_photo)

    def _update_editor(self) -> None:
        if self._pending_box is not None and self._pil is not None:
            self._set_snippet(*self._pending_box)
            self.lbl_wc.configure(text="New word")
            self.var_edit.set("")
            return
        word = self._selected
        if word is None or self._pil is None:
            self.lbl_snippet.configure(image="")
            self._snippet_photo = None
            self.lbl_wc.configure(text="—")
            self.var_edit.set("")
            return
        self._set_snippet(*word.box_px)
        self.lbl_wc.configure(
            text=f"Confidence {pct(word.wc)}" if word.wc is not None
            else "Confidence —  (unscored)")
        self.var_edit.set(word.content)

    def _apply_edit(self, advance: bool = False) -> None:
        if self._pending_box is not None:
            self._commit_add()
            return
        word = self._selected
        if word is None or self._page is None:
            return
        new_text = self.var_edit.get()
        if not new_text.strip():
            self._delete_word()
            return
        if " ".join(new_text.split()) != word.content:
            old_content, old_wc = word.content, word.wc
            try:
                self._page.edit_word(word, new_text)
            except ValueError as e:  # text ALTO cannot store: nothing changed
                self.var_status.set(str(e))
                self.bell()
                return
            self._undo.append(_EditEntry(word, old_content, old_wc))
            self._mark_touched(word)
        elif word.needs_review(self._threshold()):
            # Enter on a word left as-is: the OCR got this one right, the
            # confidence was just low. Confirm it (WC=1.00) so it leaves the
            # queue instead of being re-served on the next pass — but count
            # it apart from real corrections in the provenance.
            old_wc = word.wc
            if self._page.verify_word(word):
                self._undo.append(_VerifyEntry(word, old_wc))
                self._mark_touched(word)
        self._update_status()
        if advance:
            self._goto_flagged(+1)

    def _mark_touched(self, word: Word) -> None:
        """Recolor a word the corrector just settled (edited or confirmed)
        and enable the save button."""
        item = self._rect_by_word.get(id(word))
        if item is not None:
            self.canvas.itemconfigure(item, outline=_COLOR_EDITED)
        self.btn_save.configure(state="normal")

    def _delete_word(self) -> None:
        """Delete the selected word at once — no confirmation prompt; the
        deletion goes on the undo stack and Ctrl+Z restores it exactly."""
        word = self._selected
        if word is None or self._page is None:
            return
        nxt = self._next_after(word)
        added = any(isinstance(e, _AddEntry) and e.word is word for e in self._undo)
        if added:
            # A word added this session: net it out of the provenance count
            # instead of counting an add plus a delete.
            removal = self._page.undo_add(word)
        else:
            removal = self._page.delete_word(word)
        self._undo.append(_DeleteEntry(word, removal, added))
        self._selected = None
        self.btn_save.configure(state="normal")
        self._render()
        self._select_word(nxt)
        self._update_status(f"Deleted “{word.content}” (Ctrl+Z puts it back).")

    def _clear_page(self) -> None:
        """Bulk-delete every word — for pages whose OCR is wholly
        hallucinated (covers, photographs: texture read as text)."""
        if self._page is None:
            return
        n = len(self._page.words)
        if n == 0:
            self.var_status.set("Page has no words to clear.")
            return
        if not messagebox.askyesno(
            "Review",
            f"Remove all {n} word(s) from this page?\n\n"
            "Use this when the whole page is hallucinated OCR — texture or "
            "noise read as text on covers, photographs, or blank pages.\n\n"
            "This can't be undone except by reloading the page unsaved.",
            parent=self._ws_parent(),
        ):
            return
        removed = self._page.clear_words()
        self._undo.clear()
        self._selected = None
        self._cancel_pending_add()
        self._order_sel = []
        self._order_anchor = None
        if self.var_reorder_mode.get():
            self._refresh_order_panel()
        self.btn_save.configure(state="normal")
        self._render()
        self._update_editor()
        self.var_status.set(
            f"{self._stem}: cleared {removed} word(s) — page is now empty. "
            "Save page to keep this.")

    def _undo_edit(self) -> None:
        if not self._undo or self._page is None:
            return
        entry = self._undo.pop()
        if isinstance(entry, _MoveEntry):
            self._page.undo_move_words(entry.move)
            self._maybe_disable_save()
            if self.var_reorder_mode.get():
                self._refresh_order_panel()
            self._render()
            self._update_status()
            return
        if isinstance(entry, _AddEntry):  # remove the added word again
            self._page.undo_add(entry.word)
            if self._selected is entry.word:
                self._selected = None
            self._maybe_disable_save()
            if self.var_reorder_mode.get():  # the add's containers may be gone
                self._refresh_order_panel()
            self._render()
            self._update_editor()
            self._update_status()
            return
        if isinstance(entry, _DeleteEntry):  # put the deleted word back
            if entry.added:
                self._page.redo_add(entry.word, entry.removal)
            else:
                self._page.undo_delete(entry.word, entry.removal)
            self._maybe_disable_save()
            if self.var_reorder_mode.get():  # its containers are back too
                self._refresh_order_panel()
            self._render()
            self._select_word(entry.word)
            self._update_status(f"Restored “{entry.word.content}”.")
            return
        if isinstance(entry, _VerifyEntry):  # un-confirm: engine WC back
            self._page.unverify_word(entry.word, entry.old_wc)
        else:
            self._page.revert_word(entry.word, entry.old_content, entry.old_wc)
        item = self._rect_by_word.get(id(entry.word))
        if item is not None:
            self.canvas.itemconfigure(item, outline=_wc_color(entry.word.wc))
        self._maybe_disable_save()
        self._select_word(entry.word)
        self._update_status()

    def _maybe_disable_save(self) -> None:
        """After an undo: a page back exactly as it was loaded (or last
        saved) has nothing to save — no Save button, no "Save changes?"."""
        if not self._page.settle():
            self.btn_save.configure(state="disabled")

    def _touched_ids(self) -> set[int]:
        """Words this session has edited, confirmed, or added — drawn in the
        edited color and kept out of the flagged queue."""
        return {id(e.word) for e in self._undo
                if isinstance(e, (_EditEntry, _VerifyEntry, _AddEntry))}

    def _flagged_now(self) -> list[Word]:
        if self._page is None:
            return []
        touched = self._touched_ids()
        return [w for w in self._page.flagged(self._threshold()) if id(w) not in touched]

    def _next_after(self, word: Word) -> Optional[Word]:
        if self._page is None:
            return None
        return self._page.step_from(word, self._flagged_now(), +1)

    def _goto_flagged(self, delta: int) -> None:
        """Step to the next/previous flagged word *from where the corrector
        is on the page*. Position, not membership in the flagged list: a word
        just corrected is no longer flagged (``WC="1.00"``), and looking it up
        in that list is what used to bounce the corrector back to the page's
        first flagged word after every edit."""
        flagged = self._flagged_now()
        if not flagged or self._page is None:
            self._select_word(None)
            self.var_status.set("No flagged words left on this page. Save and pick the next page.")
            return
        current = self._selected
        if current is None:
            self._select_word(flagged[0] if delta > 0 else flagged[-1])
            return
        nxt = self._page.step_from(current, flagged, delta)
        if nxt is None:  # the selected word is the only one still flagged
            self.var_status.set(
                "This is the only flagged word left on this page — correct it, "
                "then save and pick the next page.")
            return
        wrapped = self._wrapped(current, nxt, delta)
        self._select_word(nxt)
        if wrapped:
            edge = "top" if delta > 0 else "bottom"
            self.var_status.set(
                f"Wrapped to the {edge} of the page — {len(flagged)} flagged "
                f"word(s) still unreviewed.")

    def _wrapped(self, current: Word, nxt: Word, delta: int) -> bool:
        """True when stepping ran off the end of the page and came back
        around — worth saying out loud, so a corrector who lands back near
        the top knows they finished the page rather than lost their place."""
        here = self._page.doc_index(current)
        there = self._page.doc_index(nxt)
        if here is None or there is None:
            return False
        return there < here if delta > 0 else there > here

    # ---------- save / status --------------------------------------------

    def _commit_typed_text(self) -> None:
        """Before a save: apply a correction typed into the editor but not
        yet applied with Enter, so Ctrl+S / Save page never drops it."""
        text = self.var_edit.get()
        if not text.strip():
            return
        if self._pending_box is not None:
            self._commit_add()
        elif (self._selected is not None
              and " ".join(text.split()) != self._selected.content):
            self._apply_edit(advance=False)

    def _save_page(self, parent: Optional[tk.Misc] = None) -> None:
        if self._page is None:
            return
        self._commit_typed_text()
        if not self._page.dirty:
            return
        parent = parent or self._ws_parent()
        try:
            txt = self._page.save()
        except ConcurrentEditError as e:
            # Someone else wrote this ALTO while it was open here. Only the
            # corrector can decide whose version wins.
            if not messagebox.askyesno(
                "Review",
                f"{e}\n\nOverwrite the on-disk version with your edits?",
                parent=parent,
            ):
                return
            try:
                txt = self._page.save(force=True)
            except OSError as e2:
                messagebox.showerror("Review", f"Save failed:\n{e2}", parent=parent)
                return
        except OSError as e:
            messagebox.showerror("Review", f"Save failed:\n{e}", parent=parent)
            return
        self._undo.clear()
        self.btn_save.configure(state="disabled")
        # Refresh this page's PC in the list (save recomputed it) — in its
        # row, without re-ranking the list under the corrector.
        info = scan_alto_file_safe(self._page.alto_path)
        self._pages = [
            (stem, info.pc if stem == self._stem else pc,
             info.status if stem == self._stem else status)
            for stem, pc, status in self._pages
        ]
        if self._stem in self._sorted_stems:
            i = self._sorted_stems.index(self._stem)
            self.lst_pages.delete(i)
            self.lst_pages.insert(i, self._page_label(self._stem, info.pc, info.status))
            self._select_list_row(self._stem)
        self._render()
        extra = f" + {txt.name}" if txt is not None else ""
        self.var_status.set(f"Saved {self._page.alto_path.name}{extra}.")

    def _update_status(self, note: str = "") -> None:
        if self._page is None:
            return
        n_flagged = len(self._flagged_now())
        dirty = "  •  unsaved changes" if self._page.dirty else ""
        summary = (f"{self._stem}: {len(self._page.words)} words, {n_flagged} flagged "
                   f"below {threshold_text(self._threshold())}{dirty}")
        self.var_status.set(f"{note}  {summary}" if note else summary)

    # ---------- repackage -------------------------------------------------

    def _repackage(self) -> None:
        """Refresh METS sizes/checksums (and any drifted transcripts) from
        the corrected ALTO — the Review-tab face of the CLI ``repackage``
        command, for the end of an object's review session. Accepts either
        one object folder or an output root holding several."""
        if self._repack_worker is not None and self._repack_worker.is_alive():
            return  # already running
        main = self.winfo_toplevel()
        path = Path(self.var_object.get().strip())
        dirs = iter_object_dirs(path) if path.is_dir() else []
        if not dirs:
            messagebox.showerror(
                "Review",
                f"No ALTO folder found under:\n{path}\n\n"
                "Point 'Transcript Folder' at an object's OCR output folder "
                "(or an output root containing several) first.", parent=main)
            return
        if self._page is not None and self._page.dirty:
            ans = messagebox.askyesnocancel(
                "Review",
                f"{self._stem} has unsaved changes.\n\n"
                "Save it first so the repackaged METS covers them?", parent=main)
            if ans is None:
                return
            if ans:
                self._save_page(parent=main)
                if self._page.dirty:  # save failed; its error dialog already showed
                    return
        self.btn_repackage.configure(state="disabled")
        self.var_status.set(f"Repackaging {len(dirs)} object(s)…")
        self._repack_worker = threading.Thread(
            target=self._repackage_worker, args=(dirs, self._repack_queue),
            daemon=True)
        self._repack_worker.start()
        self.after(150, self._poll_repackage)

    @staticmethod
    def _repackage_worker(dirs: list[Path],
                          q: "queue.Queue[list[RepackageResult]]") -> None:
        results: list[RepackageResult] = []
        for d in dirs:
            try:
                results.append(repackage_object(d))
            except Exception as e:  # noqa: BLE001 — report per object, never crash the thread
                logger.exception("repackage failed for %s", d)
                results.append(RepackageResult(
                    ref_id=d.name, object_dir=d, pages=0, txt_written=0,
                    mets_written=False, problems=[str(e)]))
        q.put(results)

    def _poll_repackage(self) -> None:
        try:
            results = self._repack_queue.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_repackage)
            return
        self.btn_repackage.configure(state="normal")
        msg = repackage_summary(results)
        problems = [f"{r.ref_id}: {p}" for r in results for p in r.problems]
        if problems:
            shown = "\n".join(problems[:12])
            if len(problems) > 12:
                shown += f"\n… and {len(problems) - 12} more"
            messagebox.showwarning("Review", f"{msg}\n\nProblems:\n{shown}",
                                   parent=self.winfo_toplevel())
            msg += f"  {len(problems)} problem(s) — see warning."
        self.var_status.set(msg)

    # ---------- app integration ------------------------------------------

    def has_unsaved_changes(self) -> bool:
        return self._page is not None and self._page.dirty

    def confirm_close(self) -> bool:
        """Called by the app's close handler; True → OK to quit."""
        if not self._confirm_discard(parent=self.winfo_toplevel()):
            return False
        # Where the pop-out windows were, for next time.
        self._page_window.remember_geometry()
        self._kb_window.remember_geometry()
        return True
