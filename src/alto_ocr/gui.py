"""Tkinter desktop GUI for alto_ocr.

A thin wrapper over the existing pipeline. Two tabs — **Run** (folder
pickers, output formats, engine choice, Run/Cancel, progress and the live
log) and **Review** (transcript correction, ``review.py``) — under a menu
bar whose **Tools ▸ Advanced settings…** opens the engine and
preprocessing options, including the Tesseract location, in a window of
their own.

Run with::

    python -m src.alto_ocr.gui

Threading model: the pipeline runs on a daemon worker thread, log
records and progress updates flow back through ``queue.Queue`` and are
drained on the main thread via ``root.after`` so Tk widgets are only
touched from the thread that owns them.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Mapping, Optional

from . import __version__, theme
# The OCR stack (ocr, pipeline, preprocess, glmocr) is imported inside the
# run worker, not here: it loads OpenCV and numpy, ~190 MB of native
# libraries the window would otherwise wait for on every launch (worst on a
# cold Windows start, where antivirus scans each DLL as it loads). Measured
# 2026-10-28: importing this module fell 0.73 s -> 0.09 s on a warm Linux
# cache. The defaults the window needs come from the stdlib-only config.
from .config import DEFAULT_MODEL, DEFAULT_TIMEOUT_S, GLMOCRConfig, OCROptions, PreprocessConfig
from .review import ReviewFrame
from .walker import SkippedRefId, discover_collection, discover_ref_id

if TYPE_CHECKING:
    from .ocr import OCREngine
    from .pipeline import RefIdResult

APP_TITLE = "Alto OCR"
SETTINGS_PATH = Path.home() / ".alto_ocr.json"
logger = logging.getLogger("alto_ocr.gui")


@dataclass
class JobConfig:
    # Run inputs
    input_dir: Path
    output_dir: Path
    recurse: bool
    # Output formats
    write_mets: bool
    write_plaintext: bool
    with_checksums: bool = True
    # Engine selection. Default matches the CLI + CLAUDE.md (Tesseract is
    # the default — its confidence output drives the remediation workflow);
    # GLMOCR remains selectable for handwriting-heavy material.
    engine: str = "tesseract"  # "tesseract" | "glmocr"
    tesseract_path: Optional[Path] = None
    # Advanced — mirror the upstream dataclasses they configure.
    preprocess_config: PreprocessConfig = field(default_factory=PreprocessConfig)
    debug_preprocessed_dir: Optional[Path] = None
    ocr_options: OCROptions = field(default_factory=OCROptions)
    glmocr_endpoint: Optional[str] = None
    glmocr_model: str = DEFAULT_MODEL
    glmocr_timeout: int = DEFAULT_TIMEOUT_S


class SettingsError(ValueError):
    """An Advanced setting the user has to fix; the message says what."""


def parse_advanced(raw: Mapping[str, object]) -> dict:
    """Validate the Advanced settings fields into ``JobConfig`` keyword
    arguments. ``raw`` holds the fields as the widgets return them (text
    for the number boxes, so "abc" arrives as "abc" rather than as a Tk
    error). Raises ``SettingsError`` naming what to fix. Pure — no Tk — so
    the rules are testable headless.
    """
    def whole_number(key: str, label: str) -> int:
        try:
            return int(str(raw.get(key, "")).strip())
        except ValueError:
            raise SettingsError(f"{label} must be a whole number.") from None

    min_dpi = whole_number("min_dpi", "Min DPI")
    psm = whole_number("psm", "PSM")
    oem = whole_number("oem", "OEM")
    tess_timeout = whole_number("tesseract_timeout", "Tesseract timeout")
    glmocr_timeout = whole_number("glmocr_timeout", "GLMOCR timeout")
    if min_dpi <= 0:
        raise SettingsError("Min DPI must be positive.")
    if not 0 <= psm <= 13:
        raise SettingsError("PSM must be between 0 and 13.")
    if not 0 <= oem <= 3:
        raise SettingsError("OEM must be between 0 and 3.")
    if tess_timeout <= 0:
        raise SettingsError("Tesseract timeout must be positive.")
    if glmocr_timeout <= 0:
        raise SettingsError("GLMOCR timeout must be positive.")

    tess_str = str(raw.get("tesseract_path", "") or "").strip()
    tess_path = Path(tess_str).expanduser() if tess_str else None
    if tess_path is not None and not tess_path.is_file():
        raise SettingsError(
            f"The Tesseract program path does not point at a file:\n{tess_path}\n\n"
            "Leave the field empty to find Tesseract automatically, or pick "
            "the program with Browse…")

    debug_str = str(raw.get("debug_preprocessed", "") or "").strip()
    return dict(
        tesseract_path=tess_path,
        preprocess_config=PreprocessConfig(
            min_dpi=min_dpi,
            # The frame-changing stages: both engines force them off so
            # word boxes stay on the original scan (ocr.adjust_preprocess),
            # so the GUI offers no switch that would only pretend to.
            do_deskew=False,
            do_border_crop=False,
            do_denoise=bool(raw.get("do_denoise")),
            do_clahe=bool(raw.get("do_clahe")),
            do_binarize=bool(raw.get("do_binarize")),
        ),
        debug_preprocessed_dir=Path(debug_str).expanduser() if debug_str else None,
        ocr_options=OCROptions(
            language=str(raw.get("ocr_lang", "") or "").strip() or "eng",
            psm=psm, oem=oem, timeout_s=tess_timeout),
        glmocr_endpoint=str(raw.get("glmocr_endpoint", "") or "").strip() or None,
        glmocr_model=(str(raw.get("glmocr_model", "") or "").strip()
                      or GLMOCRConfig().model),
        glmocr_timeout=glmocr_timeout,
        with_checksums=bool(raw.get("with_checksums", True)),
    )


def advanced_settings(adv: Mapping[str, object]) -> dict:
    """The settings-file entries for validated Advanced options (the output
    of ``parse_advanced``). A ``None`` value means "remove the key": the
    Tesseract path is saved only when the user chose one — an auto-detected
    path is re-derived each launch, so a reinstall can't strand the app on
    a stale location."""
    pc = adv["preprocess_config"]
    oo = adv["ocr_options"]
    debug_dir = adv["debug_preprocessed_dir"]
    return {
        "tesseract_path": str(adv["tesseract_path"]) if adv["tesseract_path"] else None,
        "min_dpi": pc.min_dpi,
        "do_denoise": pc.do_denoise,
        "do_clahe": pc.do_clahe,
        "do_binarize": pc.do_binarize,
        "debug_preprocessed": str(debug_dir) if debug_dir else "",
        "ocr_lang": oo.language,
        "psm": oo.psm,
        "oem": oo.oem,
        "tesseract_timeout": oo.timeout_s,
        "glmocr_endpoint": adv["glmocr_endpoint"] or "",
        "glmocr_model": adv["glmocr_model"],
        "glmocr_timeout": adv["glmocr_timeout"],
        "with_checksums": adv["with_checksums"],
        # Retired with their checkboxes (engines force both stages off).
        "do_deskew": None,
        "do_border_crop": None,
    }


# Bumped when a saved preference has to be retired rather than merely
# defaulted. Saved settings normally win over dataclass defaults, so a
# default flip alone would never reach anyone who has run the GUI before.
_BINARIZE_MIGRATION_KEY = "_migrated_binarize_default_off"
# The 2026-07-30 branch build shipped the same migration under this marker
# name; honor it too, or a settings file that build already migrated would
# be migrated again — clearing a do_binarize the user deliberately re-chose.
_BINARIZE_MIGRATION_KEY_2026_07_30 = "binarize_default_2026_07_30"


def _migrate_settings(s: dict) -> dict:
    """Retire a persisted ``do_binarize: true`` (one-shot).

    Binarization was default-on until v0.8.8 (2026-07-30), when it was
    measured to cost accuracy on every collection tested (see
    PreprocessConfig.do_binarize). A plain default flip would not reach
    existing users, whose saved settings take precedence — so drop the
    stale preference once and record that we did. Anyone who deliberately
    re-checks the box afterwards keeps it: the marker means this runs only
    the first time. Pure function of the dict so it stays testable
    without Tk.
    """
    if s.get(_BINARIZE_MIGRATION_KEY) or s.get(_BINARIZE_MIGRATION_KEY_2026_07_30):
        return s
    if s.get("do_binarize"):
        s.pop("do_binarize", None)
        logger.info("Binarize is no longer on by default (it lowered OCR accuracy on every "
                    "collection measured); your saved preference for it has been cleared. "
                    "Re-check the box if a collection needs it.")
    s[_BINARIZE_MIGRATION_KEY] = True
    return s


def _load_settings() -> dict:
    try:
        return _migrate_settings(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not save settings to %s: %s", SETTINGS_PATH, e)


class _QueueLogHandler(logging.Handler):
    """Forward formatted log records to a thread-safe queue.

    Tk widgets must only be touched from the main thread; the worker
    pushes records through this handler and the main loop drains the
    queue on a timer to safely append to the Text widget.
    """

    def __init__(self, q: "queue.Queue[tuple[str, str]]") -> None:
        super().__init__()
        self.queue = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.queue.put((record.levelname, msg))


class AltoOcrApp:
    POLL_MS = 100

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        # Room for the Review tab's two toolbars, clamped for small screens.
        width = min(1000, int(root.winfo_screenwidth() * 0.95))
        height = min(780, int(root.winfo_screenheight() * 0.88))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(720, 560)

        # All settings — basic and advanced — restore from the persisted
        # JSON on launch so users keep their preferred recipe across runs.
        self._settings = _load_settings()  # migrations applied inside
        s = self._settings
        pp = PreprocessConfig()  # source of truth for preprocessing defaults
        oo = OCROptions()        # source of truth for Tesseract option defaults
        gc = GLMOCRConfig()      # source of truth for GLMOCR defaults

        # ----- Tk variables — model state behind the widgets. -----
        # Run-tab basics
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        # Once the user manually picks the output, stop auto-updating from input.
        self._output_overridden = False
        self.var_recurse = tk.BooleanVar(value=bool(s.get("recurse", False)))
        self.var_mets = tk.BooleanVar(value=bool(s.get("write_mets", True)))
        self.var_plaintext = tk.BooleanVar(value=bool(s.get("write_plaintext", True)))
        self.var_engine = tk.StringVar(value=str(s.get("engine", "tesseract")))
        self.var_status = tk.StringVar(value="Ready.")

        # Advanced settings window. Number fields are StringVars so a
        # mistyped value reaches parse_advanced as text, not as a TclError.
        self.var_tesseract = tk.StringVar(value=str(s.get("tesseract_path", "")))
        self.var_min_dpi = tk.StringVar(value=str(s.get("min_dpi", pp.min_dpi)))
        self.var_do_denoise = tk.BooleanVar(value=bool(s.get("do_denoise", pp.do_denoise)))
        self.var_do_clahe = tk.BooleanVar(value=bool(s.get("do_clahe", pp.do_clahe)))
        self.var_do_binarize = tk.BooleanVar(value=bool(s.get("do_binarize", pp.do_binarize)))
        self.var_debug_preproc = tk.StringVar(value=str(s.get("debug_preprocessed", "")))
        self.var_ocr_lang = tk.StringVar(value=str(s.get("ocr_lang", oo.language)))
        self.var_psm = tk.StringVar(value=str(s.get("psm", oo.psm)))
        self.var_oem = tk.StringVar(value=str(s.get("oem", oo.oem)))
        self.var_tess_timeout = tk.StringVar(
            value=str(s.get("tesseract_timeout", oo.timeout_s)))
        self.var_glmocr_endpoint = tk.StringVar(value=str(s.get("glmocr_endpoint", "")))
        self.var_glmocr_model = tk.StringVar(value=str(s.get("glmocr_model", gc.model)))
        self.var_glmocr_timeout = tk.StringVar(value=str(s.get("glmocr_timeout", gc.timeout_s)))
        self.var_with_checksums = tk.BooleanVar(value=bool(s.get("with_checksums", True)))
        self._adv_win: Optional[tk.Toplevel] = None
        # View ▸ Theme: light (native look), dark, or match Windows.
        self.var_theme = tk.StringVar(value=theme.normalize_mode(s.get("theme")))

        # Worker bookkeeping.
        self._worker: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._progress_queue: "queue.Queue[tuple[int, int]]" = queue.Queue()

        # Auto-fill output folder when the input changes (until overridden).
        self.var_input.trace_add("write", self._on_input_changed)

        self._build_menu()
        self._build_ui()
        self._apply_theme()
        # "Match Windows" re-checks whenever the window comes forward, so a
        # change in Windows' colour settings shows up without a restart.
        self.root.bind("<Activate>", self._on_activate, add="+")
        self._install_logging()
        self._poll_after = self.root.after(self.POLL_MS, self._drain_queues)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI construction ------------------------------------------------

    _MENUS = ("File", "Tools", "View", "Help")

    def _fill_menu(self, name: str, menu: tk.Menu) -> Optional[tk.Menu]:
        """One menu's entries. Returns the Theme submenu for "View"."""
        if name == "File":
            menu.add_command(label="Exit", command=self._on_close)
        elif name == "Tools":
            menu.add_command(label="Advanced settings…", accelerator="Ctrl+,",
                             command=self._open_advanced)
        elif name == "View":
            themes = tk.Menu(menu, tearoff=False)
            for mode in theme.MODES:
                themes.add_radiobutton(label=theme.MODE_LABELS[mode], value=mode,
                                       variable=self.var_theme,
                                       command=self._on_theme_chosen)
            menu.add_cascade(label="Theme", menu=themes)
            return themes
        elif name == "Help":
            menu.add_command(label="About Alto OCR", command=self._show_about)
        return None

    def _build_menu(self) -> None:
        # Windows draws the native menu bar itself and ignores Tk's colours,
        # so the dark theme swaps it for a themed bar of menubuttons with the
        # same menus (_apply_theme). Both copies share var_theme.
        menubar = tk.Menu(self.root)
        menus: dict[str, tk.Menu] = {}
        themes: Optional[tk.Menu] = None
        for name in self._MENUS:
            menus[name] = tk.Menu(menubar, tearoff=False)
            themes = self._fill_menu(name, menus[name]) or themes
            menubar.add_cascade(label=name, menu=menus[name], underline=0)
        themed = ttk.Frame(self.root, style="Menubar.TFrame")
        for name in self._MENUS:
            button = ttk.Menubutton(themed, text=name, style="Menubar.TMenubutton",
                                    takefocus=False)
            menu = tk.Menu(button, tearoff=False)
            self._fill_menu(name, menu)
            button.configure(menu=menu)
            button.pack(side="left")
        self.root.configure(menu=menubar)
        self.root.bind("<Control-comma>", lambda _e: self._open_advanced())
        self.menubar = menubar
        self.themed_menubar = themed
        self.tools_menu = menus["Tools"]
        self.theme_menu = themes

    def _build_ui(self) -> None:
        # Only the notebook: everything about a run — Run/Cancel, progress,
        # the log — lives on the Run tab, so the Review tab gets the whole
        # window for the page it is correcting.
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        tab_run = ttk.Frame(nb, padding=8)
        nb.add(tab_run, text="Run")
        self._build_run_tab(tab_run)
        self.review = ReviewFrame(nb, self._settings,
                                  lambda: _save_settings(self._settings))
        nb.add(self.review, text="Review")
        self._nb = nb
        self.tab_run = tab_run

    def _build_run_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(7, weight=1)  # the log takes the leftover height

        ttk.Label(tab, text="Folder to OCR:").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(tab, textvariable=self.var_input).grid(
            row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(tab, text="Browse…", command=self._browse_input).grid(
            row=0, column=2, padx=(8, 0), pady=(0, 6))

        ttk.Label(tab, text="Output folder:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(tab, textvariable=self.var_output).grid(
            row=1, column=1, sticky="ew", pady=(0, 6))
        self.btn_browse_output = ttk.Button(tab, text="Browse…", command=self._browse_output)
        self.btn_browse_output.grid(row=1, column=2, padx=(8, 0), pady=(0, 6))

        ttk.Checkbutton(
            tab,
            text="OCR all subfolders inside the target folder (batch mode)",
            variable=self.var_recurse,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 4))

        formats = ttk.LabelFrame(tab, text="Output formats", padding=8)
        formats.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        ttk.Checkbutton(formats, text="METS / ALTO", variable=self.var_mets).pack(
            side="left", padx=(0, 16))
        ttk.Checkbutton(formats, text="Plain text", variable=self.var_plaintext).pack(
            side="left")

        engine_frame = ttk.LabelFrame(tab, text="OCR engine", padding=8)
        engine_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        radio_row = ttk.Frame(engine_frame)
        radio_row.pack(fill="x")
        ttk.Radiobutton(radio_row, text="Tesseract (default — word confidences)",
                        variable=self.var_engine, value="tesseract").pack(
            side="left", padx=(0, 16))
        ttk.Radiobutton(radio_row, text="GLMOCR (handwriting)",
                        variable=self.var_engine, value="glmocr").pack(side="left")
        hint = ttk.Label(
            engine_frame, cursor="hand2", style="Link.TLabel",
            text="Tesseract location and other engine options: Tools ▸ Advanced settings…")
        hint.pack(anchor="w", pady=(6, 0))
        hint.bind("<Button-1>", lambda _e: self._open_advanced())

        actions = ttk.Frame(tab)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.btn_run = ttk.Button(actions, text="Run OCR", command=self._on_run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="Cancel",
                                     command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.btn_clear = ttk.Button(actions, text="Clear log",
                                    command=self._clear_log)
        self.btn_clear.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.var_status, style="Muted.TLabel").pack(
            side="right")

        self.progress = ttk.Progressbar(tab, mode="determinate")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 6))

        log_frame = ttk.LabelFrame(tab, text="Log", padding=4)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame, wrap="none", height=8,
            background="#101418", foreground="#d8d8d8",
            insertbackground="#d8d8d8", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll_y.set, state="disabled")
        self.log_text.theme_exempt = True  # dark in both themes
        self.log_text.tag_configure("ERROR", foreground="#ff6e6e")
        self.log_text.tag_configure("WARNING", foreground="#ffd166")
        self.log_text.tag_configure("INFO", foreground="#d8d8d8")
        self.log_text.tag_configure("DEBUG", foreground="#7d8896")

    # ---------- Advanced settings window ---------------------------------------

    def _open_advanced(self) -> None:
        """Tools ▸ Advanced settings…: one window, raised if already open.
        Edits apply to the next run and are saved when the window closes."""
        if self._adv_win is not None and self._adv_win.winfo_exists():
            self._adv_win.deiconify()
            self._adv_win.lift()
            self._adv_win.focus_set()
            return
        win = tk.Toplevel(self.root)
        win.title(f"Advanced settings — {APP_TITLE}")
        win.transient(self.root)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        self._build_advanced_panel(body)

        buttons = ttk.Frame(body)
        buttons.grid(row=10, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(buttons, text="Reset to defaults",
                   command=self._on_reset_advanced).pack(side="left")
        ttk.Button(buttons, text="Close", command=self._close_advanced).pack(side="right")
        ttk.Label(buttons, text="Changes apply to the next OCR run.",
                  style="Muted.TLabel").pack(side="right", padx=(0, 12))

        theme.recolor_tree(win)
        win.protocol("WM_DELETE_WINDOW", self._close_advanced)
        win.bind("<Escape>", lambda _e: self._close_advanced())
        win.geometry(f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 40}")
        self._adv_win = win

    # ---------- Theme ----------------------------------------------------------

    def _apply_theme(self) -> None:
        palette = theme.apply(self.root, self.var_theme.get())
        self._theme_dark = palette.dark
        if palette.dark:
            self.root.configure(menu="")
            self.themed_menubar.pack(side="top", fill="x", before=self._nb)
        else:
            self.themed_menubar.pack_forget()
            self.root.configure(menu=self.menubar)

    def _on_theme_chosen(self) -> None:
        self._apply_theme()
        self._update_settings({"theme": self.var_theme.get()})

    def _on_activate(self, event: tk.Event) -> None:
        if (event.widget is self.root and self.var_theme.get() == "system"
                and theme.system_prefers_dark() != self._theme_dark):
            self._apply_theme()

    def _close_advanced(self) -> None:
        """Validate and save; an invalid field keeps the window open."""
        win = self._adv_win
        if win is None:
            return
        try:
            adv = parse_advanced(self._advanced_raw())
        except SettingsError as e:
            messagebox.showerror("Advanced settings", str(e), parent=win)
            return
        self._update_settings(advanced_settings(adv))
        win.destroy()
        self._adv_win = None

    def _build_advanced_panel(self, panel: ttk.Frame) -> None:
        panel.columnconfigure(0, weight=1)

        # ----- Tesseract OCR (its program location moved here from Run) -----
        tess = ttk.LabelFrame(panel, text="Tesseract OCR", padding=8)
        tess.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tess.columnconfigure(2, weight=1)
        ttk.Label(tess, text="Tesseract program:").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(tess, textvariable=self.var_tesseract, width=48).grid(
            row=0, column=1, columnspan=2, sticky="ew")
        ttk.Button(tess, text="Browse…", command=self._browse_tesseract).grid(
            row=0, column=3, padx=(8, 0))
        ttk.Label(
            tess, style="Muted.TLabel",
            text="Leave empty to find it automatically (PATH, TESSERACT_PATH, "
                 "or the standard install folder).",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 6))
        for row, (label, widget, hint) in enumerate((
            ("Language:", ttk.Entry(tess, textvariable=self.var_ocr_lang, width=14),
             "e.g. 'eng', 'fra', 'eng+lat'."),
            ("PSM (page segmentation):",
             ttk.Spinbox(tess, from_=0, to=13, increment=1,
                         textvariable=self.var_psm, width=6),
             "3=auto (default), 1=auto + rotated pages, 4=single column, 6=uniform block."),
            ("OEM (engine mode):",
             ttk.Spinbox(tess, from_=0, to=3, increment=1,
                         textvariable=self.var_oem, width=6),
             "3=default LSTM (recommended)."),
            ("Timeout per page (s):",
             ttk.Entry(tess, textvariable=self.var_tess_timeout, width=8),
             "A page taking longer is recorded as failed and the run moves on."),
        ), start=2):
            ttk.Label(tess, text=label).grid(row=row, column=0, sticky="w",
                                             padx=(0, 8), pady=(4, 0))
            widget.grid(row=row, column=1, sticky="w", pady=(4, 0))
            ttk.Label(tess, text=hint, style="Muted.TLabel").grid(
                row=row, column=2, columnspan=2, sticky="w", padx=(8, 0), pady=(4, 0))

        # ----- GLMOCR (handwriting) -----
        gl = ttk.LabelFrame(panel, text="GLMOCR (handwriting)", padding=8)
        gl.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        gl.columnconfigure(2, weight=1)
        ttk.Label(gl, text="Endpoint:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(gl, textvariable=self.var_glmocr_endpoint).grid(
            row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(
            gl, text="Leave empty to use OLLAMA_HOST or the built-in default.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(gl, text="Model:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(gl, textvariable=self.var_glmocr_model, width=24).grid(
            row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            gl, text="Ollama model tag, e.g. 'glm-ocr:q8_0'.", style="Muted.TLabel",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(gl, text="Timeout (sec):").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Spinbox(
            gl, from_=10, to=900, increment=10,
            textvariable=self.var_glmocr_timeout, width=8,
        ).grid(row=3, column=1, sticky="w", pady=(6, 0))

        # ----- Preprocessing -----
        pp = ttk.LabelFrame(panel, text="Preprocessing", padding=8)
        pp.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        pp.columnconfigure(1, weight=1)
        ttk.Label(pp, text="Min DPI:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Spinbox(
            pp, from_=72, to=2400, increment=50,
            textvariable=self.var_min_dpi, width=8,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            pp, text="Upscale low-DPI scans to at least this DPI before OCR.",
            style="Muted.TLabel",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        stages = ttk.Frame(pp)
        stages.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(stages, text="Denoise",
                        variable=self.var_do_denoise).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="CLAHE contrast",
                        variable=self.var_do_clahe).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="Binarize",
                        variable=self.var_do_binarize).pack(side="left", padx=(0, 12))
        ttk.Label(
            pp, style="Muted.TLabel",
            text="Deskew and border crop always stay off: they would move the "
                 "text away from where the word boxes place it on the original scan.",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))
        ttk.Label(pp, text="Save preprocessed images to:").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(pp, textvariable=self.var_debug_preproc).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(pp, text="Browse…", command=self._browse_debug_preproc).grid(
            row=3, column=3, padx=(8, 0), pady=(8, 0))
        ttk.Label(
            pp, text="Leave empty to skip; useful for debugging OCR accuracy.",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # ----- METS -----
        mets = ttk.LabelFrame(panel, text="METS", padding=8)
        mets.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            mets, text="Compute MD5 checksums (slower on large objects)",
            variable=self.var_with_checksums,
        ).pack(side="left")

    def _advanced_raw(self) -> dict:
        return {
            "tesseract_path": self.var_tesseract.get(),
            "min_dpi": self.var_min_dpi.get(),
            "do_denoise": self.var_do_denoise.get(),
            "do_clahe": self.var_do_clahe.get(),
            "do_binarize": self.var_do_binarize.get(),
            "debug_preprocessed": self.var_debug_preproc.get(),
            "ocr_lang": self.var_ocr_lang.get(),
            "psm": self.var_psm.get(),
            "oem": self.var_oem.get(),
            "tesseract_timeout": self.var_tess_timeout.get(),
            "glmocr_endpoint": self.var_glmocr_endpoint.get(),
            "glmocr_model": self.var_glmocr_model.get(),
            "glmocr_timeout": self.var_glmocr_timeout.get(),
            "with_checksums": self.var_with_checksums.get(),
        }

    def _on_reset_advanced(self) -> None:
        """Restore every Advanced setting to its dataclass default."""
        pp = PreprocessConfig()
        oo = OCROptions()
        gc = GLMOCRConfig()
        self.var_tesseract.set("")
        self.var_min_dpi.set(str(pp.min_dpi))
        self.var_do_denoise.set(pp.do_denoise)
        self.var_do_clahe.set(pp.do_clahe)
        self.var_do_binarize.set(pp.do_binarize)
        self.var_debug_preproc.set("")
        self.var_ocr_lang.set(oo.language)
        self.var_psm.set(str(oo.psm))
        self.var_oem.set(str(oo.oem))
        self.var_tess_timeout.set(str(oo.timeout_s))
        self.var_glmocr_endpoint.set("")
        self.var_glmocr_model.set(gc.model)
        self.var_glmocr_timeout.set(str(gc.timeout_s))
        self.var_with_checksums.set(True)

    # ---------- Helpers --------------------------------------------------------

    def _install_logging(self) -> None:
        # Attach a queue handler high enough in the namespace tree to catch
        # every alto_ocr submodule logger via standard log propagation.
        h = _QueueLogHandler(self._log_queue)
        h.setLevel(logging.INFO)
        for name in ("alto_ocr", "src.alto_ocr"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            lg.addHandler(h)

    def _on_input_changed(self, *_: object) -> None:
        if self._output_overridden:
            return
        in_str = self.var_input.get().strip()
        if not in_str:
            self.var_output.set("")
            return
        try:
            inp = Path(in_str).expanduser()
        except (OSError, ValueError):
            return
        # Sibling "<name>_output/" directory: derived from the input name so
        # it never lands inside the natural batch-parent of the input. Falls
        # back to "output/" when the input is a drive root (no name) or has
        # no parent distinct from itself.
        parent = inp.parent
        suffix_name = f"{inp.name}_output" if inp.name else "output"
        candidate = parent / suffix_name if parent != inp else inp / "output"
        self.var_output.set(str(candidate))

    def _browse_input(self) -> None:
        d = filedialog.askdirectory(title="Select folder to OCR", mustexist=True)
        if d:
            self.var_input.set(d)

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(title="Select output folder", mustexist=False)
        if d:
            self._output_overridden = True
            self.var_output.set(d)

    def _browse_tesseract(self) -> None:
        f = filedialog.askopenfilename(
            title="Select the Tesseract program",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
            parent=self._adv_win or self.root,
        )
        if f:
            self.var_tesseract.set(f)

    def _browse_debug_preproc(self) -> None:
        d = filedialog.askdirectory(
            title="Select folder for preprocessed-image dumps", mustexist=False,
            parent=self._adv_win or self.root,
        )
        if d:
            self.var_debug_preproc.set(d)

    def _show_about(self) -> None:
        messagebox.showinfo(
            f"About {APP_TITLE}",
            f"{APP_TITLE} {__version__}\n\n"
            "Archival page scans → METS/ALTO XML and plain-text transcripts, "
            "with a review tab for correcting them.\n\n"
            f"Settings are kept in:\n{SETTINGS_PATH}",
            parent=self.root,
        )

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _update_settings(self, changes: Mapping[str, object]) -> None:
        """Merge ``changes`` into the settings (``None`` removes a key) and
        write the file only if anything actually changed."""
        s = dict(self._settings)
        for key, value in changes.items():
            if value is None:
                s.pop(key, None)
            else:
                s[key] = value
        if s != self._settings:
            # Update in place: the Review tab holds a reference to this dict.
            self._settings.clear()
            self._settings.update(s)
            _save_settings(self._settings)

    # ---------- Run lifecycle --------------------------------------------------

    def _on_run(self) -> None:
        if self._worker and self._worker.is_alive():
            return  # ignore double-clicks
        cfg = self._collect_config()
        if cfg is None:
            return
        self._persist_settings(cfg)
        self._cancel_event.clear()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(value=0, maximum=1)
        self.var_status.set("Running…")
        self.root.title(f"{APP_TITLE} — OCR running…")
        pc = cfg.preprocess_config
        logger.info(
            "Starting: input=%s output=%s recurse=%s mets=%s plaintext=%s "
            "checksums=%s engine=%s lang=%s psm=%d oem=%d min_dpi=%d "
            "stages=[denoise=%s clahe=%s binarize=%s]",
            cfg.input_dir, cfg.output_dir, cfg.recurse,
            cfg.write_mets, cfg.write_plaintext, cfg.with_checksums,
            cfg.engine, cfg.ocr_options.language, cfg.ocr_options.psm,
            cfg.ocr_options.oem, pc.min_dpi,
            pc.do_denoise, pc.do_clahe, pc.do_binarize,
        )
        self._worker = threading.Thread(
            target=self._worker_main, args=(cfg,), daemon=True,
        )
        self._worker.start()

    def _on_cancel(self) -> None:
        if not self._worker or not self._worker.is_alive():
            return
        self._cancel_event.set()
        self.var_status.set("Cancelling — finishing current Ref ID…")
        self.btn_cancel.configure(state="disabled")

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(
                APP_TITLE,
                "OCR is still running. Quit anyway?\n\n"
                "The current Ref ID will finish; new Ref IDs will be skipped.",
            ):
                return
            self._cancel_event.set()
        if not self.review.confirm_close():
            return
        if self._adv_win is not None and self._adv_win.winfo_exists():
            # Keep what was set in an open Advanced window, if it is valid;
            # never block quitting over a half-typed field.
            try:
                self._update_settings(advanced_settings(parse_advanced(self._advanced_raw())))
            except SettingsError:
                pass
        self.root.after_cancel(self._poll_after)
        self.root.destroy()

    def _collect_config(self) -> JobConfig | None:
        in_str = self.var_input.get().strip()
        out_str = self.var_output.get().strip()
        if not in_str:
            messagebox.showerror(APP_TITLE, "Please choose a folder to OCR.")
            return None
        in_path = Path(in_str).expanduser()
        if not in_path.is_dir():
            messagebox.showerror(APP_TITLE, f"Folder does not exist:\n{in_path}")
            return None
        if not out_str:
            messagebox.showerror(APP_TITLE, "Please choose an output folder.")
            return None
        out_path = Path(out_str).expanduser()
        try:
            in_resolved = in_path.resolve()
            out_resolved = out_path.resolve()
        except OSError:
            in_resolved, out_resolved = in_path, out_path
        if out_resolved == in_resolved:
            messagebox.showerror(
                APP_TITLE,
                "Output folder cannot be the same as the input folder.",
            )
            return None
        # Refuse nested input/output: either direction creates a feedback
        # loop — output inside input means the walker (in batch mode) will
        # see prior outputs as candidate Ref IDs; input inside output means
        # the pipeline is asked to copy source TIFs over its own output tree.
        if out_resolved.is_relative_to(in_resolved):
            messagebox.showerror(
                APP_TITLE,
                f"Output folder is inside the input folder:\n  input:  {in_resolved}\n"
                f"  output: {out_resolved}\n\n"
                f"Pick an output folder that's outside the input.",
            )
            return None
        if in_resolved.is_relative_to(out_resolved):
            messagebox.showerror(
                APP_TITLE,
                f"Input folder is inside the output folder:\n  input:  {in_resolved}\n"
                f"  output: {out_resolved}\n\n"
                f"Pick an output folder that doesn't contain the input.",
            )
            return None
        try:
            adv = parse_advanced(self._advanced_raw())
        except SettingsError as e:
            messagebox.showerror(
                APP_TITLE, f"{e}\n\n(Tools ▸ Advanced settings…)",
                parent=self._adv_win or self.root)
            return None

        return JobConfig(
            input_dir=in_path,
            output_dir=out_path,
            recurse=self.var_recurse.get(),
            write_mets=self.var_mets.get(),
            write_plaintext=self.var_plaintext.get(),
            engine=self.var_engine.get(),
            **adv,
        )

    def _persist_settings(self, cfg: JobConfig) -> None:
        """Snapshot the job's basic + advanced configuration to disk so it
        comes back on the next launch."""
        changes: dict[str, object] = {
            "recurse": cfg.recurse,
            "engine": cfg.engine,
            "write_mets": cfg.write_mets,
            "write_plaintext": cfg.write_plaintext,
        }
        changes.update(advanced_settings({
            "tesseract_path": cfg.tesseract_path,
            "preprocess_config": cfg.preprocess_config,
            "debug_preprocessed_dir": cfg.debug_preprocessed_dir,
            "ocr_options": cfg.ocr_options,
            "glmocr_endpoint": cfg.glmocr_endpoint,
            "glmocr_model": cfg.glmocr_model,
            "glmocr_timeout": cfg.glmocr_timeout,
            "with_checksums": cfg.with_checksums,
        }))
        self._update_settings(changes)

    # ---------- Worker (off the main thread) -----------------------------------

    def _worker_main(self, cfg: JobConfig) -> None:
        from .pipeline import (
            PageProgressLogger,
            failed_ref_result,
            process_ref_id,
            skipped_ref_result,
        )

        # Discover work first so we know which engines we actually need.
        # Building GLMOCR runs an endpoint check; skip it when no Ref ID needs it.
        skipped: list[SkippedRefId] = []
        if cfg.recurse:
            items, skipped = discover_collection(cfg.input_dir)
            for sk in skipped:
                logger.error("SKIPPED %s: %s", sk.ref_id, sk.reason)
            if not items and not skipped:
                logger.error("No Ref IDs with TIFs found inside %s", cfg.input_dir)
                self._signal_done(success=False)
                return
            total_pages = sum(i.page_count for i in items)
            logger.info("Discovered %d Ref ID(s), %d page(s) total.",
                        len(items), total_pages)
        else:
            single = discover_ref_id(cfg.input_dir)
            if single is None:
                logger.error("No TIFs or PDFs found in %s", cfg.input_dir)
                self._signal_done(success=False)
                return
            if isinstance(single, SkippedRefId):
                logger.error("%s: %s", single.ref_id, single.reason)
                self._signal_done(success=False)
                return
            items = [single]
            logger.info("Single Ref ID: %s (%d page(s))",
                        single.ref_id, single.page_count)

        try:
            engine = self._build_engine(cfg.engine, cfg)
        except Exception as e:
            logger.error("Engine setup failed: %s", e)
            self._signal_done(success=False)
            return

        self._set_progress(0, len(items))
        results: list[RefIdResult] = []
        cancelled = False

        # Page-level progress across all Ref IDs: log at 1%, 5%, then every 5%.
        total_pages = sum(i.page_count for i in items)
        progress = PageProgressLogger(total_pages)

        for i, inputs in enumerate(items, start=1):
            if self._cancel_event.is_set():
                logger.warning("Cancelled before %s", inputs.ref_id)
                cancelled = True
                break
            try:
                r = process_ref_id(
                    inputs, cfg.output_dir,
                    engine=engine,
                    preprocess_config=cfg.preprocess_config,
                    debug_preprocessed_dir=cfg.debug_preprocessed_dir,
                    with_checksums=cfg.with_checksums,
                    write_mets_file=cfg.write_mets,
                    write_plaintext=cfg.write_plaintext,
                    on_page_done=progress.tick,
                )
                results.append(r)
            except Exception as e:
                logger.error("Unhandled error on %s:\n%s",
                             inputs.ref_id, traceback.format_exc())
                # Mirror the CLI: a crashed Ref ID must appear in the final
                # accounting, not just scroll by in the log.
                results.append(failed_ref_result(inputs, cfg.output_dir, e))
            self._set_progress(i, len(items))

        for sk in skipped:
            results.append(skipped_ref_result(sk, cfg.output_dir))
        ok = sum(1 for r in results if r.ok)
        failed_pages = sum(r.failed_page_count for r in results)
        n_total = len(items) + len(skipped)
        verb = "Cancelled" if cancelled else "Done"
        logger.info("%s: %d/%d Ref ID(s) succeeded, %d failed page(s).",
                    verb, ok, n_total, failed_pages)
        self._signal_done(success=(not cancelled and ok == n_total))

    def _build_engine(self, name: str, cfg: JobConfig) -> OCREngine:
        """GUI face of ``ocr.build_engine``: shared construction + startup
        checks; failures become RuntimeError for the worker's error path."""
        from .glmocr import GLMOCRError, resolve_endpoint
        from .ocr import TesseractNotFoundError, build_engine

        try:
            built = build_engine(
                name,
                tesseract_path=cfg.tesseract_path,
                ocr_options=cfg.ocr_options,
                glmocr_endpoint=cfg.glmocr_endpoint,
                glmocr_model=cfg.glmocr_model,
                glmocr_timeout=cfg.glmocr_timeout,
            )
        except TesseractNotFoundError as e:
            raise RuntimeError(str(e))
        except GLMOCRError as e:
            raise RuntimeError(
                f"GLMOCR endpoint check failed: {e}\n\n"
                f"Make sure Ollama is running at "
                f"{resolve_endpoint(cfg.glmocr_endpoint)} and the "
                f"{cfg.glmocr_model} model is pulled."
            )
        except ValueError as e:
            raise RuntimeError(str(e))
        if built.kind == "tesseract":
            logger.info(
                "Tesseract: %s (%s) lang=%s psm=%d oem=%d",
                built.tesseract_binary, built.tesseract_version,
                cfg.ocr_options.language, cfg.ocr_options.psm, cfg.ocr_options.oem,
            )
        else:
            gc = built.glmocr_config
            logger.info("GLMOCR: %s (model %s, timeout %ds)",
                        gc.endpoint, gc.model, gc.timeout_s)
        return built.engine

    def _set_progress(self, current: int, total: int) -> None:
        self._progress_queue.put((current, total))

    def _signal_done(self, *, success: bool) -> None:
        # Sentinel: current=-1; total carries the success flag.
        self._progress_queue.put((-1, 1 if success else 0))

    # ---------- Main-thread event pump -----------------------------------------

    def _drain_queues(self) -> None:
        # Bound the per-tick log drain so a noisy run can't starve the UI.
        for _ in range(500):
            try:
                level, msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(level, msg)

        try:
            while True:
                current, total = self._progress_queue.get_nowait()
                if current == -1:
                    success = bool(total)
                    self.btn_run.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    self.var_status.set("Done." if success else "Finished with errors.")
                    self.root.title(APP_TITLE)
                else:
                    self.progress.configure(maximum=max(total, 1), value=current)
                    if total > 0:
                        self.var_status.set(f"Processed {current}/{total} Ref ID(s).")
                        # The log lives on the Run tab; the title keeps a
                        # corrector working on the Review tab informed.
                        self.root.title(f"{APP_TITLE} — OCR {current}/{total} Ref ID(s)")
        except queue.Empty:
            pass

        self._poll_after = self.root.after(self.POLL_MS, self._drain_queues)

    def _append_log(self, level: str, msg: str) -> None:
        self.log_text.configure(state="normal")
        tag = level if level in ("ERROR", "WARNING", "INFO", "DEBUG") else "INFO"
        self.log_text.insert("end", msg + "\n", tag)
        # Trim retention: a multi-hour batch logs tens of thousands of lines
        # and Tk's Text widget gets sluggish past ~10^5. Keep the newest ~5000.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 6000:
            self.log_text.delete("1.0", f"{line_count - 5000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = tk.Tk()
    AltoOcrApp(root)  # applies the saved theme (theme.apply)
    root.mainloop()


if __name__ == "__main__":
    main()
