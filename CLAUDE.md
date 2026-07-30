# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

OCR digitized archival page-scans into **METS/ALTO XML + plain-text transcripts** for a public digital-archive interface. Downstream consumers expect ALTO so they can render highlighted hit boxes over the page images.

The tool must work across **all of the institution's digitized collections** — `MSP*`, `MSA*`, `UA*`, and any future prefix. Do **not** hard-code anything specific to one collection: no folder-name prefixes, no filename regexes tied to a particular collection's conventions, no path assumptions. The walker takes "a directory containing per-object subdirectories" and produces one output object per subdirectory; that's the only structural contract.

Source inputs are TIFs in the common case, with **PDFs supported as a secondary input format**. Some collections contain born-digital or scanned PDFs alongside the canonical TIFs; the pipeline rasterizes each PDF page to a temporary TIF up front so the rest of the pipeline stays TIF-only. See "PDF rasterization" below.

The working tool lives under `src/alto_ocr/` (CLI + Tkinter GUI, packaged for distribution — bump `__version__` in `src/alto_ocr/__init__.py` before rebuilds). Generation is working and validated; the project's current phase is the **remediation workflow** — see "Remediation workflow" below. `ePubs_Main/` is a cloned sibling project kept here purely as a **pattern reference** — see "Borrowing from ePubs_Main" below.

## Cross-platform requirement (load-bearing)

The tool will be run on **Windows and Linux** (the current maintainer uses WSL; the eventual successor may be on native Windows). Concretely:

- **Locate Tesseract via `shutil.which("tesseract")`**, not a hard-coded `/usr/bin/tesseract`. Accept an override via `--tesseract-path` CLI flag and/or `TESSERACT_PATH` env var so Windows users with a non-PATH install (e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe`) can point at it. When the Tesseract engine is selected and not found, fail loudly at engine-build time with an actionable message; don't probe at startup since GLMOCR runs need not have Tesseract installed.
- All Python deps must install cleanly on Windows via pip with no system-library prerequisites. Prefer `opencv-python-headless` over `opencv-python` (no GUI deps, smaller wheel, identical API for our needs).
- Always use `pathlib.Path` for paths; never join with `"/"` literals or split on `os.sep` assumptions.
- Invoke Tesseract with `subprocess.run([...], check=True)` passing a **list of args** (not `shell=True`), which handles spaces in paths correctly on both OSes. Source TIF filenames frequently contain spaces — this matters.
- Match TIF extensions case-insensitively: accept `.tif`, `.tiff`, `.TIF`, `.TIFF`. Windows filesystems are case-insensitive but the actual on-disk casing varies across collections.
- Filter `Thumbs.db` and `Thumbs.db:encryptable` from input directories (Windows generates these continuously; they appear all over the test data).
- No shell pipelines, no `xargs`, no `find` — pure Python.

## Output contract (load-bearing — don't drift from this)

For each input object subdirectory (Ref ID), produce two sibling folders under `<output_root>/<RefID>/` whose filename stems match exactly across folders:

```
<output_root>/<RefID>/
├── ALTO/   image001.xml image002.xml …
└── Text/   image001.txt image002.txt …
```

Plus **one** `<RefID>.mets.xml` per Ref ID (METS scope = per object, not per page) whose `structMap` references both file groups.

**The source TIFs are NOT copied into the output.** Every institution collection holds canonical preservation TIFs at `preservation-files/preservation/<RefID>/`; the OCR output is a sibling at `documentation/ALTOS-Plain Text/<RefID>/`. Copying TIFs into our output would just duplicate them on storage-constrained network drives, and the LibNova upload workflow pairs the two trees by Ref ID externally — it does not resolve TIFs through our METS. Treat the absence of `TIFF/` in the output as a deliberate, fixed part of the contract.

Decisions already made (do not re-litigate without asking):
- **Preserve** source TIF filename stems. `ALTO/<stem>.xml` and `Text/<stem>.txt` share exactly the source filename's stem. The walker fails-fast on stem collisions within one Ref ID since ALTO/Text have no extension to disambiguate.
- For **PDF sources**, the rasterized pages get the stems `<pdfstem>_page001`, `<pdfstem>_page002`, … (three-digit zero-padding) and those become the ALTO/Text stems. The stem-collision check runs against these *predicted* output stems, so it catches e.g. a standalone `Foo_page001.tif` clashing with rasterized page 1 of `Foo.pdf`, but does not false-positive on `Foo.pdf` + `Foo.tif` (those produce different stems).
- Always produce ALTO + TXT for every input page, even when the page has no detectable text. Blank-page consistency matters for upload packaging; Tesseract's empty ALTO/TXT outputs satisfy this, and the pipeline asserts the files exist post-OCR. ALTO/Text page counts stay aligned with the input page count (sum of TIF + expanded-PDF pages).
- METS-per-Ref-ID is on by default, opt-out via `--no-mets`. The institution's planned LibNova workflow may not consume METS; keep the toggle exposed without changing the default until that's confirmed.
- The `reformat` command takes `--source-tiff-dir` (typically a preservation directory) for per-page DPI lookup since no TIFs live in the output tree. Falls back to `--min-dpi` (300) when no source dir is provided.
- XML parsing of pipeline files (ALTO, METS) goes through `alto_format.hardened_parser()` — DTD entity resolution off (output trees live on shared drives and round-trip through external editors; we never use DTD entities). Route any new `etree.parse` of pipeline files through it. `repackage` preserves the METS header's original `CREATEDATE` and stamps `LASTMODDATE` on each rebuild; triage CSV cells sourced from filenames are neutralized against Excel formula injection.
- Canonical outputs (ALTO, Text, METS — including review-GUI saves, failure stubs, and `repackage` rewrites) are written **atomically** via `_util.atomic_write_text/_bytes` (temp file + `os.replace` in the destination directory): a crash or network hiccup mid-write can never truncate an existing file. Load-bearing for hand-corrected ALTO, where truncation destroys human labor. Route any new output-file writes through these helpers; byte-parity with the old `Path.write_text` behavior (incl. Windows newline translation) is locked in by `tests/test_atomic_write.py`.

## Image preprocessing for OCR accuracy (load-bearing)

`ePubs_Main` delegates all image preprocessing to `ocrmypdf`'s internal pipeline and has **no standalone preprocessing code** to fork. Since we're feeding raw TIFs directly to Tesseract, we must build the preprocessing pipeline ourselves. Tesseract's accuracy is dominated by input image quality far more than by any tuning of `--psm` / `--oem`, so this stage is where the wins are.

Use **OpenCV (`opencv-python-headless`)** as the workhorse — best deskew/threshold/morphology coverage, pip-installable on both OSes with no system prerequisites. Pillow for I/O and DPI metadata. Default pipeline, applied in order:

1. **Load + grayscale** — read with `cv2.imread(..., cv2.IMREAD_UNCHANGED)`, convert color → grayscale. Skip if already 1-bit / grayscale.
2. **DPI sanity** — read DPI from the TIF tags via Pillow. If < 300, **upscale** (Lanczos) so Tesseract sees ≥ 300 effective DPI. Pass the corrected DPI to Tesseract via `--dpi`. A tag below `_MIN_PLAUSIBLE_DPI` (50) is metadata garbage (OpenCV-written TIFs default to 1; some scanners write 0) and is treated as *unknown* — no upscale — because trusting it makes the scale factor `min_dpi/tag` (300× for a tag of 1): a gigapixel resize that stalls the batch with no exception for per-page isolation to catch. Found live 2026-07-30; regression-tested in `tests/test_preprocess_dpi.py`.
3. **Deskew** — estimate skew angle from a Hough-lines or minimum-area-rect approach on a binarized copy; rotate the grayscale image with `cv2.warpAffine` (white border fill). **Frame-changing — force-disabled by both engines' `adjust_preprocess`; see "Coordinate-frame integrity" below.** (The stage remains in `preprocess.py` for any future engine that inverse-transforms its coordinates.)
4. **Denoise** — light bilateral filter (`cv2.bilateralFilter`) on grayscale to smooth scan noise without blurring stroke edges. Skip on bitonal input.
5. **Contrast normalization** — CLAHE (`cv2.createCLAHE`) for faded or unevenly-lit scans. Modest defaults (`clipLimit=2.0`, `tileGridSize=(8,8)`).
6. **Binarization** — adaptive threshold (`cv2.adaptiveThreshold` with Gaussian). **OFF by default since 2026-07-30 — do not flip it back without re-measuring.** Tesseract 5's LSTM engine does its own thresholding and scores materially higher on grayscale than on anything we hand it pre-binarized. Measured on three unrelated collections (mean WC, ON → OFF): ZeffPapers/`MSP00074` 0.828 → 0.909, `MSA00005` 0.583 → 0.781 and 0.453 → 0.945, `MSA00018` 0.803 → 0.919; pages needing review fell 28 → 6, 9 → 3, 25 → 6. The mechanism is **speckle**: on toned or grainy paper the adaptive window flips background grain to ink, which Tesseract emits as stray punctuation (`.` `|` `:`) and glues onto real words (`the.`, `of.`). Token-level diffing confirmed the words that vanish when it's off are exactly those artifacts, while real words are *gained* — so a lower total word count after turning it off is the noise leaving, not text lost. Opt in per collection with `--binarize` (GUI: the Binarize checkbox) only if triage shows it helping. A persisted GUI preference for the old default is retired once by `gui._migrate_settings`. Locked by `tests/test_preprocess_defaults.py`.
7. **Border crop** — detect and trim solid-black scanning borders that confuse Tesseract's layout analysis (large connected components touching the edge). **Frame-changing — force-disabled by both engines, same as deskew.**

**Coordinate-frame integrity (load-bearing).** ALTO box coordinates must land on the **original** image: the public archive draws hit boxes, and the Review tab draws word boxes/snippets, over the untouched preservation scan, scaling by image-px / `Page`-dims. Only the *uniform* DPI upscale cancels in that mapping — deskew (rotation) and border-crop (translation + resize) move pixels relative to the original frame, so both engines force them off in `adjust_preprocess` (`ocr.py`), regardless of CLI/GUI toggles (which can turn stages off, never frame-changers on). Measured on ZeffPapers + MSA00005, 119 pages, 2026-07-30: deskew fired on 36% of pages (median 1.1°, max 4.4°) and put word boxes a full line off their words near page edges; the A/B on the worst pages showed disabling it also **raised** mean WC by +0.034 (Tesseract's baseline fitting handles these angles; the rotation's resampling just blurred glyphs). Border-crop fired on 0/119 (adaptive binarize hollows out solid borders before the crop sees them). If a collection ever appears whose skew genuinely breaks recognition, the fix is inverse-transforming coordinates at emission (the GLMOCR handoff's "v2"), **not** re-enabling deskew. Locked by `tests/test_coordinate_frame.py`.

Design rules for the pipeline:
- **Per-stage CLI flags** (`--no-deskew`, `--binarize`/`--no-binarize`, `--no-clahe`, `--psm 4`, etc.) so a user can tune per-collection without code changes. Default-on for every stage that's safe *and measured to help*; user disables what hurts. Binarize is the one stage that defaults **off** (see above); it keeps both spellings so older scripts carrying `--no-binarize` still run.

**Rotated scans and `--psm` (per-collection, deliberately not a default).** Some collections contain pages scanned sideways — landscape tables in a portrait frame. `--psm 3` (the default) has **no orientation detection**, so Tesseract reads them at 90°/180° and emits fluent-looking garbage at low confidence. `--psm 1` (auto + OSD) fixes them: measured on `MSA00018`, where 16 of 82 pages were rotated (4×90°, 11×180°, 1×270°), the worst table page went 0.28 → 0.73 PC and its transcript from `LUIt-9T9 OSTT-009` to `TABLE I COLLEGE ADMISSIONS DATA FOR THE TEN GROUPS`. Crucially, Tesseract rotates *internally* and still emits ALTO coordinates in the **input image frame** — verified by overlaying the boxes on the untouched scan — so `--psm 1` needs no inverse transform and does not touch the coordinate-frame rule above. **It is not the default** because on the fixtures (no rotated pages) it is a wash (−0.002/+0.000/+0.001 mean WC), costs ~13% wall time, and **manufactures noise on blank pages**: OSD has too little text to orient, guesses, and turned a clean `empty` page (0 words, PC 0.95) into 9 junk words at PC 0.39. Reach for it when triage shows a collection with garbled-but-structured pages; check afterwards that blank pages did not become `scored`.
- **Tunable params** for the noisy stages (CLAHE clip, adaptive-threshold block size & C). Reasonable defaults committed; expose flags for the dials.
- **Save the preprocessed image** that Tesseract actually saw, optionally (e.g. `--debug-preprocessed <dir>`), so accuracy issues are debuggable. Don't write these by default — they double output volume.
- **Never mutate the source TIF**. Preprocessing operates on an in-memory copy or a temp file; the original on-disk TIF is never touched.
- **Per-page failure isolation**: if preprocessing or OCR fails on one page, log it, write a stub ALTO/Text indicating the failure, and continue. One bad page should not abort an object.

Be honest about the limits: preprocessing helps a lot on clean modern scans of typewritten or printed material. It will not rescue handwriting, heavy show-through, or severely degraded microfilm — for those we may need per-collection tuning or an alternative engine. Don't oversell.

## PDF rasterization (load-bearing)

Some collections (the first observed case was `UA00058`) include PDFs alongside TIFs. Rather than teach the rest of the pipeline about PDFs — which would mean PDF code paths through preprocess, OCR, and METS — `src/alto_ocr/pdf.py` rasterizes each PDF to a sequence of TIFs up front, and the existing TIF-only pipeline consumes them unchanged.

Design decisions (don't re-litigate without asking):

- **Library: `pypdfium2`.** Apache 2.0, pre-built wheels for Windows + Linux, no system prerequisites (statically links Google's PDFium). The faster `PyMuPDF` / `fitz` alternative is AGPL-licensed, which is awkward for a Purdue redistribution; `pdf2image` would force a poppler system install on Windows, contradicting the cross-platform / no-system-deps rule.
- **Rasterization happens per-Ref-ID, inside `process_ref_id`.** A `tempfile.TemporaryDirectory()` is allocated only when `inputs.has_pdfs` is true, so the pure-TIF common path stays fast. The temp dir is torn down in an `ExitStack` `finally` after the per-page loop completes.
- **Discovery is page-count-accurate.** `walker._find_sources_in` opens each PDF just long enough to read its page count (cheap — no rendering) so the total page budget is known before OCR begins. A PDF that fails to open is logged and skipped; discovery does not abort the Ref ID.
- **Output naming for rasterized pages**: `<pdfstem>_page<NNN>.tif` with three-digit zero-padding (handles ≤ 999 pages cleanly; natural sort handles overflow). ALTO/Text outputs reuse these stems unchanged.
- **DPI.** Rasterize at `PreprocessConfig.min_dpi` (default 300) as grayscale, baked into the TIF DPI metadata so the preprocess step's DPI-sanity check treats rasterized pages like any other scanned TIF.
- **Sort order in the merged page list.** Each rasterized page is assigned a *synthetic* sort path rooted at the source PDF's parent (e.g., `<RefID>/sub/Minutes_page001.tif` for a PDF at `<RefID>/sub/Minutes.pdf`), then the unified TIF+rasterized list is sorted by the same rel-path natural-key the walker uses. This makes rasterized pages sit in the position the parent PDF would have occupied — predictable regardless of where the temp dir actually lives on disk.
- **Per-PDF temp subdirectory.** Two PDFs sharing a stem in different sub-folders (e.g. `1927/Minutes.pdf` and `1928/Minutes.pdf`) rasterize into separate per-PDF subdirs of the temp dir keyed by their relative path inside the Ref ID, so the temp filenames never collide. Output-stem collisions for that case would still be caught by the walker's stem-collision check (those PDFs *would* produce identical ALTO/Text stems in the output — that's a legitimate user error to flag).

Operational notes:

- The whole PDF for a Ref ID is rasterized before any OCR runs in that Ref ID, so a Ref ID with a very large PDF will spike temp-dir disk usage. Roughly: 300 DPI grayscale LZW TIF ≈ 2–4 MB per page. Hundreds of pages is fine; thousands would be worth splitting across Ref IDs.
- Born-digital PDFs with an embedded text layer are still rasterized and re-OCR'd. We currently make no attempt to extract the existing text layer — keeps the contract simple ("every page is OCR'd"). If this ever matters (accuracy regression on born-digital), revisit by adding a `--prefer-pdf-text` path that bypasses rasterization for pages whose extracted text is non-empty.

## Input layout — design for variation

The reference spec is in `instructions.txt`. Layouts observed in practice:

- **Flat**: TIFs sit directly in the Ref ID folder (e.g., parts of `ZeffPapers/`).
- **Nested one level**: TIFs sit inside a single child folder under the Ref ID.
- **Nested several levels**: TIFs split across sub-folders, possibly grouped by year/document (e.g., `UA00058_17072/1927/UA58i19270613SM/*.tif`).
- **Mixed PDF + TIF**: PDFs and TIFs intermingled at any depth. The walker treats them as a single Ref ID; each PDF expands to N pages via rasterization.

The walker does a recursive scan with `os.walk` (one `scandir` per directory, no per-file stat — important on SMB-mounted network drives where per-file stats can turn a 30-second scan into 30 minutes). It accepts `.tif`/`.tiff`/`.pdf` case-insensitively, filters Windows junk (`Thumbs.db` and variants), and **sorts pages deterministically** by relative-path components. Natural sort on each component so `Page 2` comes before `Page 10` and meeting `19270104` sorts before `19270613`. If a source has no clear page-number signal, the natural-sort fallback is lexicographic on the full name; spot-check the order in the log. An object dropped at discovery for **output-stem collisions** comes back as a `SkippedRefId` and surfaces in `process`/`batch`/GUI summaries as a failed red row with a nonzero exit code (2026-07-30; previously a warning-only silent skip) — "no images at all" stays a plain non-object skip, since collections legitimately contain non-object folders.

## OCR engine

**Tesseract is the default engine** (decided 2026-07-29, reversing the earlier GLMOCR-default decision). GLMOCR had won the default on raw transcript accuracy for typewritten and handwritten pages, but the project's current phase is human remediation of transcripts, and three findings flipped the choice:

- Tesseract emits per-word confidences; glm-ocr emits none, so there is no signal to drive low-confidence highlighting or page-level review triage.
- glm-ocr's errors are *fluent* (plausible substitutions, silent normalization) and therefore harder for a human reviewer to spot than Tesseract's visibly garbled failures.
- glm-ocr fails badly on dense printed pages (bench on SCI00001 newspaper-style material: ~9 words/page captured vs Tesseract's ~1021 — it transcribes headlines and skips body text).

GLMOCR remains fully supported behind `--engine glmocr` (CLI) / the engine radio (GUI) and is the right choice for **handwriting-heavy objects**, where Tesseract produces uniformly low-confidence garble. The material mix across collections is unknown up front, so the working policy is: **run Tesseract by default, then use the per-page confidence (`PC`, see below) to find objects where it failed — typically handwriting — and re-run those Ref IDs with `--engine glmocr`.** Engine choice stays per-run; the removed per-Ref-ID filesystem markers (`HANDWRITTEN.txt` scheme) stay removed — split a mixed collection into two batch runs instead.

### Tesseract invocation + confidence capture (load-bearing)

Tesseract 5+ is invoked **once per page** with three output configs sharing a single recognition pass:

```
tesseract <preprocessed.png> <output_base> -l eng --psm 3 --dpi <dpi> alto tsv txt
```

→ `<output_base>.xml` (ALTO), `.tsv` (per-word confidences), `.txt` (transcript). `_merge_tsv_confidence` in `src/alto_ocr/ocr.py` then copies each TSV word confidence onto the matching ALTO `<String>` as `WC` (0..1, matched by pixel bbox — same recognition pass, so boxes are identical), sets `Page/@PC` to the mean word confidence, and deletes the TSV. The merge must run **before** institution-format conversion (matching needs pixel coordinates) and is best-effort — a merge failure logs a warning rather than turning a good page into a failure stub. Do **not** reintroduce a second recognition pass: the single invocation is both the confidence source and a ~2× speedup over the old alto-then-txt scheme.

Prefer `subprocess.run` over `pytesseract` — we need clean control over `--psm`, `--dpi`, and config files, and `pytesseract` adds nothing here. Default `-l eng`, expose `--ocr-lang` for non-English collections. Default `--psm 3` (fully automatic), exposed as a CLI flag so a user can retry a problem object with `--psm 4` (single column) or `--psm 6` (uniform block).

### GLMOCR (handwriting engine)

GLMOCR runs against a locally-hosted Ollama instance (default `http://localhost:11434`, override via `--glmocr-endpoint` or `OLLAMA_HOST`). The model tag defaults to `glm-ocr:q8_0` (the Q8_0 quantization of the 1.1B glm-ocr model; the `glm-ocr:latest` F16 build the behavior notes were characterized on is also available locally, but q8_0 is the chosen default). The integration design and the empirically-derived constraints (image-size limits, prompt sensitivity, blank-page handling) live in `src/alto_ocr/glmocr.py`'s module docstring; the original handoff doc is in `docs/glmocr_handoff.md`.

glm-ocr emits **markup** on structured content — HTML `<table>` for tables, LaTeX `$…$` for math, sometimes Markdown pipe-tables — even with the bare `"OCR"` prompt. Since the output contract is *plain-text* transcripts plus per-word `<String>` hit-boxes, `src/alto_ocr/markup.py` (`normalize_markup`) flattens that markup to plain text (tables → space-joined cell rows; LaTeX → readable text with symbols mapped to Unicode) before the engine writes the `.txt` and splits words. It runs per block inside `GLMOCREngine.ocr` and is a near no-op on prose. Without it a whole HTML table collapses into a single `<String>` (one useless hit-box) and the transcript is off-contract.

## Remediation workflow (current phase)

Generating the files works; correcting the transcripts is the bottleneck. The field-standard architecture (digital cultural heritage) is *engine → ALTO → human-in-the-loop editor → export ALTO*, and — critically — **triage-first: almost no institution corrects everything**. Correctors work worst-first / highest-value-first, guided by confidence data. Direction agreed 2026-07-29:

- **Confidence capture — done.** Tesseract runs write `WC` per `<String>` and `PC` per `<Page>` into the ALTO (see above). The institution's reference attribute order already reserved slots for both, so downstream consumers tolerate them.
- **Triage report — done.** `src/alto_ocr/triage.py` + the `triage` CLI command scan an output tree's ALTO files (deliberately *not* in-band OCR state, so the same scan works on old outputs and after GLMOCR re-runs) and rank worst-first: per-object table + review-queue page table on the console, `triage_objects.csv` / `triage_pages.csv` (utf-8-sig for Excel) next to the outputs. Pages classify as `scored` (has WC), `unscored` (GLMOCR — no confidence, full proofread), `empty`, or `failed` (pipeline stub, sorts first). Thresholds are report-time dials (`--low-wc` 0.50, `--review-pc` 0.80), not stored state. `process`/`batch` runs also scan afterwards: summary table gains Mean WC / Review pgs columns, `--summary-json` gains per-ref `confidence` blocks, and a one-line hint points at the `triage` command. Objects with mean WC < 0.60 are called out as handwriting candidates for a GLMOCR re-run.
- **Review GUI — done.** "Review" tab in the Tkinter app (`src/alto_ocr/review.py` = Tk plumbing only; all edit/save semantics live headless-testable in `src/alto_ocr/remediate.py`). Correctors pick the output object folder plus the source-image folder (TIFs are not in the output tree — the preservation folder is canonical), get a worst-first page list (per-page `PC`), and see word boxes colored by `WC` over the page image; clicking (or stepping Next/Prev / `n`/`p`) opens the word in an editor bar with a magnified snippet of the scan region. Enter commits and advances to the next flagged word; Ctrl+Z undoes; deletion (hallucinated words) confirms, removes the neighboring `<SP>`, and cascades away emptied TextLine/TextBlock/ComposedBlock containers (never PrintSpace). Semantics locked in `tests/test_remediate.py`: a human edit sets `CONTENT` + `WC="1.00"` (verified words leave every low-confidence view); save re-serializes with institution conventions, recomputes `Page/@PC` from current WCs (partially-corrected pages sink in triage), refreshes a `<Processing ID="review">` provenance element with a cumulative corrected-word count, and regenerates the sibling `Text/*.txt` so ALTO and transcript never drift.
- **Repackage command — done.** `repackage <output_root>` (logic: `remediate.repackage_object`) regenerates everything derived from corrected ALTO: transcripts re-render through `remediate.alto_to_text` — the same canonical renderer the review GUI's save uses, so both tools produce identical Text/ files — and the METS is rebuilt with fresh sizes/checksums. The existing METS's structMap **page order is preserved** (stem sorting can't reconstruct nested-folder source order); pages new on disk append in natural order, METS entries with no ALTO on disk are dropped with a warning. Idempotent: byte-identical transcripts aren't rewritten. Opt-outs are respected — repackage never invents a `Text/` folder or a METS the original run suppressed. Run it after any external-editor session (eScriptorium etc.) and before upload packaging; after GUI-only review it mainly refreshes the METS (the GUI already keeps txt in sync).
- **Correction-protection guards — done (2026-07-30).** Two guards keep hand corrections safe: (1) `process`/`batch` refuse to overwrite an object whose existing ALTO carries `<Processing ID="review">` provenance — the object is reported failed with the reason, nothing is touched; `--force-reocr` overrides, and moving the output folder aside also works (GUI runs are always guarded — no force in the GUI on purpose). (2) `ReviewPage.save` re-stats the ALTO and raises `ConcurrentEditError` when it changed on disk since load (second review session / external editor); the Review tab then asks before overwriting. Both locked by `tests/test_overwrite_guards.py`.
- **eScriptorium — growth path, not adopted.** If handwriting-heavy collections need real accuracy at scale, pilot eScriptorium/Kraken (Docker, web multi-user) on one collection so corrections train a model — that's the only path where correction effort compounds. PoCoTo was evaluated and rejected (IMPACT-era, unmaintained); Transkribus rejected by default (paid credits, off-campus cloud).

## Borrowing from ePubs_Main

`ePubs_Main/` is a sibling reference project (PDF accessibility, not OCR-to-ALTO). **Fork patterns, not code or dependencies.**

**Take** (re-implement, don't import):
- CLI scaffolding shape from `ePubs_Main/src/processor/main_simple.py` — typer + rich `Console` + `RichHandler` logging.
- Batch-processing structure from `simple_batch_processor.py` — per-item try/except, summary JSON written next to outputs, optional `--workers` for parallelism.
- Logging conventions: module-level `logger = logging.getLogger(__name__)`, info on stage transitions, debug for per-item details.

**Do not take**:
- `PyMuPDF` / `fitz`, `pikepdf`, `ocrmypdf`, `python-magic`. For the PDF-rasterization step we use **`pypdfium2`** (Apache 2.0; see the "PDF rasterization" section for the rationale and the alternatives we rejected). Do not introduce additional PDF libraries.
- The PDF/UA enhancer (`simple_pdfua_enhancer.py`), MediaBox normalization, orphan-tag stripping — none of this applies to METS/ALTO.
- `--skip-ocr` / `--force-ocr` / `--text-threshold` semantics — those exist because input PDFs sometimes already have text. Our PDFs are rasterized and re-OCR'd unconditionally; we do not extract embedded text.

Required runtime deps for the new module: `typer`, `rich`, `tqdm`, `lxml` (METS XML), `Pillow` (TIF I/O + DPI metadata), `opencv-python-headless` (preprocessing), `numpy` (transitive via OpenCV but pin explicitly), `pypdfium2` (PDF rasterization). Tesseract is a system dep, not a Python one.

## Running

```
# Single object (Tesseract is the default engine; ALTO gains WC/PC confidences)
python -m src.alto_ocr.main process <object_dir> <output_root>

# All objects in a collection directory
python -m src.alto_ocr.main batch <collection_dir> <output_root> --workers 4

# Re-run handwriting-heavy objects with GLMOCR (requires a local Ollama)
python -m src.alto_ocr.main batch <collection_dir> <output_root> --engine glmocr

# Rank existing output worst-first for remediation (writes triage_*.csv too)
python -m src.alto_ocr.main triage <output_root>

# After correction: regenerate Text/ + METS from the edited ALTO
python -m src.alto_ocr.main repackage <output_root>

# Override Tesseract location (Windows non-PATH install; auto-detect covers
# the standard UB-Mannheim locations already)
python -m src.alto_ocr.main batch <collection_dir> <output_root> ^
    --tesseract-path "C:\Program Files\Tesseract-OCR\tesseract.exe"

# Tune preprocessing for a tricky collection
python -m src.alto_ocr.main batch <collection_dir> <output_root> ^
    --no-clahe --psm 4 --debug-preprocessed debug_imgs/

# GUI
python -m src.alto_ocr.gui

# Environment check (deps + both engines)
python -m src.alto_ocr.main doctor
```

## Layout map

```
Alto_OCR/
├── instructions.txt        # Output-format spec — source of truth for the contract
├── ZeffPapers/             # Test fixture (one collection); read-only
├── output/                 # Generated ALTO/Text folders + METS per Ref ID (not committed)
├── docs/                   # Handoff docs (e.g. glmocr_handoff.md)
├── tests/                  # unittest suite; CI runs it on ubuntu + windows (py3.12)
├── ePubs_Main/             # Pattern-reference only; not imported, not modified
└── src/alto_ocr/           # The tool: main.py (CLI), gui.py, pipeline.py, ocr.py (engines),
                            #   alto_format.py (institution ALTO serialization/conversion — the
                            #   shared layer; remediate/review depend on it, never on ocr.py), …
```
