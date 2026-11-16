# Goobi workflow integration

Runs the ALTO-OCR pipeline as an automatic script step inside
[Goobi workflow](https://docs.goobi.io/en/home). Verified against **Goobi
workflow 26.06.1** on Ubuntu 24.04 / Tomcat 10 / OpenJDK 17.

This folder is the **provisioning kit** for a Goobi host: everything this
institution adds to a stock Goobi lives here, and `install.sh` applies it to a
fresh server from a checkout of this repo. `drift_check.sh` then keeps the
server and the repo honest with each other.

| file | installs to |
|---|---|
| `GOOBI_VERSION` | — (the Goobi release the kit is verified against) |
| `run_alto_ocr.sh` | `/opt/digiverso/goobi/scripts/run_alto_ocr.sh` |
| `import_images.sh` | `/opt/digiverso/goobi/scripts/import_images.sh` |
| `config/goobi_scripts.xml` | `/opt/digiverso/goobi/config/goobi_scripts.xml` |
| `config/goobi_processProperties.xml` | `/opt/digiverso/goobi/config/` — the task link, URL filled in at install |
| `config/goobi_config.overrides.properties` | keys set in `/opt/digiverso/goobi/config/goobi_config.properties` |
| `config/<anything else>` | `/opt/digiverso/goobi/config/` as is (captured plugin configs go here) |
| `alto-ocr-review.service` | `/etc/systemd/system/` — app path filled in at install |
| `apache-ocr-review.conf` | `/etc/apache2/conf-available/` |
| `plugins.txt` | — (the plugins the workflow needs; fetched by hand) |
| `templates/*.sql` | — (process templates as SQL; imported by hand) |
| `install.sh` | — (applies all of the above) |
| `drift_check.sh` | — (reports where a host and the kit disagree) |
| `lib.sh` | — (shared by the two scripts above) |
| `export_template.sh` | — (dumps a process template as re-importable SQL) |
| `PROVISIONING.md` | — (host build notes: what Goobi's own installer gets wrong) |

> Adding a script here: this repo has `core.fileMode false`, so `chmod +x`
> is invisible to git. Set the bit in the index explicitly, or the committed
> copy cannot be executed:
> `chmod +x integration/goobi/your_script.sh && \`
> `git update-index --chmod=+x integration/goobi/your_script.sh`
>
> Both halves are needed: `update-index` sets the mode git records, and
> `chmod` sets the mode on disk. With `core.fileMode false` neither implies
> the other, and a checkout will not restore the bit either.

These live here because they are the only artifacts that encode how the two
systems join. Reconstructing them from memory on a new host means
rediscovering every constraint in the *Why the wrapper looks like this*
section below.

## Install

On the Goobi host, from a checkout of this repo at a tag or merge commit (the
wrapper runs the checkout's `src/` directly, so the checkout *is* production;
see PROVISIONING.md section 10):

```sh
sudo integration/goobi/install.sh                       # the OCR bridge only
sudo integration/goobi/install.sh --all \
    --review-url https://<goobi-host>/ocr-review/       # bridge + config + review server
sudo integration/goobi/install.sh --app-dir /srv/alto-ocr ...   # app not in this checkout
```

| flag | adds |
|---|---|
| (none) | the two scripts and `goobi_scripts.xml` |
| `--config` | the rest of `config/`, the task link rendered with `--review-url`, and the keys in `goobi_config.overrides.properties` |
| `--review` | the review server's systemd unit (enabled, **not** started) and Apache conf (installed, **not** enabled) |
| `--all` | `--config` and `--review` |

It is idempotent: a file that already matches is left alone, and one that
differs is copied to `<file>.bak-<UTC time>` before it is replaced, so
re-running after a `git pull` installs exactly what changed. It never
restarts Tomcat or Apache, never starts the review service, and never touches
the database; it prints those as next steps instead. Two of them are
deliberate gates:

- **The Apache conf is not enabled for you.** The review server rewrites
  ALTO and authenticates nobody, so its `Auth*` lines must be filled in to
  match Goobi's before `a2enconf apache-ocr-review`.
- **Templates are not imported for you.** A template import with a missing
  dependency succeeds and then fails at process creation (PROVISIONING.md
  section 7); see `templates/README.md`.

`goobi_scripts.xml` is **replaced** rather than merged: Goobi's sample entries
(Tiff-Header, Copy images, Compression) are dropped on purpose, and the backup
keeps a host's own entries for merging by hand. Keys in
`goobi_config.overrides.properties` are set in place, and a key set several
times is collapsed to one line, which also clears the duplicated-file problem
in PROVISIONING.md section 2 for those keys.

Requires the pipeline's venv at `<app-dir>/venv`, Tesseract on `PATH`, and
`--ref-id` support in the CLI (v0.8.15+); `--review` also needs
`requirements-web.txt` in that venv, and refuses to enable a unit whose first
start would crash. The venv must be readable *and executable by the `tomcat`
user*. Verify that with `sudo -u tomcat`, not as yourself.

Plugins are fetched by hand: `plugins.txt` names each one, its intranda
repository and the config file it needs. Install releases built for the
version in `GOOBI_VERSION`.

Building the Goobi host itself is a separate problem with its own set of
undocumented traps: see [PROVISIONING.md](PROVISIONING.md).

## Keeping a server and this repo in sync

The rule: **change the kit first, then the server.** A setting changed only
in Goobi's web interface or a config file on the server is drift, and the
next fresh install silently loses it.

```sh
integration/goobi/drift_check.sh            # everything; exit 1 on drift
integration/goobi/drift_check.sh --no-db    # skip re-exporting templates
integration/goobi/drift_check.sh -q         # only the problems
```

It renders every managed file exactly as `install.sh` would and compares, so
the two cannot disagree. It checks the deployed Goobi version against
`GOOBI_VERSION`, the scripts and `config/` files, each override key, the
review unit and Apache conf (with the `Auth*` lines set aside, since filling
them in is the expected local change; a conf with no active `Require` line is
reported as open), installed plugin jars against `plugins.txt` in both
directions, and each `templates/*.sql` against a fresh export. It changes
nothing on the host.

For each item it reports, either commit the server's version into the kit,
or re-run `install.sh` to put the kit's back. Run it after any change made
through the web interface (a template edited, a task added), and before and
after a Goobi upgrade.

**Harvesting a server that predates the kit.** Point `--stock` at the stock
config of the same Goobi release (`install/config` in a clone of
`intranda/goobi-workflow` at the `GOOBI_VERSION` tag):

```sh
git clone --depth 1 --branch v$(cat integration/goobi/GOOBI_VERSION) \
    https://github.com/intranda/goobi-workflow.git /tmp/goobi-src
integration/goobi/drift_check.sh --stock /tmp/goobi-src/install/config
```

It then also lists every config file that differs from stock or is not in
stock (plugin configs), and every `goobi_config.properties` line changed from
stock, that `config/` does not yet hold. Those are the customizations nobody
has captured; copy each one into `config/` (or the key into the overrides
file) and commit.

The Goobi source itself does not belong in this repo. It is only ever needed
as a reference for what Goobi does, and a shallow clone at the pinned tag
answers that without vendoring a few thousand files that would drift from
upstream.

## Creating processes from a tracking sheet

Processes are mass-created from the digitization tracking spreadsheets
with Goobi's Excel import plugin (`goobi-plugin-import-excel`, see
`plugins.txt`). The tracking sheets cannot be uploaded as they are: their
Ref ID column has no collection prefix, their headers vary from sheet to
sheet, and the plugin turns *any* non-empty row, a totals row included,
into a process. `tracking_to_goobi.py` converts them:

```sh
pip install -r integration/goobi/requirements.txt     # openpyxl
python integration/goobi/tracking_to_goobi.py --collection MSA00005 \
    msa5-box_103-105_digitization_tracking.xlsx \
    msa5-box_106-108_digitization_tracking.xlsx \
    -o goobi_import/MSA00005_goobi_import.xlsx
```

- **Process title** is `<collection>_<RefID>`, the name `import_images.sh`
  and the OCR step key on. The collection is given per run, never guessed
  from a file name.
- **Skipped, and listed:** Ref IDs that already have a process on this host
  (read from the metadata tree), rows with a *Date Digitized* (already past
  Capture; `--include-started` keeps them), repeated Ref IDs, rows without
  a Title or a folder-safe Ref ID, rows without a Ref ID at all.
- **Flagged, not skipped:** a Ref ID shaped unlike the rest of its sheet,
  e.g. `16354` among 32-character hashes. Check it before importing.
- `--dry-run` prints the report and writes nothing.

Then in Goobi: **Processes → Mass import**, pick the collection's
production template, *Upload file*, plugin `intranda_import_excel`, upload.
`config/plugin_intranda_import_excel.xml` (installed by `install.sh
--config`) maps the columns: Title to `TitleDocMain`, the title to
`CatalogIDDigital`, the Ref ID to `CatalogIDSource`, and Box, Folder,
Series, Subseries, SubGroup, TN, Notes and Collection to process
properties. Import runs as a GoobiScript; existing processes are never
replaced.

**Project = template.** The plugin has no per-row project: every process
lands in the project of the template picked for the import. For a project
per collection, give each project its own copy of the template and import
each collection's sheet against its own template.

## Task configuration in Goobi

Workflow → Process templates → edit the template → the OCR task:

- **Script step**: checked, and **Script** set to `ALTO-OCR Processing`
- **Automatic task**: `Processing queue for slow jobs`
- **Read images**: checked

Goobi closes an automatic step itself when the script exits 0, so there is no
separate "close on success" toggle in 26.06. Follow it with a manual task for
transcript QA, assigned to the reviewing user group.

## Output layout

Under `{ocrbasispath}` (= `<process>/ocr/`), for process title `T`:

```
T_alto/                    real dir — Goobi's canonical ALTO folder
T_txt/                     real dir — Goobi's canonical plain-text folder
alto_ocr/
  T/ALTO -> ../../T_alto
  T/Text -> ../../T_txt
  T/T.mets.xml             LibNova-style per-object METS
```

Goobi's folders hold the real files; `alto_ocr/` is the view this project's
own commands understand (`triage`, `repackage`, the review GUI all expect
`<root>/<RefID>/ALTO`). Because `ALTO/` and `Text/` are symlinks onto Goobi's
folders, both toolchains read and write the same bytes — no copies, and the
pipeline's review-provenance guard sees corrections made through Goobi.

Post-QA, `repackage` regenerates transcripts and refreshes the METS from the
corrected ALTO:

```sh
alto_ocr repackage <process>/ocr/alto_ocr
```

## Why the wrapper looks like this

Four constraints, each found in the Goobi source rather than the docs. Line
numbers are from the `goobi-workflow` repo at the 26.06 tag.

**1. `{tifffolder}` and `{ocrfolder}` do not exist.** Unknown tokens are
passed through verbatim, so a script gets the literal string `{tifffolder}`
as a path. The real names are compiled in
`VariableReplacer.java:89-119` — the ones that matter here are `{tifpath}`,
`{origpath}`, `{imagepath}`, `{processpath}`, `{ocrbasispath}`,
`{ocrplaintextpath}`, `{processtitle}`, `{processid}`. There is **no** ALTO
folder token; the wrapper derives that path itself. `$(name)` and `{name}`
are both accepted. The wrapper hard-fails on an argument that still looks
like a variable, so this misconfiguration can't reach the OCR stage.

**2. Anything on stderr fails the step, even on exit 0.**
`ShellScript.callShell` (`ShellScript.java:265`) overwrites the return code
with `ERRORLEVEL_ERROR` when stderr is non-empty. One Python warning would
mark every successful run as failed. The wrapper therefore sends *everything*
except a single summary line to its log file, and reports failures on stdout
with a non-zero exit rather than on stderr.

**3. Quoted arguments are safe.** `VariableReplacer.replaceBashScript` splits
the command with a `StringTokenizer` honouring `"` as a quote character, and
the parts are handed to `ProcessBuilder` as an argv list — no shell in
between. So `"{processtitle}"` survives a title containing spaces, and no
shell metacharacter in a title can be interpreted. The script itself is the
executable, so it needs `+x` and a shebang.

**4. Goobi's folder names are configurable but conventional.**
`ConfigurationHelper.java:395-407` defaults OCR output to
`{processtitle}_txt` and `{processtitle}_alto`; `Process.getOcrAltoDirectory`
is what the metadata editor, PDF export and REST media resource read. Note
`{tifpath}` resolves to `images/{processtitle}_media` on 26.06 (*not* `_tif`
— that name is historical), and `{origpath}` is `images/{processtitle}_master`,
the preservation copy. None of these accessors create directories; the
wrapper does.

A fifth constraint comes from this project rather than Goobi: the Ref ID is
the input folder's *resolved* basename, which would make it `T_media`. The
wrapper passes `--ref-id "{processtitle}"` so the Ref ID is the object's
identity in the output folder name and the METS OBJID/LABEL.

## Operating it

Tuning knobs, all read from the environment (set them in
`/etc/default/tomcat10` so Tomcat's children inherit them):

| variable | default | |
|---|---|---|
| `ALTO_OCR_APP_DIR` | `/home/schipp0/alto-ocr` | where the app + venv live |
| `ALTO_OCR_ENGINE` | `tesseract` | `glmocr` for handwriting (needs Ollama) |
| `ALTO_OCR_LANG` | `eng` | Tesseract language code |
| `ALTO_OCR_LOG_DIR` | `/opt/digiverso/logs` | per-process logs + summary JSON |
| `ALTO_OCR_EXTRA_ARGS` | — | e.g. `--psm 4 --binarize`, or `--no-mets` |

Per run: `${ALTO_OCR_LOG_DIR}/alto_ocr_<processid>.log` (full detail) and
`alto_summary_<processid>.json` (page counts, confidence). Goobi's process
journal gets the one-line verdict.

**Re-running the step over corrected pages fails by design.** The pipeline
refuses to overwrite ALTO carrying `<Processing ID="review">` provenance, so
the step errors instead of destroying human work. Discard the corrections
deliberately with `ALTO_OCR_EXTRA_ARGS="--force-reocr"`, or move the output
folder aside.

## Verifying an install

```sh
sudo -u tomcat /opt/digiverso/goobi/scripts/run_alto_ocr.sh \
    999 TestRef /path/to/tifs /tmp/ocr-check 2>/tmp/ocr-check.err
echo "exit=$?  stderr_bytes=$(wc -c < /tmp/ocr-check.err)"
```

Both must be zero: a non-empty stderr passes here but fails inside Goobi
(constraint 2). Run as `tomcat`, not as yourself — the app directory and venv
have to be readable by the Tomcat user, which is the usual first-install trip
hazard when the app lives under `/home`.

## The review server (`serve-review`)

A browser front end for transcript correction, linked from the **Transcript
Correction** task. Same edit semantics as the Tk review GUI — both are thin
layers over `remediate.py` — so provenance stamping, `WC="1.00"`, `PC`
recomputation and transcript regeneration are identical whichever UI a page
was corrected in.

It edits Goobi's own `_alto` / `_txt` folders through the
`ocr/alto_ocr/<RefID>/` symlink view, so there is no second copy to
reconcile, and a page corrected here is immediately what Goobi serves.

```sh
pip install -r requirements-web.txt          # fastapi + uvicorn, web only
python -m alto_ocr.main serve-review         # → http://127.0.0.1:8081/
```

Deploy with the three files beside this README:

| file | install to |
|---|---|
| `alto-ocr-review.service` | `/etc/systemd/system/` — runs as `tomcat`, binds localhost |
| `apache-ocr-review.conf` | `/etc/apache2/conf-available/` — proxies `/ocr-review/` |
| `config/goobi_processProperties.xml` | `/opt/digiverso/goobi/config/` — the task link |

`install.sh --review` and `--config --review-url URL` put all three in
place; the manual steps are kept here for reference.

**Read this before exposing it.** The server rewrites ALTO and authenticates
nobody. It binds to `127.0.0.1` so Apache is the only route in; fill in the
commented `Auth*` lines in `apache-ocr-review.conf` to match Goobi's own vhost
before giving anyone the URL. `--read-only` disables saving entirely.

### Two Goobi facts that shaped it

1. **Goobi renders a process property of type `link` as an anchor** on the
   task screen (`uii/template/includes/task/box_properties.xhtml`), so the
   link needs no plugin and no patch. A property with type `html` is rendered
   with `escape="false"` if a styled button is ever wanted instead.
2. **Property values are not variable-replaced.** Neither `PropertyParser`
   nor `GoobiScriptPropertySet` runs a `VariableReplacer`, so `{processtitle}`
   in a property value reaches the browser verbatim. Hence one constant URL to
   the app's own worklist, which finds objects by walking
   `metadata/<processid>/ocr/alto_ocr/<RefID>/` for itself.

### Why not use Goobi's own ALTO editor

It writes `CONTENT` only (`AltoSaver.java:91-94`) — `WC` keeps its engine
value, so a corrected word never leaves the low-confidence queue — and it
reads `HPOS`/`WIDTH` as pixels with no `<MeasurementUnit>` handling
(`SimpleAlto.java:127-150`), so on this institution's `mm10` ALTO every
highlight box lands 1.968× small toward the top-left of the word it is
supposed to mark.

### Environment overrides

The systemd unit passes CLI flags, but every knob also reads from the
environment, so a unit drop-in can change one without rewriting `ExecStart`:

| variable | default | |
|---|---|---|
| `ALTO_OCR_REVIEW_GOOBI_ROOT` | `/opt/digiverso/goobi/metadata` | Goobi-managed objects |
| `ALTO_OCR_REVIEW_OUTPUT_ROOT` | — | standalone output root instead |
| `ALTO_OCR_REVIEW_SOURCE_ROOT` | — | preservation scans for the above |
| `ALTO_OCR_REVIEW_CACHE_DIR` | system temp | rendered page JPEGs |
| `ALTO_OCR_REVIEW_LOW_WC` | `0.50` | default "Flag below"; each corrector can change it on the page (remembered per browser) |
| `ALTO_OCR_REVIEW_PAGE_PX` | `2400` | full-page render cap (long edge) |
| `ALTO_OCR_REVIEW_WORKING_PX` | `4000` | cached working-copy cap |
| `ALTO_OCR_REVIEW_READ_ONLY` | `false` | serve without the save path |

### What a corrector gets

The same tools as the desktop Review tab: worst-first page list, word boxes
coloured by confidence with a **Flag below** threshold and an **All words**
toggle, the word editor with its symbol keyboard (hideable), delete without
a prompt, **Undo**, **Add word**, **Clear page**, **◀ Page / Page ▶**, and
zoom by dropdown (Fit page, Fit width, percentages) or Ctrl+scroll about the
pointer. An **Auto / Light / Dark** switch in the header sets the
appearance (Auto follows the system setting); each browser remembers it.

**Reading order** fixes transcripts whose words are right but ordered wrong
— two columns read straight across, a caption read into a paragraph. Select
the words out of place (click, Shift+click a run, Ctrl+click to add or drop
one, or drag a box around a column), then click the word they should follow;
"Page start" puts them first. The scan shows the reading path and a side
panel shows the transcript exactly as it will be saved — computed by the
server's preview endpoint, which applies the pending batch with the same
code a save runs, so the browser never re-implements the move.

### Keyboard

`n` / `p` next/previous flagged word · `Enter` correct (or confirm, if the
text is unchanged) and advance · `a` add-word mode · `r` reading-order mode ·
`Ctrl+↑` / `Ctrl+↓` nudge the selected words one word (reading order) ·
`Esc` close the editor or clear the selection · `Ctrl+Z` undo · `Ctrl+S`
save · `Alt+Page Up` / `Alt+Page Down` previous/next page (browsers keep
`Ctrl+Page Up/Down` for their own tabs).

Corrections accumulate in the browser and post as **one batch per save**: one
file write, one provenance update, and nothing held server-side, so a closed
laptop or a restarted service loses only what was not yet saved. The save
carries a digest of the ALTO it was made against; if another session (or the
Tk GUI, or an external editor) saved in the meantime, the server answers 409
and offers to reload or overwrite.
