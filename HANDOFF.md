# ALTO-OCR + Goobi: handoff for moving to a WSL machine

Written 2026-10-15 on the Windows dev host (`C:\Users\schipp0\Alto_OCR`) for
picking the work up on a different computer under WSL, with Claude Code.

**Read order on the new machine:** this file, then `CLAUDE.md` (the durable
project rules, auto-loaded by Claude Code), then
`integration/goobi/README.md` and `integration/goobi/PROVISIONING.md` for
anything Goobi.

---

## 1. State in one screen

| | |
|---|---|
| Version | **0.10.4** (`src/alto_ocr/__init__.py`) |
| `main` | `0e35ba3` *merge: Goobi provisioning kit*, **pushed** to the `alto-ocr` remote |
| Remote | `alto-ocr` = `https://github.itap.purdue.edu/schipp0/alto-ocr.git` (named `alto-ocr`, not `origin`) |
| Tests | 463 pass, 16 skipped on Windows (`python -m unittest discover -s tests`) |
| CI | **none** (dropped in `a0e84b2`); the pre-push hook in `scripts/hooks/` is the only gate |
| Packaged exe | last built by Claude at 0.10.2 (Windows, `dist/`); 0.10.3 and 0.10.4 not built by Claude |
| Goobi | provisioning kit in `integration/goobi/`; a **new Linux Goobi server** is coming and will be built fresh from it |

Recent releases, newest first (each was a feature branch merged into `main`
with a `merge:` commit):

| version | what |
|---|---|
| kit | Goobi provisioning kit: config overlay, idempotent installer, drift check (no version bump; not app code) |
| 0.10.4 | Review usability fixes: shortcuts no longer die on focus changes, multi-word select + delete, reading-order drag-and-drop, Tk right-click menu, browser editor bar no longer resizes the scan |
| 0.10.3 | Reading-order path legible on the scan; edit a word without leaving reading-order mode |
| 0.10.2 | 2026-10-02 architecture-audit fixes (interrupted re-runs, Tesseract timeout, webreview threading/locks, corrupt-ALTO tolerance) |
| 0.10.1 | Dark mode (Tk + browser) |
| 0.10.0 | Word-level reading order; browser review parity |
| 0.9.x | Supervisor-review GUI batch; browser review server; glm-ocr loop collapse |

---

## 2. Do these on the OLD (Windows) machine before you leave

Everything on `main` is already on the remote. Four things are **not**, and
exist only on this machine:

### 2a. The provenance net-count fix (only copy is in a stale worktree)

The review save counts provenance **per action**: a word edited twice, or
edited then deleted, counts as two. A fix that counts **net effect per word**
(`ReviewPage._before` snapshots + `_net_counts`) was built on 2026-09-24 and
never landed. `main`'s `remediate.py` still has `self.session_edits += 1`.

Its only copy is the uncommitted worktree
`C:\Users\schipp0\Alto_OCR\.claude\worktrees\friendly-hofstadter-841d46`
(branch `claude/friendly-hofstadter-841d46`, based on v0.9.1). It also holds
that era's GUI work, which landed separately, so only the `remediate.py`
counting change matters. `.claude/` is git-ignored: nothing there travels.
Preserve it as a branch on the remote:

```bat
cd C:\Users\schipp0\Alto_OCR\.claude\worktrees\friendly-hofstadter-841d46
git add -A
git commit -m "WIP: net-effect provenance counting (2026-09-24 worktree, based on v0.9.1)"
git push alto-ocr HEAD:wip/provenance-net-counts
```

Porting it to `main` is real work (see section 6, item 1). Do **not** merge
that branch as is: it predates 0.9.2 through 0.10.4.

### 2b. Git-ignored docs

`docs/` is in `.gitignore`, so these never reached the remote, although
`CLAUDE.md` points at the first one:

- `docs/glmocr_handoff.md`: the glm-ocr integration design
- `docs/handoff_audit_2026-10-02.md`: the audit brief behind 0.10.2 (all six findings landed)
- `docs/ReviewProcedures.txt`: an early corrector walkthrough (outdated: delete no longer asks first)

Copy them across by hand (or un-ignore `docs/` and commit them; your call).
Also local-only and ignored: `CorrectPageSequencing/` (sample ALTO), `dist/`
and `build/` (PyInstaller output), and an unusable `venv/` (an old copy from
WSL; this machine runs on system Python 3.12).

### 2c. Claude's memory for this project

Claude Code keeps per-project notes outside the repo, at
`C:\Users\schipp0\.claude\projects\C--Users-schipp0-Alto-OCR\memory\`.
Section 7 of this file summarizes everything in them that is still true, so
copying them is optional. To carry them anyway: start `claude` once in the
new clone (it creates `~/.claude/projects/<mangled-path>/`, e.g.
`-home-<you>-alto-ocr`), then copy the `.md` files into its `memory/`
folder. Several describe this Windows host (Python, Tesseract and Ollama
locations), so expect Claude to re-check those on the new machine.

### 2d. The `C:\Users\schipp0\goobi` folder

Nothing in it needs to move. It holds an unmodified clone of the Goobi
source (its "modified" files are line-ending noise only), installer
leftovers, and August/September handoff notes whose durable content is
already in `integration/goobi/`. **Delete**
`goobi\install\goobi_install_credentials.txt` (and its `~` backup): it holds
a database password that the notes call stale, and it should not be copied.

---

## 3. Setting up the NEW machine (WSL, Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-tk \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd
# python3-tk: the Tk GUI (WSLg supplies the display). tesseract-ocr-osd: needed for --psm 1.

git clone -o alto-ocr https://github.itap.purdue.edu/schipp0/alto-ocr.git ~/alto-ocr
cd ~/alto-ocr
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-web.txt
git config core.hooksPath scripts/hooks     # pre-push runs the tests; once per clone

venv/bin/python -m unittest discover -s tests   # expect all green
venv/bin/python -m src.alto_ocr.main doctor     # deps + both engines
venv/bin/python -m src.alto_ocr.gui             # Tk GUI via WSLg
```

`-o alto-ocr` names the remote the way every doc and commit message here
assumes. Cloning from Purdue's GitHub Enterprise needs your credentials
(a personal access token over HTTPS, or an SSH key added there).

**Claude Code:** install it in WSL and run `claude` from `~/alto-ocr`;
`CLAUDE.md` loads automatically. Ask it to read this file first.

**Optional, for handwriting (glm-ocr):** install Ollama (in WSL, or reuse a
Windows-side Ollama through `OLLAMA_HOST`) and `ollama pull glm-ocr:q8_0`.
Leave Ollama's `num_ctx` at its default 4096 (see section 7).

**Network shares:** the collections live on SMB shares (`Y:` =
`\\wl.mydata.purdue.edu\lib_asc\Archives2`, `L:` = collections). In WSL,
mount them, e.g. `sudo mkdir -p /mnt/y && sudo mount -t drvfs Y: /mnt/y`.
Write OCR **output to local disk**, not to the share.

**What differs from the Windows host:**
- Some of the 16 tests skipped on Windows (symlink layout) run on Linux.
- The MAX_PATH problem (section 6, item 2) is Windows-only, so you cannot reproduce it in WSL.
- The SMB replace-retry in `_util.atomic_write_*` exists for Windows; harmless on Linux.
- **The packaged desktop app is a Windows build.** `packaging/build.py` under
  WSL produces a Linux bundle. Rebuild the `.exe` on Windows, and bump
  `__version__` first (patch bump by default).
- `.gitattributes` keeps `*.sh` and `integration/goobi/**` LF, so scripts
  copied from any checkout still run on Linux.

**Checking the browser review UI without Goobi:**

```bash
venv/bin/python -m src.alto_ocr.main serve-review \
    --output-root <output_root> --source-root <preservation_scans> --port 8093
# then open http://127.0.0.1:8093/
```

---

## 4. What the project is (orientation)

OCR archival page scans (TIF, PDF also supported) into **METS/ALTO XML +
plain-text transcripts** for the public archive, then make human
**remediation** of those transcripts tractable. The loop:

```
process/batch (Tesseract, WC/PC confidence in ALTO)
  -> triage (worst-first ranking, CSVs)
  -> review: Tk "Review" tab, or the browser server linked from Goobi
  -> repackage (Text/ + METS regenerated from corrected ALTO)
  -> upload (LibNova)
```

Code map: `main.py` (CLI) · `gui.py` + `review.py` (Tk) · `webreview/`
(FastAPI + vanilla JS) · `pipeline.py`, `walker.py`, `preprocess.py`,
`ocr.py`, `alto_format.py`, `mets.py` (generation) · `remediate.py`
(headless edit/save/repackage core shared by both review UIs) ·
`triage.py`. The output contract, the coordinate-frame rule and every other
load-bearing decision are in `CLAUDE.md`. Don't re-litigate those without
asking.

---

## 5. Goobi: where it stands

**Target:** Goobi workflow **26.06.1** on a new Linux server (Ubuntu 24.04,
Tomcat 10, Java 21, Apache, MariaDB). The old instance lived in a WSL install
(`/opt/digiverso/goobi`, app at `/home/schipp0/alto-ocr`) that the Windows
host cannot reach.

**The kit (`integration/goobi/`) is the source of truth. Change it first,
then the server.**

| piece | state |
|---|---|
| OCR bridge (`run_alto_ocr.sh`), image import (`import_images.sh`), `config/goobi_scripts.xml` | done; verified on the old host |
| Task link to the review UI (`config/goobi_processProperties.xml`) | done; `install.sh --config --review-url URL` fills in the real URL |
| `goobi_config.properties` keys (`config/goobi_config.overrides.properties`) | `dailyDelayJob` only (without it, delay steps never close) |
| Review server (`alto-ocr-review.service`, `apache-ocr-review.conf`) | done; `install.sh --review` installs it, but the Apache conf stays disabled until you fill in its `Auth*` lines |
| Plugins (`plugins.txt`) | listed (file upload, image QA, delay, planned Excel import); **jar name patterns and the delay plugin's config name are unverified** |
| Workflow template `Capture_To_Ingest_v1` (10 steps) | **not captured**; `templates/` is empty |
| `drift_check.sh` | compares a host with the kit, read-only; `--stock` lists customizations not yet captured |

**Building the new server, in order:**

1. Work through `PROVISIONING.md` sections 1 to 4 and 8 (Tomcat on Java 21,
   deduplicating `goobi_config.properties`, LDAP or campus AD, mounts before
   Tomcat). Decide AD versus local LDAP **before** creating users.
2. Clone this repo onto the server **at a tag or merge commit**. The wrapper
   executes the checkout's `src/` directly, so whatever is checked out is
   production.
3. Build `venv/` there (as above), readable and executable by `tomcat`.
4. `sudo integration/goobi/install.sh --all --review-url https://<host>/ocr-review/`
5. Fetch the plugins in `plugins.txt` (releases for 26.06.1).
6. Rebuild `Capture_To_Ingest_v1` in the web UI from the stage table in
   `PROVISIONING.md` section 5, then
   `integration/goobi/export_template.sh Capture_To_Ingest_v1 > integration/goobi/templates/Capture_To_Ingest_v1.sql`
   and commit it.
7. Fill in the Apache `Auth*` lines, `a2enconf apache-ocr-review`, start
   `alto-ocr-review`, restart Tomcat.
8. Run `integration/goobi/drift_check.sh` until it reports no drift; commit
   whatever it finds (captured plugin configs go into `config/`).

**If the new computer is the one that still has the old WSL Goobi
install,** harvest it first: export the template with `export_template.sh`
and run `drift_check.sh --stock <clone of intranda/goobi-workflow at
v26.06.1>/install/config` there. That lists every customization the kit is
missing.

**Open from the 2 September demo prep (status unknown since; check):** the
review server had never been started on the old host; the task link wasn't
installed; all logins were stock `test*` accounts with stock group names;
stage 8 (S3 upload to LibSafe) was specified but not built.

---

## 6. Open work, roughly in priority order

1. **Port the provenance net-count fix** (section 2a) onto current `main`.
   Rebuild it against `remediate.py` as it is now: `delete_word`,
   `verify_word`, `add_word`/`undo_add` and the multi-word delete from 0.10.4
   all touch the counters. Add tests in `tests/test_remediate.py`. Keep
   reading-order moves counted per move (deliberate). The browser review
   already sends at most one operation per word, so the over-count is a Tk
   tab problem, and the fix belongs in `remediate.py`.
2. **Sub-series folders and long paths** (found 2026-09-28; **waiting on your
   decision**). LibSafe adds a sub-series level above Ref IDs with names up to
   126 characters, which pushes Windows paths past MAX_PATH (`LongPathsEnabled=0`
   on the Windows host). The `\\?\` prefix fixes every library except
   Tesseract's input read, so a fix must keep Tesseract's scratch PNG in a
   short local temp dir (today it is written next to the output ALTO, in
   `ocr.py`). Open question: do transcripts live at
   `documentation/ALTOS-Plain Text/<sub-series>/<RefID>/` (an opt-in
   "keep input folder name" output option), or does the sub-series sit above
   both trees? Hazard either way: pointing `batch` one level too high silently
   merges a whole sub-series into one object.
3. **Rebuild the Windows exe** for 0.10.4 if not done (bump to 0.10.5 first if
   anything changed).
4. **Goobi server** (section 5).
5. **Re-OCR old outputs before upload:** anything made before 0.8.3 has word
   boxes in a rotated frame on skewed pages, and anything before 0.8.8 was
   binarized and is measurably worse.
6. **Smaller backlog:**
   - glm-ocr sometimes wraps output in a code fence; `markup.normalize_markup` doesn't strip it yet.
   - Spot-check word boxes in the real public archive viewer when access exists (`scripts/overlay_words.py` was the local stand-in).
   - Suggested tests never written: `ocr.build_engine` (especially num_predict pass-through), `triage.scan_output_root` edge cases, an anisotropic-DPI `reformat` test.
   - Deferred: merge+convert parses each page twice (1–3%); a METS rebuild that keeps dmdSec if the institution adds metadata sections; a pip lock file for reproducible exe builds.
   - Ideas from 2026-10-15 usability testing: "select all flagged words" for bulk-deleting noise; a click on bare scan clearing the selection in the Tk tab (the browser already does).
7. **Growth path for handwriting at scale:** pilot eScriptorium/Kraken on one
   collection so corrections train a model. PoCoTo and Transkribus were
   evaluated and rejected.

---

## 7. Hard-won findings (from Claude's machine-local memory)

Most of these are also in `CLAUDE.md`. They are here because the memory
files don't travel.

**OCR quality (all measured, don't undo without re-measuring):**
- Tesseract is the default engine because it emits per-word confidence; glm-ocr is the per-run choice for handwriting.
- **Binarization OFF** (0.8.8): on three collections mean WC rose (0.83→0.91, 0.58→0.78, 0.80→0.92) and review pages fell (28→6, 9→3, 25→6). The words that vanish are speckle artifacts.
- **Deskew and border-crop are forced OFF** for both engines: ALTO boxes must land on the untouched scan. Deskew fired on 36% of pages and put boxes a line off.
- **`--psm 1` per collection, not default:** it fixes sideways scans (16 of 82 pages in MSA00018) but manufactures noise on blank pages. Afterwards, check that no blank page became `scored`.
- MSA00018 pilot (2026-07-30): 82 pages clean; review queue 25 → 6.
- Low mean WC can mean "no text" (photographs, postcards) rather than handwriting. Check words per page first.
- Some JPEG exports rotate pages through the EXIF orientation tag; the loader applies `exif_transpose`. Garbled-but-structured OCR on an upright-looking page means: check orientation.
- Dense printed material: glm-ocr captures only headlines (9 words vs Tesseract's 1,021 on one SCI00001 page).
- Point Tesseract at the TIFF export and glm-ocr at the JPEG export. Keep the two in separate folders (same stems collide), and keep QC-subset folders out of OCR input trees (duplicate stems abort discovery with a misleading "No TIFs found").

**glm-ocr behavior (`glm-ocr:q8_0` on Ollama):**
- Whole-page model: single-line crops return nothing, while column crops work. The engine transcribes per column block and spreads words over detected lines.
- Longest edge must stay at or under ~1024 px (`GGML_ASSERT` above it, content-dependent even inside the limit; the engine retries smaller).
- Keep `num_ctx` at the default 4096: raising it lets repetition loops run 5× longer. `repeat_penalty` corrupts real repeated text, so it is rejected.
- Loop collapse is exact-periodic plus intra-line and enumerated shapes only (0.9.1). **Never ratio-based de-dup**: it destroyed roll-calls and ledgers. Thresholds came from replaying 4,844 pages; re-run that replay before changing them.
- The chat-commentary stripper is precision-first, because a false match deletes real text.
- A blank verso with show-through can make glm-ocr invent a full page of fluent prose (2,877 words on MSF00327 p. 79). Prefer Tesseract where blank versos are interleaved.
- Accepted limitations: occasional column doubling, garbled math, left-edge clip on spanning titles.
- Production settings measured on an A2000: `--workers 6 --glmocr-num-predict 2048` with `OLLAMA_NUM_PARALLEL=6`.

**Engineering rules:**
- Canonical writes go through `_util.atomic_write_*`; pipeline XML parses go through `alto_format.hardened_parser()`.
- Corrected ALTO is protected: `process`/`batch` refuse objects with review provenance (`--force-reocr` overrides; the GUI never forces).
- Goobi fails a script step on **any** stderr output, even with exit 0. Unknown `{variables}` pass through verbatim. Goobi's own ALTO editor misplaces boxes on mm10 ALTO and never updates `WC`.
- On Windows, Rich crashed on non-ASCII under the cp1252 console; `_util.force_utf8_output` fixes it.

**Preferences:**
- Never touch OneDrive paths, even if one appears in a message.
- Bump `__version__` (patch by default) before any rebuild of the packaged app.

---

## 8. Working conventions

- **Branch, then merge.** Work on a feature branch; merge into `main` with
  `git merge --no-ff` and a `merge: <summary> (vX.Y.Z)` message. Commit
  messages read `area: what changed (vX.Y.Z)`.
- **Tests before push:** the pre-push hook runs the suite. `tests/test_gui.py`
  drives the real Tk app on a hidden root and skips where Tk has no display.
  The browser client has no browser in the test run: `tests/test_webreview_static.py`
  catches renamed element ids, so verify behavior by hand in a browser.
- **`CLAUDE.md` is the durable record.** When a decision is load-bearing,
  write it there with its measurement or source, not only in a commit.
- **Version:** bump `__version__` with user-visible changes; the packaged
  zip is named after it.
- **Goobi:** change `integration/goobi/` first, deploy with `install.sh`,
  confirm with `drift_check.sh`. Shell scripts need the executable bit set in
  git (`git update-index --chmod=+x`); see the note in that README.

### Quick reference

```bash
python -m src.alto_ocr.main process <object_dir> <output_root>
python -m src.alto_ocr.main batch <collection_dir> <output_root> --workers 4
python -m src.alto_ocr.main batch <collection_dir> <output_root> --engine glmocr
python -m src.alto_ocr.main triage <output_root>
python -m src.alto_ocr.main repackage <output_root>
python -m src.alto_ocr.main serve-review --output-root <out> --source-root <scans>
python -m src.alto_ocr.gui
python -m unittest discover -s tests
sudo integration/goobi/install.sh --all --review-url https://<host>/ocr-review/
integration/goobi/drift_check.sh
```
