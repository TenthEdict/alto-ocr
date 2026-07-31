"""Tkinter desktop GUI for alto_ocr.

A thin wrapper over the existing pipeline. Folder picker, four toggles
(METS, plaintext, recurse-subfolders, engine type), live log + progress.

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
from typing import Optional

from .glmocr import GLMOCRConfig, GLMOCRError, check_endpoint, resolve_endpoint
from .line_detect import DetectParams
from .ocr import (
    GLMOCREngine,
    OCREngine,
    OCROptions,
    TesseractEngine,
    TesseractNotFoundError,
    check_tesseract_version,
    find_tesseract,
)
from .pipeline import (
    PageProgressLogger,
    RefIdResult,
    failed_ref_result,
    process_ref_id,
    skipped_ref_result,
)
from .preprocess import PreprocessConfig
from .review import ReviewFrame
from .walker import SkippedRefId, discover_collection, discover_ref_id

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
    glmocr_model: str = "glm-ocr:q8_0"
    glmocr_timeout: int = 120


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _migrate_settings(s: dict) -> dict:
    """One-shot migrations for changed defaults.

    v0.8.8 flipped the binarize default OFF (adaptive thresholding
    measurably hurt Tesseract 5 on every fixture + pilot collection).
    Saved GUI settings outrank dataclass defaults, so a pre-0.8.8 saved
    ``do_binarize: true`` — usually just the old default echoed back by a
    save — would silently keep binarization on forever. Drop the key once;
    the marker means any value present afterwards was chosen by the user
    under the new default and is kept. Pure function of the dict so it
    stays testable without Tk.
    """
    if not s.get("_migrated_binarize_default_off"):
        s.pop("do_binarize", None)
        s["_migrated_binarize_default_off"] = True
    return s


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
        self.root.geometry("820x720")
        self.root.minsize(720, 720)

        # All settings — basic and advanced — restore from the persisted
        # JSON on launch so users keep their preferred recipe across runs.
        self._settings = _migrate_settings(_load_settings())
        s = self._settings
        pp = PreprocessConfig()  # source of truth for preprocessing defaults
        oo = OCROptions()        # source of truth for Tesseract option defaults
        gc = GLMOCRConfig()      # source of truth for GLMOCR defaults

        # ----- Tk variables — model state behind the widgets. -----
        # Run-tab basics
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        # Once the user manually overrides the output, stop auto-updating from input.
        self._output_overridden = False
        self.var_recurse = tk.BooleanVar(value=bool(s.get("recurse", False)))
        self.var_mets = tk.BooleanVar(value=bool(s.get("write_mets", True)))
        self.var_plaintext = tk.BooleanVar(value=bool(s.get("write_plaintext", True)))
        self.var_engine = tk.StringVar(value=str(s.get("engine", "tesseract")))
        self.var_tesseract = tk.StringVar(value=str(s.get("tesseract_path", "")))
        self.var_status = tk.StringVar(value="Ready.")

        # Advanced — Preprocessing
        self.var_min_dpi = tk.IntVar(value=int(s.get("min_dpi", pp.min_dpi)))
        self.var_do_deskew = tk.BooleanVar(value=bool(s.get("do_deskew", pp.do_deskew)))
        self.var_do_denoise = tk.BooleanVar(value=bool(s.get("do_denoise", pp.do_denoise)))
        self.var_do_clahe = tk.BooleanVar(value=bool(s.get("do_clahe", pp.do_clahe)))
        self.var_do_binarize = tk.BooleanVar(value=bool(s.get("do_binarize", pp.do_binarize)))
        self.var_do_border_crop = tk.BooleanVar(
            value=bool(s.get("do_border_crop", pp.do_border_crop)))
        self.var_debug_preproc = tk.StringVar(value=str(s.get("debug_preprocessed", "")))

        # Advanced — Tesseract OCR options
        self.var_ocr_lang = tk.StringVar(value=str(s.get("ocr_lang", oo.language)))
        self.var_psm = tk.IntVar(value=int(s.get("psm", oo.psm)))
        self.var_oem = tk.IntVar(value=int(s.get("oem", oo.oem)))

        # Advanced — GLMOCR
        self.var_glmocr_endpoint = tk.StringVar(value=str(s.get("glmocr_endpoint", "")))
        self.var_glmocr_model = tk.StringVar(value=str(s.get("glmocr_model", gc.model)))
        self.var_glmocr_timeout = tk.IntVar(value=int(s.get("glmocr_timeout", gc.timeout_s)))

        # Advanced — METS
        self.var_with_checksums = tk.BooleanVar(value=bool(s.get("with_checksums", True)))

        # Worker bookkeeping.
        self._worker: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._progress_queue: "queue.Queue[tuple[int, int]]" = queue.Queue()

        # Auto-fill output folder when the input changes (until overridden).
        self.var_input.trace_add("write", self._on_input_changed)

        self._build_ui()
        self._install_logging()
        self.root.after(self.POLL_MS, self._drain_queues)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        # Grid layout: PanedWindow (Notebook + Log, draggable sash between)
        # gets all extra space; actions and progress are persistent chrome
        # at the bottom and are NEVER inside the paned region — so the
        # Run/Cancel buttons can't get clipped no matter how the user
        # resizes the sash or the window.
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)  # paned (resizable area)
        outer.rowconfigure(1, weight=0)  # actions
        outer.rowconfigure(2, weight=0)  # progress

        # Vertical PanedWindow: Notebook on top, Log on bottom. Using
        # tk.PanedWindow (not ttk) because only the legacy widget exposes
        # per-pane minsize, which keeps the log from collapsing to zero.
        paned = tk.PanedWindow(
            outer, orient="vertical",
            sashwidth=6, sashrelief="raised", borderwidth=0,
        )
        # Match the sash background to the ttk theme so it blends in;
        # ttk widgets don't expose `background` via cget, so query the
        # style instead. Skip silently if the theme doesn't define one.
        theme_bg = ttk.Style().lookup("TFrame", "background")
        if theme_bg:
            paned.configure(background=theme_bg)
        paned.grid(row=0, column=0, sticky="nsew")

        nb = ttk.Notebook(paned)
        tab_run = ttk.Frame(nb, padding=8)
        tab_advanced = ttk.Frame(nb, padding=8)
        nb.add(tab_run, text="Run")
        nb.add(tab_advanced, text="Advanced settings")
        self._build_run_tab(tab_run)
        self._build_advanced_tab(tab_advanced)
        self.review = ReviewFrame(nb, self._settings,
                                  lambda: _save_settings(self._settings))
        nb.add(self.review, text="Review")
        paned.add(nb, minsize=240, stretch="never")
        self._paned = paned
        self._nb = nb
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_frame = ttk.LabelFrame(paned, text="Log", padding=4)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame, wrap="none", height=10,
            background="#101418", foreground="#d8d8d8",
            insertbackground="#d8d8d8", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll_y.set, state="disabled")
        self.log_text.tag_configure("ERROR", foreground="#ff6e6e")
        self.log_text.tag_configure("WARNING", foreground="#ffd166")
        self.log_text.tag_configure("INFO", foreground="#d8d8d8")
        self.log_text.tag_configure("DEBUG", foreground="#7d8896")
        paned.add(log_frame, minsize=160, stretch="always")

        # Persistent chrome below the paned area.
        actions = ttk.Frame(outer)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        self.btn_run = ttk.Button(actions, text="Run OCR", command=self._on_run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="Cancel",
                                     command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.btn_clear = ttk.Button(actions, text="Clear log",
                                    command=self._clear_log)
        self.btn_clear.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.var_status, foreground="#666").pack(
            side="right")

        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.grid(row=2, column=0, sticky="ew", pady=(0, 6))

    def _build_run_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)

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
        ttk.Button(tab, text="Override…", command=self._browse_output).grid(
            row=1, column=2, padx=(8, 0), pady=(0, 6))

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
        engine_frame.columnconfigure(1, weight=1)
        radio_row = ttk.Frame(engine_frame)
        radio_row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(radio_row, text="Tesseract (default — word confidences)",
                        variable=self.var_engine, value="tesseract").pack(
            side="left", padx=(0, 16))
        ttk.Radiobutton(radio_row, text="GLMOCR (handwriting)",
                        variable=self.var_engine, value="glmocr").pack(side="left")
        # Optional Tesseract binary override — leave blank to auto-detect.
        ttk.Label(engine_frame, text="Tesseract path:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(engine_frame, textvariable=self.var_tesseract).grid(
            row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(engine_frame, text="Browse…",
                   command=self._browse_tesseract).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Label(
            engine_frame,
            text="Only used when Tesseract is selected. Leave empty to "
                 "auto-detect (PATH, TESSERACT_PATH, or standard install).",
            foreground="#888",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _build_advanced_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)

        # ----- Preprocessing -----
        pp = ttk.LabelFrame(tab, text="Preprocessing", padding=8)
        pp.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        pp.columnconfigure(1, weight=1)

        ttk.Label(pp, text="Min DPI:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Spinbox(
            pp, from_=72, to=2400, increment=50,
            textvariable=self.var_min_dpi, width=8,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            pp, text="Upscale low-DPI scans to at least this DPI before OCR.",
            foreground="#888",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        stages = ttk.Frame(pp)
        stages.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(stages, text="Deskew",
                        variable=self.var_do_deskew).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="Denoise",
                        variable=self.var_do_denoise).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="CLAHE contrast",
                        variable=self.var_do_clahe).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="Binarize",
                        variable=self.var_do_binarize).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(stages, text="Border crop",
                        variable=self.var_do_border_crop).pack(side="left")

        ttk.Label(pp, text="Save preprocessed images to:").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(pp, textvariable=self.var_debug_preproc).grid(
            row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(pp, text="Browse…", command=self._browse_debug_preproc).grid(
            row=2, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Label(
            pp, text="Leave empty to skip; useful for debugging OCR accuracy.",
            foreground="#888",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # ----- Tesseract OCR options -----
        tess = ttk.LabelFrame(tab, text="Tesseract OCR", padding=8)
        tess.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        tess.columnconfigure(1, weight=0)

        ttk.Label(tess, text="Language:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(tess, textvariable=self.var_ocr_lang, width=14).grid(
            row=0, column=1, sticky="w")
        ttk.Label(
            tess, text="e.g. 'eng', 'fra', 'eng+lat'.",
            foreground="#888",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(tess, text="PSM (page segmentation):").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Spinbox(
            tess, from_=0, to=13, increment=1,
            textvariable=self.var_psm, width=6,
        ).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            tess, text="3=auto (default), 4=single column, 6=uniform block.",
            foreground="#888",
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(tess, text="OEM (engine mode):").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Spinbox(
            tess, from_=0, to=3, increment=1,
            textvariable=self.var_oem, width=6,
        ).grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            tess, text="3=default LSTM (recommended).",
            foreground="#888",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))

        # ----- GLMOCR (handwritten) -----
        gl = ttk.LabelFrame(tab, text="GLMOCR (handwritten)", padding=8)
        gl.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        gl.columnconfigure(1, weight=1)

        ttk.Label(gl, text="Endpoint:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(gl, textvariable=self.var_glmocr_endpoint).grid(
            row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(
            gl, text="Leave empty to use OLLAMA_HOST or the built-in default.",
            foreground="#888",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Label(gl, text="Model:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(gl, textvariable=self.var_glmocr_model, width=24).grid(
            row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            gl, text="Ollama model tag, e.g. 'glm-ocr:q8_0'.",
            foreground="#888",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(gl, text="Timeout (sec):").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Spinbox(
            gl, from_=10, to=900, increment=10,
            textvariable=self.var_glmocr_timeout, width=8,
        ).grid(row=3, column=1, sticky="w", pady=(6, 0))

        # ----- METS -----
        mets = ttk.LabelFrame(tab, text="METS", padding=8)
        mets.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            mets, text="Compute MD5 checksums (slower on large objects)",
            variable=self.var_with_checksums,
        ).pack(side="left")

        # Reset button
        reset_row = ttk.Frame(tab)
        reset_row.grid(row=4, column=0, sticky="ew")
        ttk.Button(reset_row, text="Reset advanced settings to defaults",
                   command=self._on_reset_advanced).pack(side="right")

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
            title="Select tesseract binary",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if f:
            self.var_tesseract.set(f)

    def _browse_debug_preproc(self) -> None:
        d = filedialog.askdirectory(
            title="Select folder for preprocessed-image dumps", mustexist=False,
        )
        if d:
            self.var_debug_preproc.set(d)

    def _on_reset_advanced(self) -> None:
        """Restore every Advanced-tab field to its dataclass default."""
        pp = PreprocessConfig()
        oo = OCROptions()
        gc = GLMOCRConfig()
        self.var_min_dpi.set(pp.min_dpi)
        self.var_do_deskew.set(pp.do_deskew)
        self.var_do_denoise.set(pp.do_denoise)
        self.var_do_clahe.set(pp.do_clahe)
        self.var_do_binarize.set(pp.do_binarize)
        self.var_do_border_crop.set(pp.do_border_crop)
        self.var_debug_preproc.set("")
        self.var_ocr_lang.set(oo.language)
        self.var_psm.set(oo.psm)
        self.var_oem.set(oo.oem)
        self.var_glmocr_endpoint.set("")
        self.var_glmocr_model.set(gc.model)
        self.var_glmocr_timeout.set(gc.timeout_s)
        self.var_with_checksums.set(True)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

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
        pc = cfg.preprocess_config
        logger.info(
            "Starting: input=%s output=%s recurse=%s mets=%s plaintext=%s "
            "checksums=%s engine=%s lang=%s psm=%d oem=%d min_dpi=%d "
            "stages=[deskew=%s denoise=%s clahe=%s binarize=%s border=%s]",
            cfg.input_dir, cfg.output_dir, cfg.recurse,
            cfg.write_mets, cfg.write_plaintext, cfg.with_checksums,
            cfg.engine, cfg.ocr_options.language, cfg.ocr_options.psm,
            cfg.ocr_options.oem, pc.min_dpi,
            pc.do_deskew, pc.do_denoise, pc.do_clahe, pc.do_binarize, pc.do_border_crop,
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

    def _on_tab_changed(self, _event: object) -> None:
        # The Review tab needs canvas height, but the default layout gives
        # extra space to the log pane. On switching to Review with a short
        # notebook pane, drop the sash so the notebook gets ~72% of the
        # paned area; the user can still drag it back.
        try:
            if (self._nb.select() == str(self.review)
                    and self._nb.winfo_height() < 500):
                self._paned.sash_place(0, 0, int(self._paned.winfo_height() * 0.72))
        except tk.TclError:
            pass

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
        tess_str = self.var_tesseract.get().strip()
        tess_path: Optional[Path] = Path(tess_str).expanduser() if tess_str else None
        if tess_path is not None and not tess_path.is_file():
            messagebox.showerror(
                APP_TITLE,
                f"Tesseract path does not point at a file:\n{tess_path}\n\n"
                f"Leave the field empty to auto-detect, or pick the binary with Browse…",
            )
            return None

        # ----- Advanced tab -----
        try:
            min_dpi = int(self.var_min_dpi.get())
            psm = int(self.var_psm.get())
            oem = int(self.var_oem.get())
            glmocr_timeout = int(self.var_glmocr_timeout.get())
        except (tk.TclError, ValueError):
            messagebox.showerror(
                APP_TITLE,
                "Advanced settings: Min DPI / PSM / OEM / GLMOCR timeout must be integers.",
            )
            return None
        if min_dpi <= 0:
            messagebox.showerror(APP_TITLE, "Min DPI must be positive.")
            return None
        if not (0 <= psm <= 13):
            messagebox.showerror(APP_TITLE, "PSM must be between 0 and 13.")
            return None
        if not (0 <= oem <= 3):
            messagebox.showerror(APP_TITLE, "OEM must be between 0 and 3.")
            return None
        if glmocr_timeout <= 0:
            messagebox.showerror(APP_TITLE, "GLMOCR timeout must be positive.")
            return None

        ocr_lang = self.var_ocr_lang.get().strip() or "eng"
        glmocr_model = self.var_glmocr_model.get().strip() or GLMOCRConfig().model
        glmocr_endpoint_str = self.var_glmocr_endpoint.get().strip()

        debug_str = self.var_debug_preproc.get().strip()
        debug_dir = Path(debug_str).expanduser() if debug_str else None

        preprocess_config = PreprocessConfig(
            min_dpi=min_dpi,
            do_deskew=self.var_do_deskew.get(),
            do_denoise=self.var_do_denoise.get(),
            do_clahe=self.var_do_clahe.get(),
            do_binarize=self.var_do_binarize.get(),
            do_border_crop=self.var_do_border_crop.get(),
        )
        ocr_options = OCROptions(language=ocr_lang, psm=psm, oem=oem)

        return JobConfig(
            input_dir=in_path,
            output_dir=out_path,
            recurse=self.var_recurse.get(),
            write_mets=self.var_mets.get(),
            write_plaintext=self.var_plaintext.get(),
            with_checksums=self.var_with_checksums.get(),
            engine=self.var_engine.get(),
            tesseract_path=tess_path,
            preprocess_config=preprocess_config,
            debug_preprocessed_dir=debug_dir,
            ocr_options=ocr_options,
            glmocr_endpoint=glmocr_endpoint_str or None,
            glmocr_model=glmocr_model,
            glmocr_timeout=glmocr_timeout,
        )

    def _persist_settings(self, cfg: JobConfig) -> None:
        """Snapshot the job's basic + advanced configuration to disk so it
        comes back on the next launch.

        The Tesseract path keeps its existing semantics: only saved when the
        user supplied an explicit path; auto-detected paths are re-derived
        each launch so a Tesseract reinstall doesn't strand us on a stale
        location.
        """
        s = dict(self._settings)
        # Run-tab basics
        s["recurse"] = cfg.recurse
        s["engine"] = cfg.engine
        s["write_mets"] = cfg.write_mets
        s["write_plaintext"] = cfg.write_plaintext
        # Advanced — Preprocessing
        pc = cfg.preprocess_config
        s["min_dpi"] = pc.min_dpi
        s["do_deskew"] = pc.do_deskew
        s["do_denoise"] = pc.do_denoise
        s["do_clahe"] = pc.do_clahe
        s["do_binarize"] = pc.do_binarize
        s["do_border_crop"] = pc.do_border_crop
        s["debug_preprocessed"] = (
            str(cfg.debug_preprocessed_dir) if cfg.debug_preprocessed_dir else ""
        )
        # Advanced — Tesseract OCR
        s["ocr_lang"] = cfg.ocr_options.language
        s["psm"] = cfg.ocr_options.psm
        s["oem"] = cfg.ocr_options.oem
        # Advanced — GLMOCR
        s["glmocr_endpoint"] = cfg.glmocr_endpoint or ""
        s["glmocr_model"] = cfg.glmocr_model
        s["glmocr_timeout"] = cfg.glmocr_timeout
        # Advanced — METS
        s["with_checksums"] = cfg.with_checksums
        # Tesseract path — only if explicit.
        if cfg.tesseract_path:
            s["tesseract_path"] = str(cfg.tesseract_path)
        else:
            s.pop("tesseract_path", None)

        if s != self._settings:
            # Update in place: the Review tab holds a reference to this dict
            # (its keys ride along in `s` via the dict(...) copy above).
            self._settings.clear()
            self._settings.update(s)
            _save_settings(self._settings)

    # ---------- Worker (off the main thread) -----------------------------------

    def _worker_main(self, cfg: JobConfig) -> None:
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
        if name == "tesseract":
            try:
                binary = find_tesseract(cfg.tesseract_path)
            except TesseractNotFoundError as e:
                raise RuntimeError(str(e))
            logger.info(
                "Tesseract: %s (%s) lang=%s psm=%d oem=%d",
                binary, check_tesseract_version(binary),
                cfg.ocr_options.language, cfg.ocr_options.psm, cfg.ocr_options.oem,
            )
            return TesseractEngine(binary=binary, options=cfg.ocr_options)
        if name == "glmocr":
            endpoint = resolve_endpoint(cfg.glmocr_endpoint)
            config = GLMOCRConfig(
                endpoint=endpoint,
                model=cfg.glmocr_model,
                timeout_s=cfg.glmocr_timeout,
            )
            try:
                check_endpoint(config)
            except GLMOCRError as e:
                raise RuntimeError(
                    f"GLMOCR endpoint check failed: {e}\n\n"
                    f"Make sure Ollama is running at {endpoint} and the "
                    f"{config.model} model is pulled."
                )
            logger.info("GLMOCR: %s (model %s, timeout %ds)",
                        endpoint, config.model, config.timeout_s)
            return GLMOCREngine(config=config, detect_params=DetectParams())
        raise RuntimeError(f"Unknown engine: {name!r}")

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
                else:
                    self.progress.configure(maximum=max(total, 1), value=current)
                    if total > 0:
                        self.var_status.set(f"Processed {current}/{total} Ref ID(s).")
        except queue.Empty:
            pass

        self.root.after(self.POLL_MS, self._drain_queues)

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
    style = ttk.Style()
    for preferred in ("vista", "winnative", "clam", "default"):
        if preferred in style.theme_names():
            try:
                style.theme_use(preferred)
                break
            except tk.TclError:
                continue
    AltoOcrApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
