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
next flagged word, **Escape** deselects, **n/p** step through flagged words,
**Tab/Shift+Tab** step too — from inside the editor as well, where n and p
must type — **Delete** (or **Ctrl+D** anywhere) removes the selected
word(s), **Ctrl+Z** undoes the last change, **Ctrl+S** saves. Shortcuts are
routed by one tab-level dispatcher (``_on_key``, ``bind_all``), not by
bindings on the canvas: they work wherever keyboard focus happens to sit —
on a toolbar button just clicked, the page list, the scan — and only the
text fields keep the plain keys for typing. Until v0.10.4 they were canvas
bindings, and because selecting a word (which n/p themselves do) moves focus
into the editor, the second ``n`` typed an "n" over the word instead of
stepping; a click on any toolbar button silenced Esc the same way. Enter
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

**Selecting several words** works the same way in every mode: Shift+click
extends to a run, Ctrl+click adds or drops a word, dragging a box on the
scan selects the words whose centres fall inside it, a **double-click**
selects the word's whole line and a **triple-click** its block. With
several words selected the editor bar shows the count and **Delete** (the
key, the button, Ctrl+D, or the right-click menu) removes them all in one
step — a garbage line or a column of noise no longer has to be deleted
word by word or by clearing the whole page — and one Ctrl+Z puts them all
back (``remediate.ReviewPage.delete_words``). Outside reading-order mode
only words drawn on the scan are selectable this way, so with "All words"
off a box cannot sweep up words the corrector cannot see. A **right-click**
on the scan opens a menu of the same actions (edit, delete, select line /
block, and in reading-order mode the move targets) for correctors who
would rather not memorise keys.

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
after, or **drag the selection and drop it on that word**; the right-click
menu offers "after" and "before" the clicked word; "Page start" puts them
first, Ctrl+Up/Ctrl+Down nudge them one word. The scan shows the
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from . import theme
from .alto_parse import upright
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

# Files read at once while loading an object. Loading is file I/O, mostly
# over SMB, where each read waits on a network round trip: overlapping them
# is what makes a 300-page object load in a fraction of the serial time.
_LOAD_WORKERS = 8

PageRow = tuple[str, Optional[float], str]  # (stem, pc, status)


def scan_object_pages(
    alto_dir: Path,
    source_dir: Path,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[dict[str, Path], list[PageRow]]:
    """Everything "Load object" needs from disk: the source-image index and
    each page's confidence summary, in natural page order.

    Headless and thread-safe, so the Review tab runs it off the Tk thread.
    The image walk and the ALTO reads overlap on one pool; ``progress`` is
    called with (pages scanned, total) from the calling thread.
    """
    xmls = sorted(alto_dir.glob("*.xml"), key=natural_key)
    with ThreadPoolExecutor(max_workers=_LOAD_WORKERS) as pool:
        index = pool.submit(index_source_images, source_dir)
        pages: list[PageRow] = []
        for i, info in enumerate(pool.map(scan_alto_file_safe, xmls), start=1):
            pages.append((xmls[i - 1].stem, info.pc, info.status))
            if progress is not None:
                progress(i, len(xmls))
        return index.result(), pages

# "Fit" shows the whole page; "Fit width" fills the view's width and
# scrolls vertically — the useful one in a wide or popped-out window.
_ZOOMS: tuple[str, ...] = ("Fit", "Fit width", "50%", "75%", "100%", "150%", "200%")
_FIT_ZOOMS = ("Fit", "Fit width")

# "Flag below" choices: whole percentages, the way the tab shows confidence.
THRESHOLD_CHOICES: tuple[str, ...] = tuple(f"{p}%" for p in range(0, 101, 5))

# Narrowest the page list and the page view can be dragged (screen px).
_MIN_LIST_PX = 120
_MIN_PAGE_PX = 480

# WC → outline color. Blue marks words edited this session (WC becomes 1.00
# on disk, but the corrector should still see what they touched).
_COLOR_EDITED = "#1f6feb"
_COLOR_UNSCORED = "#c026d3"  # no WC (GLMOCR output): needs eyes everywhere
# The selection, outside reading-order mode: one colour for one word or
# fifty, distinct from every confidence colour and from edited blue.
_COLOR_SELECTED = "#7c3aed"
# Reading-order mode. The path has to read at a glance on a page of black
# type, so it is a saturated colour, drawn heavy on a thin white casing; the
# word boxes recede to a thin neutral grey so path and boxes never blur
# together (they were one purple, the path 1–2 px, until 2026-10-05). The
# selection stays _COLOR_EDITED blue. Widths are screen pixels at any zoom.
# Line breaks are told apart by their arrowheads, not by dashes: Tk on
# Windows draws every dash pattern as fine dots whatever the line width.
_COLOR_ORDER_BOX = "#64748b"
_COLOR_ORDER_PATH = "#db2777"
_COLOR_ORDER_CASING = "#ffffff"
# Full weight once words are this tall on screen; zoomed out further the
# path thins (to no less than _ORDER_MIN_WEIGHT of it) so it doesn't bury
# type that small. Arrowshape is Tk's (length, wing length, half-width).
_ORDER_FULL_WEIGHT_PX = 28
_ORDER_MIN_WEIGHT = 0.45
_ORDER_PATH_WIDTH = 3
_ORDER_CASING_WIDTH = 5
_ORDER_ARROW = (15, 18, 7)
_ORDER_CASING_ARROW = (17, 21, 8)

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
    """One deletion gesture — a single word or a whole selection — undone
    as one step. ``added`` marks words added this session: their undo
    re-adds them (``redo_add``) instead of restoring an engine word."""

    items: list[tuple[Word, Removal, bool]]  # (word, removal, added), deletion order

    @property
    def word(self) -> Word:
        return self.items[0][0]

    @property
    def words(self) -> list[Word]:
        return [w for w, _r, _a in self.items]


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


Box = tuple[float, float, float, float]


def order_link(a: Box, b: Box, same_line: bool) -> tuple[float, float, float, float]:
    """Where to draw the reading-path link from word ``a`` to word ``b``
    (``x, y, w, h`` boxes): ``(x1, y1, x2, y2)``.

    Links run between the words, not through them, so the path never
    strikes out the text the corrector is reading. Along a line: edge to
    facing edge at mid-height — the gap between two words. Across a line
    break: from the corner of the line's last word to the corner of the
    next line's first word that faces it (bottom → top going down, top →
    bottom going up), which keeps the diagonal in the space between the
    lines and puts the arrowhead at the corner of the word that comes
    next. Overlapping words on one line fall back to centre to centre.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if same_line:
        if bx >= ax + aw:
            return (ax + aw, ay + ah / 2, bx, by + bh / 2)
        if bx + bw <= ax:
            return (ax, ay + ah / 2, bx + bw, by + bh / 2)
        return (ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2)
    if by + bh / 2 >= ay + ah / 2:
        return (ax + aw, ay + ah, bx, by)
    return (ax + aw, ay, bx, by + bh)


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

        self._pages: list[PageRow] = []
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
        # The selection, in reading order, in every mode: the words a
        # Delete removes, a reading-order move carries, and — when it is
        # exactly one word — the word in the editor bar (``_selected``).
        self._sel: list[Word] = []
        self._anchor: Optional[Word] = None       # where Shift+click extends from
        self._selected: Optional[Word] = None
        self._undo: list[_EditEntry | _VerifyEntry | _AddEntry
                         | _DeleteEntry | _MoveEntry] = []
        # Reading-order mode: the transcript panel's (start, end, word)
        # character spans and its buttons.
        self._order_spans: list[tuple[int, int, Word]] = []
        self._order_buttons: list[ttk.Button] = []
        # A press on the scan (outside add mode): (canvas x, y, modifier)
        # until release decides whether it was a click, a box drag, or — in
        # reading-order mode, from a selected word — a drag of the selection
        # to drop on its new place.
        self._press_at: Optional[tuple[float, float, str]] = None
        self._press_word: Optional[Word] = None
        self._drag_rect: Optional[int] = None
        self._dropping = False
        self._menu: Optional[tk.Menu] = None
        # True right after a click moved words, so the double-click that
        # click began does not also select a line.
        self._last_click_moved = False
        self._windowing = str(self.tk.call("tk", "windowingsystem"))
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
        # "Load object" reads every ALTO in the object, which over SMB took
        # seconds on a large object with the window frozen; it runs on a
        # worker too. Each load gets a fresh queue, so a superseded load's
        # late messages land in a queue nobody polls.
        self._load_queue: Optional[queue.Queue] = None

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
        self.btn_load = ttk.Button(paths, text="Load object", command=self.load_object)
        self.btn_load.grid(row=0, column=3, rowspan=2, padx=(10, 0), sticky="ns")
        self.btn_repackage = ttk.Button(paths, text="Repackage",
                                        command=self._repackage)
        self.btn_repackage.grid(row=0, column=4, rowspan=2, padx=(6, 0), sticky="ns")

        # -- Row 1: page list | page view, split by a draggable sash so long
        # page stems can be given room (or the list squeezed to give the scan
        # more); the list's width is remembered as review_pages_width.
        # A classic PanedWindow, not ttk, for opaqueresize=False: dragging
        # moves only an outline and the panes resize once, on release. Live
        # resizing re-laid out the toolbars, the ~300 symbol keys and the
        # scan on every mouse step, and the page view tore and clipped
        # until the drag stopped (seen on Windows, 2026-10-28).
        self.split = tk.PanedWindow(
            self, orient="horizontal", opaqueresize=False, sashwidth=5,
            sashrelief="flat", borderwidth=0, sashcursor="sb_h_double_arrow")
        self.split.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(self.split)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Checkbutton(left, text="Worst first", variable=self.var_worst_first,
                        command=self._on_worst_first_toggled).grid(row=0, column=0, sticky="w")
        self.lst_pages = tk.Listbox(left, width=30, exportselection=False)
        self.lst_pages.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst_pages.yview)
        sb.grid(row=1, column=1, sticky="ns")
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self.lst_pages.xview)
        hsb.grid(row=2, column=0, sticky="ew")
        self.lst_pages.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.lst_pages.bind("<<ListboxSelect>>", self._on_page_selected)

        # Detachable needs a grid cell to put the page (or its placeholder)
        # back in, so the right pane is a frame holding one.
        body = ttk.Frame(self.split, padding=(4, 0, 0, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.split.add(left, stretch="never", minsize=_MIN_LIST_PX)
        self.split.add(body, stretch="always", minsize=_MIN_PAGE_PX)
        self.split.bind("<ButtonRelease-1>", self._on_split_moved, add="+")
        self._restore_split()

        # The page view is a classic frame: only those can become a window
        # of their own (widgets.Detachable).
        self.workspace = tk.Frame(body)
        self.workspace.grid(row=0, column=0, sticky="nsew")
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
        Tooltip(b, "Previous flagged word (p; Shift+Tab from the editor)")
        b = ttk.Button(bar, text="Next ▶", command=lambda: self._goto_flagged(+1))
        b.pack(side="left", padx=(4, 10))
        Tooltip(b, "Next flagged word (n; Tab from the editor)")
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

        # Every shortcut goes through one dispatcher, bound at the
        # application level, so it works wherever focus is (see _on_key).
        # Tab is the exception: Tk's own focus-traversal binding for it is
        # more specific than <KeyPress>, so the two widgets where Tab means
        # "next flagged word" bind it themselves.
        self.bind_all("<KeyPress>", self._on_key, add="+")
        for widget in (self.canvas, self.ent_edit):
            widget.bind("<Tab>", lambda _e: self._goto_flagged(+1) or "break")
            widget.bind("<Shift-Tab>", lambda _e: self._goto_flagged(-1) or "break")
            if widget.tk.call("tk", "windowingsystem") == "x11":
                widget.bind("<ISO_Left_Tab>", lambda _e: self._goto_flagged(-1) or "break")
        # Tool buttons never take focus: a click on "Next ▶" must not put
        # the keyboard on the button, where n/p/Esc would be lost.
        for widget in self._tool_widgets(ws):
            widget.configure(takefocus=False)

    @staticmethod
    def _tool_widgets(root: tk.Misc) -> list[tk.Widget]:
        found: list[tk.Widget] = []
        stack = [root]
        while stack:
            w = stack.pop()
            for child in w.winfo_children():
                if isinstance(child, (ttk.Button, ttk.Checkbutton, tk.Button)):
                    found.append(child)
                stack.append(child)
        return found

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
        c.bind("<Double-Button-1>", lambda e: self._on_canvas_multi_click(e, "line"))
        c.bind("<Triple-Button-1>", lambda e: self._on_canvas_multi_click(e, "block"))
        # The context menu: right button (button 2 on macOS).
        c.bind("<Button-3>", self._on_canvas_menu)
        if c.tk.call("tk", "windowingsystem") == "aqua":
            c.bind("<Button-2>", self._on_canvas_menu)

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
            ("Clear", "Clear the selection (Esc)", self._clear_selection),
        ):
            b = ttk.Button(btns, text=label, command=command)
            b.pack(side="left", padx=(0, 4))
            Tooltip(b, tip)
            self._order_buttons.append(b)
        # Fixing a word's text without leaving the mode: a single selected
        # word sits in the editor bar below, ready for typing.
        self.btn_order_edit = ttk.Button(btns, text="✎ Edit word",
                                         command=self._edit_selected_word)
        self.btn_order_edit.pack(side="left", padx=(0, 4))
        Tooltip(self.btn_order_edit,
                "Correct the selected word's text in the editor below (Enter or F2)")
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
        t.bind("<B1-Motion>", lambda _e: "break")
        t.bind("<Double-Button-1>", lambda e: self._on_order_text_multi(e, "line"))
        t.bind("<Triple-Button-1>", lambda e: self._on_order_text_multi(e, "block"))
        panel.grid_remove()

    def _build_editor(self, ws: tk.Frame) -> None:
        editor = self.editor_frame = ttk.LabelFrame(ws, text="Selected word", padding=6)
        editor.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        editor.columnconfigure(2, weight=1)
        self.lbl_snippet = ttk.Label(editor)
        self.lbl_snippet.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.lbl_wc = ttk.Label(editor, text="—", width=24)
        self.lbl_wc.grid(row=0, column=1, sticky="w")
        self.ent_edit = ttk.Entry(editor, textvariable=self.var_edit, font=("Consolas", 12))
        self.ent_edit.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8))
        self.ent_edit.bind("<Return>", lambda _e: self._apply_edit(advance=True))
        self.ent_edit.bind("<KP_Enter>", lambda _e: self._apply_edit(advance=True))
        self.ent_edit.bind("<Escape>", lambda _e: self._on_escape())
        btns = ttk.Frame(editor)
        btns.grid(row=1, column=3, sticky="e")
        ttk.Button(btns, text="Apply / Confirm (Enter)",
                   command=lambda: self._apply_edit(advance=True)).pack(side="left")
        self.btn_delete = ttk.Button(btns, text="Delete word",
                                     command=self._delete_selection)
        self.btn_delete.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_delete, "Remove the selected word(s) the OCR made up "
                                 "(Delete / Ctrl+D; Ctrl+Z puts them back)")
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

    def _restore_split(self) -> None:
        """Put the sash where the corrector last left it. A sash can only be
        placed once the pane is mapped, so this waits for the first <Map>."""
        try:
            width = int(self._settings.get("review_pages_width", 0))
        except (TypeError, ValueError):
            width = 0
        if width <= 0:
            return

        def place(_event=None) -> None:
            self.split.unbind("<Map>", bind_id)
            # Never let a saved width swallow the page view on a smaller screen.
            room = self.split.winfo_width() - _MIN_PAGE_PX
            if room > 0:
                self.split.sash_place(0, max(_MIN_LIST_PX, min(width, room)), 1)

        bind_id = self.split.bind("<Map>", place, add="+")

    def list_width(self) -> int:
        """The page list's width: where the sash sits."""
        return int(self.split.sash_coord(0)[0])

    def _on_split_moved(self, _event=None) -> None:
        # This widget binding runs before the PanedWindow class binding that
        # actually drops the sash on release, so read it once that has run.
        self.after_idle(self._remember_split)

    def _remember_split(self) -> None:
        try:
            width = self.list_width()
        except tk.TclError:
            return
        if width > 0 and self._settings.get("review_pages_width") != width:
            self._settings["review_pages_width"] = width
            self._persist()

    def _on_symbol_category(self, name: str) -> None:
        if self._settings.get("review_symbols_tab") != name:
            self._settings["review_symbols_tab"] = name
            self._persist()

    def _insert_symbol(self, ch: str) -> None:
        """Type a keyboard symbol into the word editor the way a key press
        would: replacing any selected text, leaving the caret after it."""
        ent = self.ent_edit
        if self._selected is None and self._pending_box is None:
            self.var_status.set("Select one word to type into first.")
            self.bell()
            return
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

        # Nothing stays open while the pages load: the old object's list and
        # page would otherwise invite clicks into an object being replaced.
        self._clear_view()
        self._stem = None
        self._pages = []
        self._img_index = {}
        self._refresh_page_list()
        self.btn_load.configure(state="disabled")
        self.var_status.set("Loading pages…")
        q: queue.Queue = queue.Queue()
        self._load_queue = q

        def work() -> None:
            try:
                q.put(("done", obj, src, scan_object_pages(
                    alto_dir, src, lambda n, total: q.put(("progress", n, total)))))
            except Exception as e:  # noqa: BLE001 — report it, never kill the thread silently
                logger.exception("loading %s failed", obj)
                q.put(("error", obj, e))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_load, q)

    def _poll_load(self, q: queue.Queue) -> None:
        if q is not self._load_queue:
            return  # superseded
        msg = None
        try:
            while True:  # drain: only the newest progress is worth showing
                msg = q.get_nowait()
                if msg[0] != "progress":
                    break
        except queue.Empty:
            pass
        if msg is None or msg[0] == "progress":
            if msg is not None:
                self.var_status.set(f"Loading pages… {msg[1]}/{msg[2]}")
            self.after(100, self._poll_load, q)
            return
        self._load_queue = None
        self.btn_load.configure(state="normal")
        if msg[0] == "error":
            self.var_status.set("Loading failed.")
            messagebox.showerror("Review", f"Could not load {msg[1]}:\n{msg[2]}",
                                 parent=self.winfo_toplevel())
            return
        _, obj, src, (self._img_index, self._pages) = msg
        missing = sum(1 for stem, _pc, _st in self._pages if stem not in self._img_index)

        self._settings["review_object_dir"] = str(obj)
        self._settings["review_source_dir"] = str(src)
        self._persist()

        self._refresh_page_list()
        self._update_window_title()
        msg_text = f"{len(self._pages)} page(s) loaded."
        if missing:
            msg_text += f"  {missing} page(s) have no matching source image and can't be reviewed."
        self.var_status.set(msg_text)

    def loading(self) -> bool:
        """True while "Load object" is still reading the object."""
        return self._load_queue is not None

    def _page_sort_key(self, entry: PageRow) -> tuple:
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
        self._sel = []
        self._anchor = None
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
            # upright(): the frame OCR read the page in (see alto_parse).
            pil = upright(Image.open(img_path)).convert("RGB")
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
        chosen = {id(w) for w in self._sel}
        for word in self._page.words:
            flagged = word.needs_review(threshold)
            picked = id(word) in chosen
            if not (flagged or self.var_show_all.get() or id(word) in edited
                    or picked):
                continue
            x, y, bw, bh = word.box_px
            color = (_COLOR_SELECTED if picked
                     else _COLOR_EDITED if id(word) in edited
                     else _wc_color(word.wc))
            item = self.canvas.create_rectangle(
                x * scale, y * scale, (x + bw) * scale, (y + bh) * scale,
                outline=color, width=3 if picked else 2,
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

    # ---------- keyboard ---------------------------------------------------

    # Widget classes whose plain keys are typing. A disabled or read-only
    # one (the zoom/threshold dropdowns, the transcript panel, the editor
    # while several words are selected) types nothing, so it gets the
    # shortcuts like any other widget.
    _TYPING_CLASSES = frozenset({"Entry", "TEntry", "TCombobox", "Spinbox",
                                 "TSpinbox", "Text"})

    def _widget(self, widget: object) -> Optional[tk.Misc]:
        if isinstance(widget, tk.Misc):
            return widget
        try:
            return self.nametowidget(str(widget))
        except (KeyError, tk.TclError):
            return None  # e.g. a combobox's popdown, which Tkinter never wrapped

    def _owns_event(self, widget: object) -> bool:
        """Is a key event for this tab? Its widgets (the page view keeps its
        path when it pops out), or — just after a click on the tab itself —
        the main window or the notebook while this tab is on show."""
        path, mine = str(widget), str(self)
        if path == mine or path.startswith(mine + "."):
            return True
        w = self._widget(widget)
        if w is None or not self.winfo_ismapped():
            return False
        return w is self.winfo_toplevel() or isinstance(w, ttk.Notebook)

    def _is_typing(self, widget: object) -> bool:
        w = self._widget(widget)
        if w is None:
            return False
        try:
            if w.winfo_class() not in self._TYPING_CLASSES:
                return False
            return str(w.cget("state")) not in ("disabled", "readonly")
        except tk.TclError:
            return False

    def _focus(self) -> Optional[tk.Misc]:
        try:
            return self.focus_get()
        except (KeyError, tk.TclError):
            return None

    def _on_key(self, event: tk.Event) -> Optional[str]:
        """Every Review shortcut, wherever focus sits in the tab.

        Bound with ``bind_all``, so it runs after the focused widget's own
        bindings: the editor's Enter/Escape and the page list's arrows keep
        working as before. Ctrl shortcuts work everywhere except the folder
        fields; plain keys (n, p, a, r, Esc, Delete, Enter, F2) only where
        they would not be typing."""
        if not self._owns_event(event.widget):
            return None
        key = event.keysym
        ctrl = bool(event.state & 0x4)
        alt = bool(event.state & (0x20000 if self._windowing == "win32" else 0x8))
        typing = self._is_typing(event.widget)
        editor = str(event.widget) == str(self.ent_edit)
        other_field = typing and not editor  # the folder entries
        if ctrl and not alt:
            k = key.lower()
            if k == "s":
                self._save_page()
            elif key == "Prior":
                self._goto_page(-1)
            elif key == "Next":
                self._goto_page(+1)
            elif k == "z" and not other_field:
                self._undo_edit()
            elif k == "d" and not other_field:
                self._delete_selection()
            elif key in ("Up", "Down") and not typing:
                self._order_nudge(-1 if key == "Up" else +1)
            else:
                return None
            return "break"
        if typing or alt:
            return None
        k = key.lower() if len(key) == 1 else key
        if k == "n":
            self._goto_flagged(+1)
        elif k == "p":
            self._goto_flagged(-1)
        elif k == "a":
            self._toggle_add_mode()
        elif k == "r":
            self._toggle_reorder_mode()
        elif key == "Escape":
            self._on_canvas_escape()
        elif key in ("Delete", "BackSpace"):
            self._delete_selection()
        elif key in ("Return", "KP_Enter", "F2"):
            self._edit_selected_word()
        else:
            return None
        return "break"

    # ---------- the scan: clicks, drags, the context menu -----------------

    def _on_canvas_click(self, event: tk.Event) -> None:
        self._canvas_press(event, "plain")

    def _on_canvas_modified_click(self, event: tk.Event, mode: str) -> None:
        self._canvas_press(event, mode)

    def _canvas_press(self, event: tk.Event, mode: str) -> None:
        """A button press on the scan. Add mode starts a rubber band; any
        other mode records the press, and release decides what it was: a
        click, a box drawn around words, or (reading order, from a selected
        word) the selection dragged to its new place."""
        self.canvas.focus_set()
        self._last_click_moved = False
        if self._page is None:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self.var_add_mode.get():
            self._cancel_pending_add()
            self._drag_start = (cx, cy)
            return
        word = self._page.word_at(cx / self._scale, cy / self._scale)
        self._press_at = (cx, cy, mode)
        self._press_word = word
        self._drag_rect = None
        self._dropping = (self.var_reorder_mode.get() and mode == "plain"
                          and word is not None and self._is_selected(word))

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._drag_start is not None:  # add mode: the word's box
            x0, y0 = self._drag_start
            x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
            if self._drag_item is None:
                self._drag_item = self.canvas.create_rectangle(
                    x0, y0, x1, y1, outline=_COLOR_EDITED, width=2, dash=(4, 2),
                    tags=(_OVERLAY,))
            else:
                self.canvas.coords(self._drag_item, x0, y0, x1, y1)
            return
        if self._press_at is None:
            return
        self._autoscroll(event)
        x0, y0, _mode = self._press_at
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self._drag_rect is None and abs(x1 - x0) < 5 and abs(y1 - y0) < 5:
            return  # still a click: hands wobble
        if self._dropping:
            self._draw_drop_marker(x1, y1)
            return
        if self._drag_rect is None:
            self._drag_rect = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=_COLOR_SELECTED, width=2, dash=(4, 2),
                tags=(_OVERLAY,))
        else:
            self.canvas.coords(self._drag_rect, x0, y0, x1, y1)

    def _autoscroll(self, event: tk.Event) -> None:
        """Dragging near an edge of the view scrolls it, so a box or a drop
        can reach words that are off screen."""
        margin = 24
        if event.y < margin:
            self.canvas.yview_scroll(-1, "units")
        elif event.y > self.canvas.winfo_height() - margin:
            self.canvas.yview_scroll(1, "units")
        if event.x < margin:
            self.canvas.xview_scroll(-1, "units")
        elif event.x > self.canvas.winfo_width() - margin:
            self.canvas.xview_scroll(1, "units")

    def _drop_target(self, cx: float, cy: float) -> tuple[Optional[Word], bool]:
        """Where a dragged selection would land: the word under the canvas
        point and whether it goes *before* it (left half) or after it."""
        word = self._page.word_at(cx / self._scale, cy / self._scale)
        if word is None or self._is_selected(word):
            return None, False
        x, _y, bw, _bh = word.box_px
        return word, cx / self._scale < x + bw / 2

    def _draw_drop_marker(self, cx: float, cy: float) -> None:
        """While dragging the selection: an insertion bar on the side of the
        word it would land beside, and a hint in the status line."""
        if self._drag_rect is not None:
            self.canvas.delete(self._drag_rect)
            self._drag_rect = None
        self.canvas.configure(cursor="fleur")
        target, before = self._drop_target(cx, cy)
        if target is None:
            # A placeholder item keeps "a drag is in progress" true.
            self._drag_rect = self.canvas.create_line(
                cx, cy, cx, cy, fill="", tags=(_OVERLAY,))
            self.var_status.set(f"Drop the {len(self._sel)} word(s) on the word "
                                "they belong beside.")
            return
        s = self._scale
        x, y, bw, bh = target.box_px
        bar_x = (x if before else x + bw) * s
        self._drag_rect = self.canvas.create_line(
            bar_x, (y - bh * 0.15) * s, bar_x, (y + bh * 1.15) * s,
            fill=_COLOR_SELECTED, width=4, capstyle="round", tags=(_OVERLAY,))
        self.var_status.set(f"Release to move the {len(self._sel)} word(s) "
                            f"{'before' if before else 'after'} “{target.content}”.")

    def _on_canvas_release(self, event: tk.Event) -> None:
        if self._drag_start is not None:
            self._finish_add_drag(event)
            return
        if self._press_at is None:
            return
        x0, y0, mode = self._press_at
        word, dropping = self._press_word, self._dropping
        self._press_at, self._press_word, self._dropping = None, None, False
        dragged = self._drag_rect is not None
        if dragged:
            self.canvas.delete(self._drag_rect)
            self._drag_rect = None
            self.canvas.configure(cursor="")
        if self._page is None:
            return
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if dragged and dropping:
            target, before = self._drop_target(x1, y1)
            if target is None:
                self._update_status("Nothing moved — drop the words on a word "
                                    "outside the selection.")
            else:
                self._order_place(target, before)
            return
        if dragged:
            s = self._scale
            self._box_select((min(x0, x1) / s, min(y0, y1) / s,
                              abs(x1 - x0) / s, abs(y1 - y0) / s),
                             add=mode != "plain")
            return
        self._click_word(word, mode)

    def _click_word(self, word: Optional[Word], mode: str) -> None:
        """A click (no drag) on the scan or in the transcript panel."""
        if self.var_reorder_mode.get():
            if mode == "ctrl":
                self._toggle_word(word)
            else:
                self._order_pick(word, shift=mode == "shift")
            return
        if mode == "ctrl":
            self._toggle_word(word)
        elif mode == "shift" and word is not None and self._anchor is not None:
            self._extend_to(word)
        else:
            # Clicking a word is reaching for it: put the caret in the editor.
            self._select_word(word, focus_editor=word is not None)

    def _on_canvas_multi_click(self, event: tk.Event, unit: str) -> str:
        """Double-click: the word's whole line; triple-click: its block —
        a garbage line or a margin note picked up in one gesture. Skipped
        when the click before it moved words (reading order, with a
        selection): the gesture was a placement, not a selection."""
        if (self._page is None or self.var_add_mode.get()
                or self._last_click_moved):
            return "break"
        x = self.canvas.canvasx(event.x) / self._scale
        y = self.canvas.canvasy(event.y) / self._scale
        self._select_unit(self._page.word_at(x, y), unit)
        return "break"

    def _select_unit(self, word: Optional[Word], unit: str) -> None:
        if word is None or self._page is None:
            return
        words = (self._page.line_words(word) if unit == "line"
                 else self._page.block_words(word))
        if not self.var_reorder_mode.get():
            words = [w for w in words if self._shown(w)] or [word]
        self._set_selection(words, words[0])
        if not self.var_reorder_mode.get():
            self._update_status(f"Selected the {unit}: {len(words)} word(s).")

    def _menu_entries(self, word: Optional[Word]
                      ) -> list[Optional[tuple[str, Callable[[], None]]]]:
        """The right-click menu for a click on ``word`` (None: empty scan),
        as (label, action) pairs and None for separators. Pure state → menu,
        so it is testable without posting a real menu."""
        entries: list[Optional[tuple[str, Callable[[], None]]]] = []
        n = len(self._sel)
        order = self.var_reorder_mode.get()
        if order and n and word is not None and not self._is_selected(word):
            entries += [
                (f"Move {n} word(s) after “{word.content}”",
                 lambda: self._order_place(word, before=False)),
                (f"Move {n} word(s) before “{word.content}”",
                 lambda: self._order_place(word, before=True)),
                ("Move them to the page start", self._order_to_start),
                None,
            ]
        if self._selected is not None:
            entries.append((f"Edit “{self._selected.content}”\tEnter",
                            self._edit_selected_word))
        if n:
            entries.append((f"Delete {n} word(s)\tDel" if n > 1 else "Delete word\tDel",
                            self._delete_selection))
        if word is not None:
            if entries:
                entries.append(None)
            entries += [
                ("Select this line\tdouble-click", lambda: self._select_unit(word, "line")),
                ("Select this block\ttriple-click", lambda: self._select_unit(word, "block")),
            ]
        if n:
            entries.append(("Clear selection\tEsc", self._clear_selection))
        return entries

    def _on_canvas_menu(self, event: tk.Event) -> None:
        if self._page is None or self.var_add_mode.get():
            return
        self.canvas.focus_set()
        x = self.canvas.canvasx(event.x) / self._scale
        y = self.canvas.canvasy(event.y) / self._scale
        word = self._page.word_at(x, y)
        # Right-click outside the selection picks the word first — unless
        # it is the target of a reading-order move.
        if word is not None and not self._is_selected(word) and not (
                self.var_reorder_mode.get() and self._sel):
            if self.var_reorder_mode.get():
                self._set_selection([word], word)
            else:
                self._select_word(word)
        entries = self._menu_entries(word)
        if not entries:
            return
        if self._menu is not None:
            self._menu.destroy()
        menu = self._menu = tk.Menu(self.canvas, tearoff=False)
        for entry in entries:
            if entry is None:
                menu.add_separator()
            else:
                label, action = entry
                text, _, accel = label.partition("\t")
                menu.add_command(label=text, accelerator=accel, command=action)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------- the selection ---------------------------------------------

    def _is_selected(self, word: Word) -> bool:
        return any(w is word for w in self._sel)

    def _shown(self, word: Word) -> bool:
        """Is the word drawn on the scan? Outside reading-order mode a box or
        a line selection only takes words the corrector can see."""
        if self.var_reorder_mode.get() or self.var_show_all.get():
            return True
        return (word.needs_review(self._threshold())
                or id(word) in self._touched_ids() or self._is_selected(word))

    def _set_selection(self, words: list[Word], anchor: Optional[Word], *,
                       focus_editor: bool = False, scroll: bool = False,
                       refill: bool = False) -> None:
        """Make ``words`` the selection (kept in reading order, the order a
        move carries them in). Exactly one word also sits in the editor bar.

        Focus is left where it is, with two exceptions: ``focus_editor``
        puts the caret in the editor (a click on a word), and when the
        editor holds focus but no longer holds a word, focus goes back to
        the scan — an empty editor would swallow every shortcut key."""
        pos = {id(w): i for i, w in enumerate(self._page.words)} if self._page else {}
        unique = {id(w): w for w in words if id(w) in pos}
        self._sel = sorted(unique.values(), key=lambda w: pos[id(w)])
        self._anchor = anchor if anchor is not None and id(anchor) in pos else (
            self._sel[0] if self._sel else None)
        single = self._sel[0] if len(self._sel) == 1 else None
        if self._sel and self._pending_box is not None:
            # Selecting real words supersedes an un-committed add box.
            self._pending_box = None
        changed = single is not self._selected
        self._selected = single
        if changed or refill or single is None:
            self._update_editor()
        if self.var_reorder_mode.get():
            self._refresh_order_panel()
        self._render()
        in_editor = self._focus() is self.ent_edit
        if single is not None and focus_editor:
            self._edit_selected_word()
        elif single is not None:
            # Pre-selected, so typing (once Enter/F2 or a click puts the
            # caret there) or a symbol key replaces the word outright.
            self.ent_edit.selection_range(0, "end")
            self.ent_edit.icursor("end")
        elif single is None and in_editor:
            self.canvas.focus_set()
        if scroll and self._sel:
            self._scroll_to_word(self._sel[0])
        if len(self._sel) > 1 and not self.var_reorder_mode.get():
            self._update_status(f"{len(self._sel)} words selected — Delete removes "
                                "them, Ctrl+Z puts them back, Esc clears.")

    def _select_word(self, word: Optional[Word], *, focus_editor: bool = False) -> None:
        """Select one word (None: nothing), bring it into view, and put it in
        the editor — the stepping and clicking selection."""
        self._set_selection([word] if word is not None else [], word,
                            focus_editor=focus_editor, scroll=word is not None,
                            refill=True)

    def _clear_selection(self) -> None:
        self._set_selection([], None)

    def _toggle_word(self, word: Optional[Word]) -> None:
        """Ctrl+click: add a word to the selection, or take it out."""
        if word is None or self._page is None:
            return
        if self._is_selected(word):
            rest = [w for w in self._sel if w is not word]
            anchor = self._anchor if self._anchor is not word else None
            self._set_selection(rest, anchor or (rest[0] if rest else None))
        else:
            self._set_selection(self._sel + [word], word)

    def _extend_to(self, word: Word) -> bool:
        """Shift+click: select every word from the anchor to ``word`` in
        reading order (outside reading-order mode, every one drawn)."""
        i = self._page.doc_index(self._anchor) if self._anchor is not None else None
        j = self._page.doc_index(word)
        if i is None or j is None:
            return False
        lo, hi = sorted((i, j))
        run = [w for w in self._page.words[lo:hi + 1] if self._shown(w)]
        self._set_selection(run, self._anchor)
        return True

    def _box_select(self, box: tuple[float, float, float, float], add: bool) -> None:
        """Select the words whose centers fall inside an image-px box — a
        column or a caption in one drag, where Tesseract's lines interleave
        it with the text beside it. ``add`` keeps the current selection."""
        x, y, bw, bh = box
        hits = [w for w in self._page.words
                if x <= w.box_px[0] + w.box_px[2] / 2 <= x + bw
                and y <= w.box_px[1] + w.box_px[3] / 2 <= y + bh
                and self._shown(w)]
        if add:
            hits = self._sel + [w for w in hits if not self._is_selected(w)]
        if not hits:
            self.var_status.set("No words inside that box.")
            return
        self._set_selection(hits, self._anchor if add and self._anchor else hits[0])

    def _prune_selection(self) -> None:
        """Drop selected words that are no longer on the page (deleted,
        cleared, an undone add)."""
        on_page = {id(w) for w in self._page.words} if self._page else set()
        if all(id(w) in on_page for w in self._sel) and (
                self._selected is None or id(self._selected) in on_page):
            return
        self._sel = [w for w in self._sel if id(w) in on_page]
        if self._anchor is not None and id(self._anchor) not in on_page:
            self._anchor = self._sel[0] if self._sel else None
        single = self._sel[0] if len(self._sel) == 1 else None
        if single is not self._selected:
            self._selected = single
            self._update_editor()

    def _edit_selected_word(self) -> None:
        """Enter/F2/✎ Edit word: put the caret in the editor over the
        selected word, ready to type. With nothing selected (outside reading
        order), Enter starts on the next flagged word."""
        if self._pending_box is not None:
            self.ent_edit.focus_set()
            return
        if self._selected is None:
            if not self._sel and not self.var_reorder_mode.get() and self._page:
                self._goto_flagged(+1)
            if self._selected is None:
                return
        self.ent_edit.focus_set()
        self.ent_edit.selection_range(0, "end")
        self.ent_edit.icursor("end")

    def _on_canvas_escape(self) -> None:
        """Esc where nothing is being typed: drop a drawn-but-untyped add
        box, else the selection, else leave add mode."""
        if self._pending_box is not None:
            self._cancel_pending_add()
        elif self._sel:
            self._clear_selection()
        elif self.var_add_mode.get():
            self._toggle_add_mode()
        self.canvas.focus_set()

    def _on_escape(self) -> None:
        """Esc in the editor: abandon what was typed and give the keys back
        to the shortcuts. A word whose text was untouched is also let go
        (outside reading order, where the selection is still wanted)."""
        if self._pending_box is not None:
            self._cancel_pending_add()
        elif (self._selected is not None and not self.var_reorder_mode.get()
              and " ".join(self.var_edit.get().split()) == self._selected.content):
            self._clear_selection()
        else:
            self._update_editor()
        self.canvas.focus_set()

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

    def _cancel_pending_add(self) -> None:
        self._drag_start = None
        if self._drag_item is not None:
            self.canvas.delete(self._drag_item)
            self._drag_item = None
        if self._pending_box is not None:
            self._pending_box = None
            self._update_editor()

    def _finish_add_drag(self, event: tk.Event) -> None:
        x0, y0 = self._drag_start
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self._drag_start = None
        box = (min(x0, x1) / self._scale, min(y0, y1) / self._scale,
               abs(x1 - x0) / self._scale, abs(y1 - y0) / self._scale)
        if box[2] < 3 or box[3] < 3:  # a stray click, not a drawn box
            self._cancel_pending_add()
            return
        self._sel, self._anchor, self._selected = [], None, None
        self._pending_box = box
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
        self._select_word(word)
        self.canvas.focus_set()  # ready for the next box, or n/p
        self._update_status()

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
        self._cancel_pending_add()
        self.order_panel.grid()
        self._set_selection([], None)
        self.canvas.focus_set()

    def _teardown_reorder(self) -> None:
        self.order_panel.grid_remove()
        self._set_selection([], None)

    def _order_hint(self) -> str:
        if not self._sel:
            return ("Select the words that are out of place: click one (on the "
                    "scan or in the transcript below), Shift+click to extend to "
                    "a run, Ctrl+click to add or remove a word, double-click for "
                    "a whole line, or drag a box around a column or caption on "
                    "the scan. A single selected word can also be corrected in "
                    "the editor below.")
        n = len(self._sel)
        hint = (f"{n} word(s) selected. Now click the word they should come "
                "after — or drag them onto it (drop on its left half to put "
                "them before it). Right-click a word for more; “Page start” "
                "puts them first; Ctrl+Up/Ctrl+Down nudge one word; Delete "
                "removes them; Esc clears; Ctrl+Z undoes.")
        if n == 1:
            hint += " To fix its text, press Enter or F2 (or ✎ Edit word)."
        return hint

    def _refresh_order_panel(self) -> None:
        """Re-render the transcript (exactly what a save will write), mark
        the selection in it, and refresh the hint and buttons."""
        self._prune_selection()
        page = self._page
        words = {id(w.elem): w for w in page.words} if page is not None else {}
        self.var_order_hint.set(self._order_hint())
        state = "normal" if self._sel else "disabled"
        for b in self._order_buttons:
            b.configure(state=state)
        self.btn_order_edit.configure(
            state="normal" if len(self._sel) == 1 else "disabled")

        txt = self.txt_order
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        self._order_spans = []
        if page is not None:
            text, spans = transcript_spans(page.root)
            txt.insert("1.0", text or "(no words on this page)")
            self._order_spans = [(a, b, words[id(el)]) for el, a, b in spans
                                 if id(el) in words]
            chosen = {id(w) for w in self._sel}
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
        self._last_click_moved = False
        self._order_pick(self._order_word_in_text(event), shift)
        return "break"  # no text selection / insert cursor in the panel

    def _on_order_text_toggle(self, event: tk.Event) -> str:
        self._toggle_word(self._order_word_in_text(event))
        return "break"

    def _on_order_text_multi(self, event: tk.Event, unit: str) -> str:
        if not self._last_click_moved:
            self._select_unit(self._order_word_in_text(event), unit)
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
            if not shift and self._sel:
                self._clear_selection()
            return
        if shift and self._anchor is not None and self._extend_to(word):
            return
        if not self._sel or shift or self._is_selected(word):
            self._set_selection([word], word)
            return
        self._order_place(word, before=False)

    def _order_place(self, target: Word, before: bool) -> None:
        """Move the selection to read right after ``target`` — or right
        before it, which is right after the word read before it (the page
        start when there is none)."""
        if before:
            others = [w for w in self._page.words if not self._is_selected(w)]
            i = next((k for k, w in enumerate(others) if w is target), None)
            if i is None:
                return
            after = others[i - 1] if i > 0 else None
            where = f"before “{target.content}”"
        else:
            after, where = target, f"after “{target.content}”"
        self._order_apply(lambda: self._page.move_words(self._sel, after), where)

    def _order_nudge(self, delta: int) -> None:
        if self.var_reorder_mode.get() and self._sel:
            self._order_apply(lambda: self._page.nudge_words(self._sel, delta),
                              "one word earlier" if delta < 0 else "one word later")

    def _order_to_start(self) -> None:
        if self._sel:
            self._order_apply(lambda: self._page.move_words(self._sel, None),
                              "to the start of the page")

    def _order_apply(self, do_move: Callable[[], Optional[WordMove]], where: str) -> None:
        """Run one move of the selection; keep the selection so it can be
        nudged again."""
        if self._page is None or not self._sel:
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
        self._last_click_moved = True
        self._undo.append(_MoveEntry(move))
        self.btn_save.configure(state="normal")
        self._refresh_order_panel()
        self._render()
        self._update_status(f"Moved {len(self._sel)} word(s) {where}. "
                            "Ctrl+Z undoes.")

    def _draw_order_overlays(self) -> None:
        """Every word outlined, the selection heavy, and the reading path:
        a link between each pair of words along a line, and an arrow from
        the end of one line to the start of the next — so a wrong order is
        visible at a glance. Redrawn from state inside _render.

        Boxes go down first and the path over them, each link on a white
        casing so it stays legible where it does cross type (order_link
        keeps it off the words wherever the order is right)."""
        s = self._scale
        chosen = {id(w) for w in self._sel}
        for word in self._page.words:
            if id(word) in chosen:
                continue  # drawn last, over the path
            x, y, bw, bh = (v * s for v in word.box_px)
            self.canvas.create_rectangle(
                x, y, x + bw, y + bh, outline=_COLOR_ORDER_BOX, width=1,
                tags=(_OVERLAY,))
        heights = sorted(w.box_px[3] * s for w in self._page.words)
        k = (max(_ORDER_MIN_WEIGHT,
                 min(1.0, heights[len(heights) // 2] / _ORDER_FULL_WEIGHT_PX))
             if heights else 1.0)
        links = []
        prev = None
        for word in self._page.words:
            box = tuple(v * s for v in word.box_px)
            if prev is not None:
                same_line = prev[0].elem.getparent() is word.elem.getparent()
                links.append((order_link(prev[1], box, same_line), same_line))
            prev = (word, box)
        # All casings, then all paths: a casing drawn later would cut
        # through the link before it where the two meet.
        for color, width, arrow in (
            (_COLOR_ORDER_CASING, _ORDER_CASING_WIDTH, _ORDER_CASING_ARROW),
            (_COLOR_ORDER_PATH, _ORDER_PATH_WIDTH, _ORDER_ARROW),
        ):
            width = max(1.0, width * k)
            arrow = tuple(max(2.0, v * k) for v in arrow)
            for coords, same_line in links:
                self.canvas.create_line(
                    *coords, fill=color, width=width, capstyle="round",
                    arrow="none" if same_line else "last", arrowshape=arrow,
                    tags=(_OVERLAY,))
        for word in self._sel:
            x, y, bw, bh = (v * s for v in word.box_px)
            self.canvas.create_rectangle(
                x, y, x + bw, y + bh, outline=_COLOR_EDITED, width=3,
                tags=(_OVERLAY,))

    # ---------- the editor bar --------------------------------------------

    def _scroll_to_word(self, word: Word) -> None:
        """Bring a word into view — centred, but only when it is not already
        on screen, so clicking a visible word never jerks the page."""
        x, y, bw, bh = (v * self._scale for v in word.box_px)
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        left, top = self.canvas.canvasx(0), self.canvas.canvasy(0)
        if left <= x and x + bw <= left + cw and top <= y and y + bh <= top + ch:
            return
        w = max(int(self._pil.width * self._scale), 1)
        h = max(int(self._pil.height * self._scale), 1)
        self.canvas.xview_moveto(max(0.0, min(1.0, (x + bw / 2 - cw / 2) / w)))
        self.canvas.yview_moveto(max(0.0, min(1.0, (y + bh / 2 - ch / 2) / h)))

    _SNIPPET_H = 48
    _SNIPPET_MAX_W = 520

    def _set_snippet(self, x: float, y: float, bw: float, bh: float) -> None:
        """Show a magnified crop of the scan region in the editor bar — a
        word, or the span of a multi-word selection (narrowed to fit)."""
        pad = 8
        crop = self._pil.crop((
            max(0, int(x - pad)), max(0, int(y - pad)),
            min(self._pil.width, int(x + bw + pad)),
            min(self._pil.height, int(y + bh + pad)),
        ))
        scale = min(self._SNIPPET_H / max(crop.height, 1),
                    self._SNIPPET_MAX_W / max(crop.width, 1))
        crop = crop.resize((max(int(crop.width * scale), 1),
                            max(int(crop.height * scale), 1)), Image.LANCZOS)
        self._snippet_photo = ImageTk.PhotoImage(crop)
        self.lbl_snippet.configure(image=self._snippet_photo)

    def _update_editor(self) -> None:
        self.ent_edit.configure(state="normal")
        self.btn_delete.configure(text="Delete word")
        self.editor_frame.configure(text="Selected word")
        if self._pending_box is not None and self._pil is not None:
            self._set_snippet(*self._pending_box)
            self.lbl_wc.configure(text="New word")
            self.var_edit.set("")
            return
        if len(self._sel) > 1 and self._pil is not None:
            boxes = [w.box_px for w in self._sel]
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[0] + b[2] for b in boxes)
            y1 = max(b[1] + b[3] for b in boxes)
            self._set_snippet(x0, y0, x1 - x0, y1 - y0)
            n = len(self._sel)
            self.editor_frame.configure(text=f"{n} words selected")
            self.lbl_wc.configure(text=f"{n} words — Delete removes them all")
            self.btn_delete.configure(text=f"Delete {n} words")
            self.var_edit.set("")
            self.ent_edit.configure(state="disabled")
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
            self._delete_selection()
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
        if self.var_reorder_mode.get():
            # Fixed in passing while ordering: the word stays selected (it
            # may still need moving), the transcript panel shows the new
            # text, and the keys go back to the reading-order bindings.
            self._update_editor()
            self._refresh_order_panel()
            self._render()
            self.canvas.focus_set()
            self._update_status(f"“{word.content}” set.")
            return
        self._update_status()
        if advance:
            self._goto_flagged(+1)

    def _mark_touched(self, word: Word) -> None:
        """Recolor a word the corrector just settled (edited or confirmed)
        and enable the save button."""
        item = self._rect_by_word.get(id(word))
        if item is not None and not self._is_selected(word):
            self.canvas.itemconfigure(item, outline=_COLOR_EDITED)
        self.btn_save.configure(state="normal")

    def _delete_selection(self) -> None:
        """Delete every selected word at once — no confirmation prompt; the
        whole deletion is one entry on the undo stack, so one Ctrl+Z puts
        every word back exactly. Outside reading order the next flagged
        word after them is selected, as after a single deletion."""
        words = list(self._sel)
        if not words or self._page is None:
            return
        nxt = None
        if not self.var_reorder_mode.get():
            gone = {id(w) for w in words}
            pool = [w for w in self._flagged_now() if id(w) not in gone]
            nxt = self._page.step_from(words[-1], pool, +1)
        added = {id(e.word) for e in self._undo if isinstance(e, _AddEntry)}
        items: list[tuple[Word, Removal, bool]] = []
        for word in words:
            if id(word) in added:
                # A word added this session: net it out of the provenance
                # count instead of counting an add plus a delete.
                items.append((word, self._page.undo_add(word), True))
            else:
                items.append((word, self._page.delete_word(word), False))
        self._undo.append(_DeleteEntry(items))
        self.btn_save.configure(state="normal")
        what = (f"“{words[0].content}”" if len(words) == 1
                else f"{len(words)} words")
        if self.var_reorder_mode.get():
            self._clear_selection()  # refreshes the panel + scan
        else:
            self._select_word(nxt)
        self._update_status(f"Deleted {what} (Ctrl+Z puts "
                            f"{'it' if len(words) == 1 else 'them'} back).")

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
            "noise read as text on covers, photographs, or blank pages. To "
            "remove only some words, select them (drag a box, or double-click "
            "a line) and press Delete instead.\n\n"
            "This can't be undone except by reloading the page unsaved.",
            parent=self._ws_parent(),
        ):
            return
        removed = self._page.clear_words()
        self._undo.clear()
        self._cancel_pending_add()
        self.btn_save.configure(state="normal")
        self._set_selection([], None)
        self.canvas.focus_set()
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
            self._maybe_disable_save()
            self._set_selection([w for w in self._sel if w is not entry.word],
                                self._anchor)
            self._update_status()
            return
        if isinstance(entry, _DeleteEntry):  # put the deleted words back
            for word, removal, added in reversed(entry.items):
                if added:
                    self._page.redo_add(word, removal)
                else:
                    self._page.undo_delete(word, removal)
            self._maybe_disable_save()
            words = entry.words
            if len(words) == 1:
                self._show_word(words[0])
            else:
                self._set_selection(words, words[0], scroll=True)
            self._update_status(
                f"Restored “{words[0].content}”." if len(words) == 1
                else f"Restored {len(words)} words.")
            return
        if isinstance(entry, _VerifyEntry):  # un-confirm: engine WC back
            self._page.unverify_word(entry.word, entry.old_wc)
        else:
            self._page.revert_word(entry.word, entry.old_content, entry.old_wc)
        self._maybe_disable_save()
        self._show_word(entry.word)
        self._update_status()

    def _show_word(self, word: Word) -> None:
        """Put an undone word back in front of the corrector: selected, in
        the editor with its restored text, in view."""
        self._set_selection([word], word, scroll=True, refill=True)

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

    def _goto_flagged(self, delta: int) -> None:
        """Step to the next/previous flagged word *from where the corrector
        is on the page*. Position, not membership in the flagged list: a word
        just corrected is no longer flagged (``WC="1.00"``), and looking it up
        in that list is what used to bounce the corrector back to the page's
        first flagged word after every edit. From a multi-word selection the
        step starts at its last (or, going back, first) word."""
        if self._page is None:
            return
        flagged = self._flagged_now()
        if not flagged:
            self._select_word(None)
            self.var_status.set("No flagged words left on this page. Save and pick the next page.")
            return
        current = self._selected or (
            (self._sel[-1] if delta > 0 else self._sel[0]) if self._sel else None)
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
