# Alto_OCR — Architectural & Security Review @ `51860d0` (v0.8.1)

**Verdict up front:** the codebase is in good structural health — clean layering, no circular imports, disciplined subprocess and path handling, and honest per-page failure isolation. But the review found **one critical correctness flaw at the heart of the product's promise** (hit-box coordinates are emitted in the wrong reference frame whenever deskew or border-crop fires), **one critical data-durability gap** (hand-corrected ALTO — the most expensive artifact this project produces — is unprotected against truncation, clobbering, and concurrent edits), and **one critical accounting gap** (objects can silently vanish from a batch with exit code 0). Severity-ordered findings below; the three criticals include concrete refactoring strategies.

## System map

```
                 CLI (main.py)          GUI (gui.py)
                    │  │  │                │    │
       ┌────────────┘  │  └───────┐       │    └── review.py (Tk only)
       ▼               ▼          ▼       ▼            │
  walker.py       pipeline.py   triage.py         remediate.py ◄─ headless edit/save
   │    │            │  │  │        │              │   │   │      + repackage logic
   │    ▼            ▼  │  ▼        │              │   │   │
   │  pdf.py  preprocess │ mets.py ◄┼──────────────┘   │   │
   │                     ▼          │                  │   │
   │                  ocr.py ───────┼──────────────────┘   │
   │        (TesseractEngine +      │   imports _ATTR_ORDER, _qname,
   │         GLMOCREngine + the     │   _reorder_attrs, _serialize_with_tabs
   │         institution-format     │
   │         converter + helpers)   ▼
   │             │    │    │     walker._natural_key (also used by triage, main)
   │             ▼    ▼    ▼
   │      glmocr.py markup.py line_detect.py
   │             │
   │             ▼
   └──────► _util.py (stdlib-only; shared with scripts/)
        alto_parse.py + overlay_alto.py: stdlib QA sidecar (deliberately dependency-light)
```

No cycles. The one structural smell is that **remediate/main/pipeline/triage all import underscore-private symbols from `ocr.py` and `walker.py`** — the serialization/formatting layer is a de-facto shared module that hasn't been extracted (details in the architecture section).

---

## CRITICAL

### C1 [logic/arch] — Tesseract ALTO coordinates are in the *preprocessed* frame; every consumer maps them onto the *original* image

The default preprocessing chain includes deskew (rotation) and border-crop (translation + dimension change). Tesseract OCRs the transformed image, so the ALTO boxes and `Page` dimensions describe the **deskewed/cropped frame**. But every consumer — the public archive's hit-box rendering, the Review tab ([remediate.py:106](src/alto_ocr/remediate.py:106) scales by `image_px / Page_dims`), and the QA overlays ([alto_parse.py:66](src/alto_ocr/alto_parse.py:66)) — draws those boxes on the **original preservation TIF**. Only a *uniform* transform (the DPI upscale) cancels in that ratio. A crop that removes a 200-px black scanner border shifts every box and corrupts the scale factor for the whole page; a 1° deskew displaces box edges by ~30 px on a 3,300-px page.

The codebase already *knows* this: `GLMOCREngine.adjust_preprocess` ([ocr.py:1003](src/alto_ocr/ocr.py:1003)) disables deskew and border-crop with a comment explaining exactly this hazard — "with them on, the ALTO coordinates no longer land on the original image the access platform displays" — while `TesseractEngine.adjust_preprocess` ([ocr.py:577](src/alto_ocr/ocr.py:577)) keeps both on for the **default engine**. The live validation didn't catch it because the synthetic demo pages were clean (border-crop only fires on real dark borders; deskew skips < 0.2°) — but microfilm and bound-volume scans with black edges are precisely the target material. Concrete failure: a corrector opens a bordered page in the Review tab and the magnified snippet shows the *wrong region of the scan*; on the public site, search-hit highlights land beside the word, drifting worse toward the page edges.

**Refactoring strategy:**
1. Add `transform: np.ndarray` (2×3 affine, original→processed, identity default) to `PreprocessResult`; have upscale, `_deskew`, and `_crop_borders` compose their contribution into it (all three are affine).
2. After OCR, before mm10 conversion, map every `HPOS/VPOS/WIDTH/HEIGHT` through the inverse (axis-aligned bounding box of the inverse-mapped corners — exact for crop/scale, sub-pixel-approximate for ≤2° rotations) and set `Page` dims from the **original** image. Pipeline owns both `pre` and the ALTO path, so it's the natural place; `dpi` passed to the converter becomes the original's effective DPI.
3. Shippable today as a stopgap: make `TesseractEngine.adjust_preprocess` drop `do_border_crop` (the worst offender — translation + scale corruption) exactly as GLMOCR does, and accept the small deskew error or drop deskew too. Recognition accuracy on skewed pages suffers slightly, which is why (1)+(2) — deskew for recognition, inverse-map for emission — is the real fix.
4. Add a regression test: render a synthetic page, pad it with a black border, rotate it 2°, run the pipeline, and assert box-IoU against known word positions on the *original* image.

### C2 [logic/sec] — Hand-corrected ALTO has no durability or concurrency protection

Corrected ALTO is the single most expensive artifact this project produces (human hours per page), and it is exposed on four sides:

- **(a) Non-atomic writes.** `_serialize_with_tabs` ([ocr.py:378](src/alto_ocr/ocr.py:378)) does a direct `write_text` — a crash, disk-full, or SMB hiccup mid-write leaves a truncated, unparseable ALTO, destroying the corrector's accumulated work in that file. Same pattern in `write_mets` ([mets.py:261](src/alto_ocr/mets.py:261)) and every txt write.
- **(b) Re-OCR silently clobbers reviewed pages.** `process_ref_id` ([pipeline.py:240](src/alto_ocr/pipeline.py:240)) overwrites existing output unconditionally. The intended workflow *encourages* re-runs into the same root ("re-run low-PC Ref IDs with `--engine glmocr`"); nothing checks for the `<Processing ID="review">` provenance the save path stamps, so one mistyped batch erases weeks of correction with no warning.
- **(c) No concurrent-edit guard.** Two Review-tab sessions (or a review save racing a `repackage`) on the same shared-drive object are last-writer-wins with silent loss.
- **(d) GUI discards edits after a *failed* save.** `_confirm_discard` ([review.py:278](src/alto_ocr/review.py:278)) returns `True` after calling `_save_page()` even when the save raised `OSError` (network blip) — the caller then switches pages and the unsaved edits are gone. It should return `not self._page.dirty` (dirty is only cleared on successful save).

**Refactoring strategy:**
1. One `atomic_write_text/bytes` helper in `_util.py` (tmp file in the same directory + `os.replace`), adopted at the three serialization choke points (`_serialize_with_tabs`, `write_mets`, txt writes). This is a ~20-line change that removes truncation risk everywhere at once.
2. In `process_ref_id`, before writing a page whose ALTO already exists, check the existing file for review provenance (one cheap parse, only when the file exists — or once per object). Refuse with an actionable message unless a new `--force-reocr` flag is passed.
3. `ReviewPage` records `st_mtime_ns` at load; `save()` re-stats and aborts with "file changed on disk since you opened it" on mismatch (SMB mtime granularity is coarse but sufficient for human-timescale races).
4. Fix (d) as a one-line change now, independent of the rest.

### C3 [logic] — Batch integrity: skipped objects exit 0, and worker-pool logs go dark

- `discover_ref_id` returns `None` both for "no sources" and for **stem collisions** ([walker.py:230](src/alto_ocr/walker.py:230)); `discover_collection` drops those Ref IDs. `batch` then processes what remains and **exits 0** ([main.py:570](src/alto_ocr/main.py:570)) — an object that was never OCR'd leaves only a warning line buried in hours of logs, and never appears in the summary table or `--summary-json`. On the single-object path the collision case is at least a nonzero exit, but with the *wrong message* ("No TIFs found in X" — [main.py:314](src/alto_ocr/main.py:314)) when the real cause is a collision.
- With `--workers > 1`, spawned children never configure logging ( `_setup_logging` runs only in the parent; [main.py:352](src/alto_ocr/main.py:352) is executed in a fresh process). Python's last-resort handler prints WARNING+ raw to stderr and **drops all INFO** — so the pool path shows *no* per-Ref-ID "done" lines for hours, contradicting the code comment that relies on them ([main.py:494](src/alto_ocr/main.py:494)), and warnings arrive unformatted and untimestamped.
- The GUI's batch loop has a smaller cousin: a Ref-ID-level exception is logged but **no failed result is appended** ([gui.py:794](src/alto_ocr/gui.py:794)), so the closing "N failed page(s)" line undercounts — the CLI's `_failed_ref_result` synthesis was never mirrored.

**Refactoring strategy:**
1. Make discovery return skips explicitly: `discover_collection → (list[RefIdInputs], list[SkipRecord(ref_id, reason)])`. Surface skips in the summary table, `--summary-json`, and the exit code; give `process` the true collision message. This converts a silent hole into a visible red row for ~40 lines of change.
2. `ProcessPoolExecutor(initializer=...)` installing a basic handler at the parent's level in each child (or, for uniform Rich formatting, a `QueueHandler` in children draining to a parent `QueueListener`).
3. Move `_failed_ref_result`/`_record_ref_failure` from `main.py` into `pipeline.py` and reuse them in `gui._worker_main` — fixes the undercount and removes CLI/GUI duplication in one move.

---

## HIGH

**H1 [logic] — `remediate`/`repackage` silently blank transcripts on prefix-namespaced ALTO.** `ReviewPage` and `alto_to_text` resolve the namespace via `root.nsmap.get(None)` ([remediate.py:90](src/alto_ocr/remediate.py:90), [remediate.py:250](src/alto_ocr/remediate.py:250)). An ALTO that declares `xmlns:alto=…` with prefixed elements — perfectly legal, and plausible from the eScriptorium round-trip that is this workflow's announced growth path — yields `ns=None`, `iter()` finds zero `String`s, and `repackage` **rewrites every Text/ file to empty** (the diff-write sees "" ≠ current and commits it). `triage` already handles this correctly with `{*}` wildcards ([triage.py:165](src/alto_ocr/triage.py:165)); port that pattern to `remediate`, and add a guard: refuse to write an empty transcript over a non-empty one unless the ALTO genuinely has zero words.

**H2 [logic/sec] — Failure-stub ALTO is built by unescaped string interpolation.** `_write_failure_stubs` formats the raw exception text into `ALTO_FAILURE_STUB` ([pipeline.py:205](src/alto_ocr/pipeline.py:205)). Tesseract stderr routinely contains `<`, `>`, `&` — the resulting stub is **malformed XML**. `triage` happens to survive (parse failure also classifies as `failed`), but the Review tab's `ReviewPage` raises `XMLSyntaxError`, which `_open_page` doesn't catch ([review.py:300](src/alto_ocr/review.py:300)) → unhandled Tk callback exception. Fix: `xml.sax.saxutils.escape(reason)` (already imported in `ocr.py`), and widen `_open_page`'s except to include parse errors.

**H3 [arch] — `doctor` still describes the pre-flip engine policy.** [main.py:860-887](src/alto_ocr/main.py:860) labels glm-ocr "(default engine)" and Tesseract "(fallback engine)", including in the code comments — the `3eae8f5` engine flip missed this command. Anyone running the advertised environment check gets policy guidance that contradicts the CLI, GUI, and CLAUDE.md. Cosmetic to fix, misleading to leave.

## MEDIUM

**M1 [logic] — `markup.py` can eat `$` signs from prose across lines.** `_MATH_SPAN_RE`'s single-`$` alternative is DOTALL ([markup.py:189](src/alto_ocr/markup.py:189)), so two `$` characters on *different lines* pair into one "math span". The currency guard only saves the case where a digit immediately follows the closing `$`; `"cost $5 flat"` … later line … `"paid in $"` silently loses both dollar signs (glmocr path only). Restrict the single-`$` alternative to one line and/or require a LaTeX marker in the interior. Notably, **`normalize_markup` has no tests** despite its documented history of exactly this class of regression.

**M2 [logic] — Provenance count can double on save-retry.** `ReviewPage.save()` clears `session_edits` only after the txt write; if the ALTO write succeeds and the txt write raises, a retried save re-reads the *already-updated* total from the in-memory tree and adds `session_edits` again ([remediate.py:199](src/alto_ocr/remediate.py:199), [remediate.py:229](src/alto_ocr/remediate.py:229)). Cosmetic (a statistic), but provenance is the one thing that should never lie. Snapshot `prior` at load, or clear `session_edits` immediately after `_stamp_provenance`.

**M3 [arch] — `repackage` rebuilds METS from scratch: future institution-added sections and the original CREATEDATE are lost.** `build_mets` regenerates the whole document ([remediate.py:384](src/alto_ocr/remediate.py:384)); `metsHdr/@CREATEDATE` becomes "now" (METS has `LASTMODDATE` for this), and if the institution ever merges `dmdSec`/`amdSec` into these files — which [mets.py:12](src/alto_ocr/mets.py:12) explicitly anticipates — repackage will silently drop them. Preserve CREATEDATE from the old header now; when dmdSec appears, switch to surgical structMap/fileSec replacement.

**M4 [logic] — Review-tab page list: stems parsed back out of display labels, and sorted lexicographically.** `_on_page_selected` recovers the stem via `.split("   ")[0]` ([review.py:270](src/alto_ocr/review.py:270)) — a stem containing three consecutive spaces (archival filenames do contain multi-space runs) truncates and the page can't open. Store stems by listbox index instead of round-tripping through the label. Also `load_object` sorts `sorted(alto_dir.glob(...))` ([review.py:226](src/alto_ocr/review.py:226)) — lexicographic (`page10` before `page2`) while every other component uses `_natural_key`.

**M5 [logic] — GLMOCR + `--workers N` multiplies concurrent Ollama requests.** Ollama serializes generations per model, so N−1 requests queue and the 120 s per-page timeout can fire while *queued*, producing spurious failure stubs at higher worker counts. Either document "workers=1 for glmocr" or scale the timeout by worker count.

## SECURITY (calibrated to the real threat model: single-operator internal tool, institution-controlled inputs, shared network drives, external-editor round-trips)

**S1 — lxml parses with default entity resolution.** Every `etree.parse` (ocr, triage, remediate, repackage) uses defaults, which resolve entities — including local-file external entities — declared in a document's internal DTD subset. Your ALTO round-trips through external editors and lives on shared drives where others write. Realistic risk is low, but the hardening is one shared parser: `etree.XMLParser(resolve_entities=False, no_network=True)` (you never legitimately use entities). The stdlib-ET sidecar (`alto_parse`) is already safe (it refuses external entities).

**S2 — `Image.MAX_IMAGE_PIXELS = None` disables the decompression-bomb guard process-wide** ([alto_parse.py:26](src/alto_ocr/alto_parse.py:26), [review.py:34](src/alto_ocr/review.py:34)) — an import side effect that also covers the pipeline when the GUI runs. A corrupt/malicious TIF in a user-picked folder becomes an unbounded allocation. Prefer a generous explicit cap (e.g. `4_000_000_000`) over `None`.

**S3 — CSV formula injection.** `triage_*.csv` cells come from filesystem stems ([triage.py:280](src/alto_ocr/triage.py:280)); a stem beginning `=`/`+`/`-`/`@` executes as a formula when the corrector opens the CSV in Excel (the explicit consumption path). Far-fetched vector, one-line neutralization (prefix `'` on those leading chars).

**S4 — Remote Ollama means page images over plaintext HTTP.** `OLLAMA_HOST` redirection sends base64 page scans unencrypted ([glmocr.py:203](src/alto_ocr/glmocr.py:203)). Fine for localhost (the design center); worth one doc sentence if a remote endpoint is ever used for unreleased archival material.

**S5 — Hardening nit:** `subprocess.run(..., text=True)` without `encoding=` decodes Tesseract output with the ANSI code page on Windows; add `encoding="utf-8", errors="replace"` to avoid a theoretical decode crash inside the error path.

**Clean bill, verified:** no `shell=True` anywhere, list-args throughout (spaces-in-paths safe); no path-traversal surface (output paths derive from real directory names); MD5 is fixity-only and annotated as such; no credentials or secrets stored; settings JSON holds only paths/toggles; PDFium (Chrome's PDF library) is a reasonable parser for donor-originated PDFs; GLMOCR's ALTO builder escapes CONTENT correctly ([ocr.py:848](src/alto_ocr/ocr.py:848)); `_natural_chunks` is type-safe by construction (regex-split parity guarantees str/int never compare); pool payloads pickle cleanly on Windows spawn; `BrokenProcessPool` harvesting logic is genuinely correct.

## PERFORMANCE

**P1 — Each Tesseract page is parsed and serialized twice** — `_merge_tsv_confidence` writes the ALTO ([ocr.py:256](src/alto_ocr/ocr.py:256)), then `_convert_to_institution_format` re-parses and rewrites it. Passing the tree in memory halves the XML work; ~1–3% of page wall-time, so an easy win, not urgent.

**P2 — Post-run confidence scan re-reads the whole output tree** ([main.py:562](src/alto_ocr/main.py:562)): on a 4,000-page batch over SMB this adds minutes after the run finishes. It's a *deliberate* design (one scan implementation everywhere, per CLAUDE.md) — accept, but be aware it's the tail latency on big batches.

**P3 — Review zoom at 200% on high-DPI scans** builds a full-page `PhotoImage` (600 DPI microfilm → ~10,000×14,000 px ≈ 0.5 GB) ([review.py:342](src/alto_ocr/review.py:342)) — a `MemoryError` risk. Cap the rendered pixel budget or render only the visible viewport.

**P4 — The GUI log Text widget retains every line of a multi-hour batch** unbounded (drain is bounded per tick, retention isn't); Tk Text gets sluggish past ~10⁵ lines. Trim to the last N lines.

**Verified efficient:** walker is genuinely one `scandir` per directory with zero per-file stats (the SMB constraint holds); checksum computation streams in 64 KiB chunks; `bucket.pop(0)` in the TSV merge is O(n²) only across *identical* bboxes (n≈1); `_next_after`'s `list.index` scans are O(n²) per keypress but trivially fast at page word counts; repackage's read-before-write diff is the right idempotence trade on network storage.

## ARCHITECTURE & PROCESS

**A1 — Extract the serialization layer.** `remediate.py` imports `_ATTR_ORDER, _qname, _reorder_attrs, _serialize_with_tabs` from `ocr.py`; `pipeline`, `main`, `triage`, `remediate` import `_natural_key`/`_rel_natural_key`/`_natural_chunks` from `walker`. Private-underscore symbols crossing module boundaries means the "institution ALTO format" abstraction has no home. Extract `alto_format.py` (attr order, qname, serializer, and ideally `_convert_to_institution_format`) and promote the natural-sort helpers to public names — `ocr.py` (1,104 lines: two engines + converter + GLMOCR geometry assignment) stops being a god-module, and H1's namespace consolidation gets a natural landing spot.

**A2 — Three ALTO-parsing idioms** (stdlib-ET dual-namespace probe in `alto_parse`, `{*}` wildcard in `triage`, `nsmap.get(None)` in `remediate`) is how H1 happened. One namespace-resolution helper, used everywhere, prevents the next drift.

**A3 — The GUI↔Review settings-dict aliasing contract** ([gui.py:731](src/alto_ocr/gui.py:731) must `clear()/update()`, never rebind, because `ReviewFrame` holds the reference) is correct today and guarded only by a comment. Passing `ReviewFrame` a getter/setter, or a tiny `Settings` object, makes the contract structural instead of disciplinary.

**A4 — Test-coverage skew.** 66/66 green, but all 66 tests cover code from the last three sessions plus glmocr text cleanup. **Zero tests** exist for the oldest, most contract-critical transforms: `_convert_to_institution_format` (margins, mm10, ID renumbering, namespace swap — the output contract itself), `mets.build_mets` (NCName sanitization, ID dedup), `walker` (natural sort, stem-collision prediction), `markup.normalize_markup` (see M1), `pdf`, `line_detect`, and preprocess beyond the DPI floor. The converter and walker are where a regression would corrupt deliverables silently; they deserve the same lock-in the remediation semantics got.

**A5 — Dependencies are range-pinned** (sensible bounds, `<major+1` caps — good), but the PyInstaller exe rebuild would benefit from a lock file (`pip freeze` snapshot in `packaging/`) so the shipped binary is reproducible. And the standing item: **CLAUDE.md is still gitignored** ([.gitignore:40](.gitignore:40)) — all project documentation exists only on this machine; that's a bus-factor risk for the successor-maintainer scenario the project explicitly plans for.

---

## Suggested order of attack

1. **C2(a)+(d)** — atomic-write helper + the `_confirm_discard` one-liner (an afternoon; removes the live data-loss vectors).
2. **C1 stopgap** — disable border-crop for Tesseract like GLMOCR does, then schedule the inverse-transform work; spot-check one real bordered scan with `overlay_words.py` first to size the damage.
3. **C2(b)** — the reviewed-ALTO overwrite guard, before correctors start producing valuable files at volume.
4. **C3 + H1 + H2 + H3** — each is small and self-contained.
5. **A1/A2/A4** as the next refactoring block, which structurally prevents the H1 class from recurring.

Happy to start on any of these — the two afternoon-sized items (1 and the H-row) would close out most of the acute risk.