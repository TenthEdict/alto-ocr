"""Review tab for the Alto OCR GUI: correct transcripts over the page image.

Layout: pick an OCR *output* object folder (the one containing ``ALTO/``)
plus the matching *source image* folder (TIFs are deliberately not in the
output tree — the preservation folder is the canonical copy). The page list
shows per-page confidence (``PC``) and can sort worst-first, mirroring the
``triage`` command's queue. The canvas shows the page with word boxes
colored by ``WC``; clicking a word (or stepping with Next/Prev) opens it in
the editor bar with a magnified snippet of the actual scan region.

Keyboard flow for correctors: **Enter** commits the edit and jumps to the
next flagged word, **Escape** deselects, **n/p** step through flagged words
(canvas focus), **Ctrl+Z** undoes the last edit. Deletions (hallucinated
words) confirm first and are undone only by reloading the page.

**Add word** mode (toolbar toggle, or ``a`` with canvas focus) covers words
the OCR missed entirely: drag a box around the word on the scan, type it,
press Enter — the word is inserted into the ALTO at the drawn position
(``remediate.ReviewPage.add_word`` picks the line/block) with ``WC="1.00"``.
Ctrl+Z removes it again. A glyph palette under the editor inserts math and
engineering characters (π Σ √ ° …) correctors can't reach on a US keyboard
— common in the institution's lab-notebook material.

All ALTO mutation/save semantics live in ``remediate.py`` (headless,
unit-tested); this module is only Tk plumbing.
"""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from .remediate import (
    ConcurrentEditError,
    ReviewPage,
    StubPageError,
    Word,
    index_source_images,
)
from .triage import DEFAULT_LOW_WC, scan_alto_file
from .walker import natural_chunks, natural_key

# Archival scans are huge (hundreds of megapixels), so raise Pillow's
# decompression-bomb guard far past any real scan — but keep a ceiling so a
# corrupt file can't demand unbounded memory (warn at 1 GP, error at 2 GP).
Image.MAX_IMAGE_PIXELS = 1_000_000_000

# Cap on rendered canvas pixels: 200% zoom on a 600-DPI scan would build a
# multi-hundred-MB PhotoImage and can MemoryError — clamp the scale instead.
_MAX_RENDER_PX = 40_000_000

logger = logging.getLogger("alto_ocr.review")

_ZOOMS: tuple[str, ...] = ("Fit", "50%", "75%", "100%", "150%", "200%")

# Characters correctors can't type on a US keyboard but the collections
# need — engineering notebooks (math), lab reports (units), typescripts
# (dashes/fractions). One click inserts at the editor cursor.
_GLYPHS: tuple[str, ...] = (
    "π", "Σ", "Δ", "θ", "Ω", "μ", "√", "∫", "∞", "±", "×", "÷",
    "≈", "≠", "≤", "≥", "°", "·", "²", "³", "½", "¼", "¾", "—",
)

# WC → outline color. Blue marks words edited this session (WC becomes 1.00
# on disk, but the corrector should still see what they touched).
_COLOR_EDITED = "#1f6feb"
_COLOR_UNSCORED = "#c026d3"  # no WC (GLMOCR output): needs eyes everywhere


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


class ReviewFrame(ttk.Frame):
    """The Review tab. ``settings`` is the app's persisted dict; ``persist``
    writes it to disk (called after path/threshold changes)."""

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
        self._snippet_photo: Optional[ImageTk.PhotoImage] = None
        self._scale = 1.0
        self._rect_by_word: dict[int, int] = {}   # id(Word) -> canvas item
        self._word_by_rect: dict[int, Word] = {}
        self._selected: Optional[Word] = None
        # Undo stack: (word, old_content, old_wc) for edits; old_content is
        # None for an added word (undo removes it again).
        self._undo: list[tuple[Word, str | None, float | None]] = []
        # Add-word mode state: the drag in progress and the finalized box
        # (image-pixel space) awaiting its text.
        self._drag_start: Optional[tuple[float, float]] = None
        self._drag_item: Optional[int] = None
        self._pending_box: Optional[tuple[float, float, float, float]] = None

        self.var_object = tk.StringVar(value=str(settings.get("review_object_dir", "")))
        self.var_source = tk.StringVar(value=str(settings.get("review_source_dir", "")))
        self.var_worst_first = tk.BooleanVar(value=bool(settings.get("review_worst_first", True)))
        self.var_threshold = tk.DoubleVar(
            value=float(settings.get("review_threshold", DEFAULT_LOW_WC)))
        self.var_show_all = tk.BooleanVar(value=False)
        self.var_add_mode = tk.BooleanVar(value=False)
        self.var_zoom = tk.StringVar(value="Fit")
        self.var_status = tk.StringVar(value="Pick an output object folder and its source images.")
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
        ttk.Label(paths, text="Output object:").grid(row=0, column=0, sticky="w", padx=(0, 6))
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

        # -- Row 1: page list | canvas
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        left.rowconfigure(1, weight=1)
        ttk.Checkbutton(left, text="Worst first", variable=self.var_worst_first,
                        command=self._refresh_page_list).grid(row=0, column=0, sticky="w")
        self.lst_pages = tk.Listbox(left, width=30, exportselection=False)
        self.lst_pages.grid(row=1, column=0, sticky="ns")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst_pages.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.lst_pages.configure(yscrollcommand=sb.set)
        self.lst_pages.bind("<<ListboxSelect>>", self._on_page_selected)

        center = ttk.Frame(body)
        center.grid(row=0, column=1, sticky="nsew")
        center.columnconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)

        bar = ttk.Frame(center)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(bar, text="Zoom:").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.var_zoom, values=_ZOOMS,
                          width=6, state="readonly")
        cb.pack(side="left", padx=(2, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._render())
        ttk.Label(bar, text="Flag WC <").pack(side="left")
        sp = ttk.Spinbox(bar, from_=0.0, to=1.0, increment=0.05, width=5,
                         textvariable=self.var_threshold, command=self._on_threshold_changed)
        sp.pack(side="left", padx=(2, 10))
        ttk.Checkbutton(bar, text="All words", variable=self.var_show_all,
                        command=self._render).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="◀ Prev", command=lambda: self._goto_flagged(-1)).pack(side="left")
        ttk.Button(bar, text="Next ▶", command=lambda: self._goto_flagged(+1)).pack(
            side="left", padx=(4, 10))
        ttk.Button(bar, text="Undo", command=self._undo_edit).pack(side="left")
        ttk.Checkbutton(bar, text="Add word (a)", variable=self.var_add_mode,
                        command=self._on_add_mode_toggled).pack(side="left", padx=(10, 0))
        self.btn_save = ttk.Button(bar, text="Save page", command=self._save_page,
                                   state="disabled")
        self.btn_save.pack(side="right")

        self.canvas = tk.Canvas(center, background="#404040", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        vs = ttk.Scrollbar(center, orient="vertical", command=self.canvas.yview)
        vs.grid(row=1, column=1, sticky="ns")
        hs = ttk.Scrollbar(center, orient="horizontal", command=self.canvas.xview)
        hs.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Configure>", lambda _e: self._render_if_fit())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)
        self.canvas.bind("<Key-n>", lambda _e: self._goto_flagged(+1))
        self.canvas.bind("<Key-p>", lambda _e: self._goto_flagged(-1))
        self.canvas.bind("<Key-a>", lambda _e: self._toggle_add_mode())
        self.canvas.bind("<Control-z>", lambda _e: self._undo_edit())

        # -- Row 2: editor bar
        editor = ttk.LabelFrame(self, text="Selected word", padding=6)
        editor.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        editor.columnconfigure(2, weight=1)
        self.lbl_snippet = ttk.Label(editor)
        self.lbl_snippet.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.lbl_wc = ttk.Label(editor, text="—", width=14)
        self.lbl_wc.grid(row=0, column=1, sticky="w")
        self.ent_edit = ttk.Entry(editor, textvariable=self.var_edit, font=("Consolas", 12))
        self.ent_edit.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8))
        self.ent_edit.bind("<Return>", lambda _e: self._apply_edit(advance=True))
        self.ent_edit.bind("<Escape>", lambda _e: self._on_escape())
        self.ent_edit.bind("<Control-z>", lambda _e: self._undo_edit())
        btns = ttk.Frame(editor)
        btns.grid(row=1, column=3, sticky="e")
        ttk.Button(btns, text="Apply (Enter)",
                   command=lambda: self._apply_edit(advance=True)).pack(side="left")
        ttk.Button(btns, text="Delete word", command=self._delete_word).pack(
            side="left", padx=(6, 0))
        glyphs = ttk.Frame(editor)
        glyphs.grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))
        for ch in _GLYPHS:
            ttk.Button(glyphs, text=ch, width=2, style="Toolbutton",
                       command=lambda c=ch: self._insert_glyph(c)).pack(side="left")

        ttk.Label(self, textvariable=self.var_status, foreground="#555").grid(
            row=3, column=0, sticky="w", pady=(4, 0))

    # ---------- object / page loading ------------------------------------

    def _browse_object(self) -> None:
        d = filedialog.askdirectory(title="OCR output object folder (contains ALTO/)")
        if d:
            self.var_object.set(d)

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(title="Source image folder for this object")
        if d:
            self.var_source.set(d)

    def load_object(self) -> None:
        if not self._confirm_discard():
            return
        obj = Path(self.var_object.get().strip())
        src = Path(self.var_source.get().strip())
        alto_dir = obj / "ALTO"
        if not alto_dir.is_dir():
            messagebox.showerror("Review", f"No ALTO folder in:\n{obj}")
            return
        if not src.is_dir():
            messagebox.showerror("Review", f"Source image folder not found:\n{src}")
            return

        self._img_index = index_source_images(src)
        self._pages = []
        missing = 0
        for xml in sorted(alto_dir.glob("*.xml"), key=natural_key):
            info = scan_alto_file(xml)
            self._pages.append((xml.stem, info.pc, info.status))
            if xml.stem not in self._img_index:
                missing += 1

        self._settings["review_object_dir"] = str(obj)
        self._settings["review_source_dir"] = str(src)
        self._persist()

        self._page = None
        self._stem = None
        self._selected = None
        self._undo.clear()
        self._cancel_pending_add()
        self.canvas.delete("all")
        self.btn_save.configure(state="disabled")
        self._refresh_page_list()
        msg = f"{len(self._pages)} page(s) loaded."
        if missing:
            msg += f"  {missing} page(s) have no matching source image and can't be reviewed."
        self.var_status.set(msg)

    def _page_sort_key(self, entry: tuple[str, float | None, str]) -> tuple:
        stem, pc, status = entry
        if self.var_worst_first.get():
            rank = {"failed": 0, "scored": 1, "unscored": 2, "empty": 3}.get(status, 4)
            return (rank, pc if pc is not None else 2.0, natural_chunks(stem))
        return (natural_chunks(stem),)

    def _refresh_page_list(self) -> None:
        self.lst_pages.delete(0, "end")
        self._sorted_stems = []
        for stem, pc, status in sorted(self._pages, key=self._page_sort_key):
            if status == "scored" and pc is not None:
                label = f"{stem}   PC {pc:.2f}"
            else:
                label = f"{stem}   [{status}]"
            self.lst_pages.insert("end", label)
            self._sorted_stems.append(stem)
            if stem == self._stem:
                self.lst_pages.selection_set("end")

    def _on_page_selected(self, _event: object) -> None:
        sel = self.lst_pages.curselection()
        if not sel or sel[0] >= len(self._sorted_stems):
            return
        # Index into the parallel stem list — never parse the display label
        # back apart (stems can legally contain the label's separator).
        stem = self._sorted_stems[sel[0]]
        if stem == self._stem:
            return
        if not self._confirm_discard():
            self._refresh_page_list()
            return
        self._open_page(stem)

    def _confirm_discard(self) -> bool:
        """True → proceed (saved or discarded); False → stay on the page."""
        if self._page is None or not self._page.dirty:
            return True
        ans = messagebox.askyesnocancel(
            "Review", f"Save changes to {self._stem} first?")
        if ans is None:
            return False
        if ans:
            self._save_page()
            # A failed save (the error dialog came from _save_page) leaves
            # the page dirty — stay on it instead of discarding the edits.
            return not self._page.dirty
        return True

    def _open_page(self, stem: str) -> None:
        img_path = self._img_index.get(stem)
        if img_path is None:
            self.var_status.set(f"{stem}: no matching source image in the source folder.")
            return
        try:
            pil = Image.open(img_path).convert("RGB")
        except OSError as e:
            messagebox.showerror("Review", f"Could not open {img_path}:\n{e}")
            return
        alto_path = Path(self.var_object.get().strip()) / "ALTO" / f"{stem}.xml"
        try:
            page = ReviewPage(alto_path, pil.size)
        except StubPageError as e:
            self._page = None
            self._stem = stem
            self._pil = None
            self.canvas.delete("all")
            self.var_status.set(str(e))
            return

        self._page = page
        self._stem = stem
        self._pil = pil
        self._selected = None
        self._undo.clear()
        self._cancel_pending_add()
        self.btn_save.configure(state="disabled")
        self._render()
        self._update_editor()
        self._update_status()

    # ---------- rendering -------------------------------------------------

    def _zoom_scale(self) -> float:
        z = self.var_zoom.get()
        if z == "Fit":
            cw = max(self.canvas.winfo_width(), 1)
            ch = max(self.canvas.winfo_height(), 1)
            scale = max(min(cw / self._pil.width, ch / self._pil.height), 0.02)
        else:
            scale = int(z.rstrip("%")) / 100.0
        max_scale = (_MAX_RENDER_PX / (self._pil.width * self._pil.height)) ** 0.5
        return min(scale, max_scale)

    def _render_if_fit(self) -> None:
        if self._pil is not None and self.var_zoom.get() == "Fit":
            self._render()

    def _render(self) -> None:
        if self._pil is None or self._page is None:
            return
        self._scale = self._zoom_scale()
        w = max(int(self._pil.width * self._scale), 1)
        h = max(int(self._pil.height * self._scale), 1)
        resample = Image.LANCZOS if self._scale < 1 else Image.NEAREST
        self._photo = ImageTk.PhotoImage(self._pil.resize((w, h), resample))

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, w, h))

        self._rect_by_word.clear()
        self._word_by_rect.clear()
        threshold = self._threshold()
        edited = {id(w) for w, _c, _wc in self._undo}
        for word in self._page.words:
            flagged = word.needs_review(threshold)
            if not (flagged or self.var_show_all.get() or id(word) in edited):
                continue
            x, y, bw, bh = word.box_px
            color = _COLOR_EDITED if id(word) in edited else _wc_color(word.wc)
            item = self.canvas.create_rectangle(
                x * self._scale, y * self._scale,
                (x + bw) * self._scale, (y + bh) * self._scale,
                outline=color, width=3 if word is self._selected else 2,
            )
            self._rect_by_word[id(word)] = item
            self._word_by_rect[item] = word

        # A pending add-word box survives re-renders (zoom, threshold
        # changes) — redraw it; canvas.delete("all") above dropped the item.
        if self._pending_box is not None:
            x, y, bw, bh = self._pending_box
            self._drag_item = self.canvas.create_rectangle(
                x * self._scale, y * self._scale,
                (x + bw) * self._scale, (y + bh) * self._scale,
                outline=_COLOR_EDITED, width=2, dash=(4, 2),
            )
        else:
            self._drag_item = None

    def _threshold(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.var_threshold.get())))
        except (tk.TclError, ValueError):
            return DEFAULT_LOW_WC

    def _on_threshold_changed(self) -> None:
        self._settings["review_threshold"] = self._threshold()
        self._persist()
        self._render()
        self._update_status()

    def _on_wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _on_wheel_h(self, event: tk.Event) -> None:
        self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")

    # ---------- selection / editing --------------------------------------

    def _on_canvas_click(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self._page is None:
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

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self._drag_item is None:
            self._drag_item = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=_COLOR_EDITED, width=2, dash=(4, 2))
        else:
            self.canvas.coords(self._drag_item, x0, y0, x1, y1)

    def _on_canvas_release(self, event: tk.Event) -> None:
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
            return
        self._undo.append((word, None, None))  # None content marks an addition
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

    def _insert_glyph(self, ch: str) -> None:
        """Type a palette glyph into the edit box at the cursor."""
        self.ent_edit.insert("insert", ch)
        self.ent_edit.focus_set()

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
            self.lbl_wc.configure(text="new word")
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
            text=f"WC {word.wc:.2f}" if word.wc is not None else "WC —  (unscored)")
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
            self._undo.append((word, word.content, word.wc))
            self._page.edit_word(word, new_text)
            item = self._rect_by_word.get(id(word))
            if item is not None:
                self.canvas.itemconfigure(item, outline=_COLOR_EDITED)
            self.btn_save.configure(state="normal")
        self._update_status()
        if advance:
            self._goto_flagged(+1)

    def _delete_word(self) -> None:
        word = self._selected
        if word is None or self._page is None:
            return
        if not messagebox.askyesno(
            "Review",
            f"Delete the word {word.content!r}?\n\n"
            "Deletion can't be undone except by reloading the page unsaved.",
        ):
            return
        nxt = self._next_after(word)
        if any(w is word and c is None for w, c, _wc in self._undo):
            # Deleting a word added this session: net it out of the
            # provenance count instead of counting an add plus a delete.
            self._page.undo_add(word)
        else:
            self._page.delete_word(word)
        self._undo = [(w, c, wc) for (w, c, wc) in self._undo if w is not word]
        self._selected = None
        self.btn_save.configure(state="normal")
        self._render()
        self._select_word(nxt)
        self._update_status()

    def _undo_edit(self) -> None:
        if not self._undo or self._page is None:
            return
        word, old_content, old_wc = self._undo.pop()
        if old_content is None:  # the entry was an added word — remove it again
            self._page.undo_add(word)
            if self._selected is word:
                self._selected = None
            if self._page.session_edits == 0 and not self._page.dirty:
                self.btn_save.configure(state="disabled")
            self._render()
            self._update_editor()
            self._update_status()
            return
        self._page.revert_word(word, old_content, old_wc)
        item = self._rect_by_word.get(id(word))
        if item is not None:
            self.canvas.itemconfigure(item, outline=_wc_color(word.wc))
        if self._page.session_edits == 0 and not self._page.dirty:
            self.btn_save.configure(state="disabled")
        self._select_word(word)
        self._update_status()

    def _flagged_now(self) -> list[Word]:
        if self._page is None:
            return []
        edited = {id(w) for w, _c, _wc in self._undo}
        return [w for w in self._page.flagged(self._threshold()) if id(w) not in edited]

    def _next_after(self, word: Word) -> Optional[Word]:
        flagged = self._flagged_now()
        after = [w for w in flagged if w is not word]
        if not after or self._page is None:
            return None
        try:
            idx = self._page.words.index(word)
        except ValueError:
            return after[0]
        for w in after:
            if self._page.words.index(w) > idx:
                return w
        return after[0]

    def _goto_flagged(self, delta: int) -> None:
        flagged = self._flagged_now()
        if not flagged:
            self._select_word(None)
            self.var_status.set("No flagged words left on this page. Save and pick the next page.")
            return
        if self._selected is None or self._selected not in flagged:
            self._select_word(flagged[0] if delta > 0 else flagged[-1])
            return
        idx = (flagged.index(self._selected) + delta) % len(flagged)
        self._select_word(flagged[idx])

    # ---------- save / status --------------------------------------------

    def _save_page(self) -> None:
        if self._page is None:
            return
        try:
            txt = self._page.save()
        except ConcurrentEditError as e:
            # Someone else wrote this ALTO while it was open here. Only the
            # corrector can decide whose version wins.
            if not messagebox.askyesno(
                "Review",
                f"{e}\n\nOverwrite the on-disk version with your edits?",
            ):
                return
            try:
                txt = self._page.save(force=True)
            except OSError as e2:
                messagebox.showerror("Review", f"Save failed:\n{e2}")
                return
        except OSError as e:
            messagebox.showerror("Review", f"Save failed:\n{e}")
            return
        self._undo.clear()
        self.btn_save.configure(state="disabled")
        # Refresh this page's PC in the list (save recomputed it).
        info = scan_alto_file(self._page.alto_path)
        self._pages = [
            (stem, info.pc if stem == self._stem else pc,
             info.status if stem == self._stem else status)
            for stem, pc, status in self._pages
        ]
        self._refresh_page_list()
        self._render()
        extra = f" + {txt.name}" if txt is not None else ""
        self.var_status.set(f"Saved {self._page.alto_path.name}{extra}.")

    def _update_status(self) -> None:
        if self._page is None:
            return
        n_flagged = len(self._flagged_now())
        dirty = "  •  unsaved changes" if self._page.dirty else ""
        self.var_status.set(
            f"{self._stem}: {len(self._page.words)} words, {n_flagged} flagged "
            f"below {self._threshold():.2f}{dirty}"
        )

    # ---------- app integration ------------------------------------------

    def has_unsaved_changes(self) -> bool:
        return self._page is not None and self._page.dirty

    def confirm_close(self) -> bool:
        """Called by the app's close handler; True → OK to quit."""
        return self._confirm_discard()
