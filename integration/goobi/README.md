# Goobi workflow integration

Runs the ALTO-OCR pipeline as an automatic script step inside
[Goobi workflow](https://docs.goobi.io/en/home). Verified against **Goobi
workflow 26.06.1** on Ubuntu 24.04 / Tomcat 10 / OpenJDK 17.

| file | installs to |
|---|---|
| `run_alto_ocr.sh` | `/opt/digiverso/goobi/scripts/run_alto_ocr.sh` |
| `goobi_scripts.xml` | `/opt/digiverso/goobi/config/goobi_scripts.xml` |
| `install.sh` | — (puts the other two in place) |
| `PROVISIONING.md` | — (host build notes: what Goobi's own installer gets wrong) |
| `import_images.sh` | `/opt/digiverso/goobi/scripts/import_images.sh` |
| `export_template.sh` | — (dumps a process template as re-importable SQL) |

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

```sh
sudo integration/goobi/install.sh            # app dir defaults to this repo
sudo integration/goobi/install.sh --app-dir /srv/alto-ocr
sudo systemctl restart tomcat10              # goobi_scripts.xml is read at startup
```

`install.sh` **replaces** `goobi_scripts.xml` rather than merging into it —
Goobi's sample entries (Tiff-Header, Copy images, Compression) are dropped on
purpose. Merge by hand on a host that has its own entries.

Requires the pipeline's venv at `<app-dir>/venv`, Tesseract on `PATH`, and
`--ref-id` support in the CLI (v0.8.15+). The venv must be readable *and
executable by the `tomcat` user* — verify with `sudo -u tomcat`, not as
yourself.

Building the Goobi host itself is a separate problem with its own set of
undocumented traps: see [PROVISIONING.md](PROVISIONING.md).

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
