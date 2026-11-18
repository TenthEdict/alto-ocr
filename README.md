# alto-ocr

OCR for digitized archival page scans (TIF, plus PDF) that produces
**METS/ALTO XML + plain-text transcripts** for the public digital archive. It
also includes a review workflow (desktop GUI or browser) for correcting the
transcripts.

For each object folder (Ref ID) it writes:

```
<output_root>/<RefID>/
├── ALTO/   image001.xml …
├── Text/   image001.txt …
└── <RefID>.mets.xml
```

Source TIFs are not copied into the output. The user guides are in `docs/`.
Design notes and the output contract are in `CLAUDE.md` and
`instructions.txt`.

---

## Setup: Windows

1. **Install Python 3** from <https://www.python.org/downloads/>. In the
   installer, tick **"Add python.exe to PATH"**. Don't use the Microsoft Store
   "python" stub.

2. **Install Tesseract 5+** with the UB-Mannheim installer:
   <https://github.com/UB-Mannheim/tesseract/wiki>.
   The tool finds the standard install locations
   (`C:\Program Files\Tesseract-OCR\` and the per-user location) by itself.
   If you install it somewhere else, either add it to PATH, set the
   `TESSERACT_PATH` environment variable, or pass
   `--tesseract-path "D:\path\to\tesseract.exe"`.

3. **Clone and install** (in PowerShell or Command Prompt):

   ```bat
   git clone https://github.itap.purdue.edu/schipp0/alto-ocr.git
   cd alto-ocr
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   If PowerShell blocks `activate`, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

4. **Check the environment:**

   ```bat
   python -m src.alto_ocr.main doctor
   ```

## Setup: Linux / WSL

1. **Install system packages** (Debian/Ubuntu):

   ```bash
   sudo apt update
   sudo apt install python3 python3-venv python3-tk tesseract-ocr git
   ```

   `python3-tk` is needed only for the GUI.

2. **Clone and install:**

   ```bash
   git clone https://github.itap.purdue.edu/schipp0/alto-ocr.git
   cd alto-ocr
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Check the environment:**

   ```bash
   python -m src.alto_ocr.main doctor
   ```

All Python dependencies install from pip wheels on both platforms, with no
other system libraries needed. Tesseract is required only for OCR runs
(`process`/`batch`). `triage`, `repackage` and the Review tab work without it.

### Optional extras

| File | Install when you need |
|---|---|
| `requirements-web.txt` | the browser review server (`serve-review`) |
| `requirements-dev.txt` | building the PyInstaller desktop bundle (`python packaging/build.py`) |

---

## Running

Run commands from the repo root with the venv activated. The tool runs as a
module and is not installed as a package. Windows users can use `^` instead
of `\` for line continuation.

```bash
# Desktop GUI (Run + Review tabs)
python -m src.alto_ocr.gui

# One object
python -m src.alto_ocr.main process <object_dir> <output_root>

# Every object folder in a collection
python -m src.alto_ocr.main batch <collection_dir> <output_root> --workers 4

# Rank output worst-first for correction (also writes triage_*.csv)
python -m src.alto_ocr.main triage <output_root>

# After correcting ALTO: regenerate Text/ and METS
python -m src.alto_ocr.main repackage <output_root>

# Browser review server (needs requirements-web.txt)
python -m src.alto_ocr.main serve-review --output-root output/ --source-root <preservation_dir>
```

Add `--help` to any command to see its options. Examples include the
preprocessing switches, `--psm`, `--ocr-lang` and `--no-mets`.

---

## Development

```bash
# Run the test suite (Tk tests skip when there is no display)
python -m unittest discover tests

# Enable the pre-push hook that runs the tests (once per clone)
git config core.hooksPath scripts/hooks
```

Bump `__version__` in `src/alto_ocr/__init__.py` before building a release.

---

## Goobi workflow setup (Linux server only)

In production the pipeline runs as an automatic script step inside
[Goobi workflow](https://docs.goobi.io/en/home). A browser review server is
linked from the Transcript Correction task. Everything this repo adds to a
stock Goobi lives in [`integration/goobi/`](integration/goobi/), and
`install.sh` applies it to a host.

The kit is verified against the Goobi release in
`integration/goobi/GOOBI_VERSION` (26.06.1) on Ubuntu 24.04, Tomcat 10 and
OpenJDK 17+. The steps below are an outline. The linked documents are the
full reference, and each step depends on details recorded there.

1. **Build the Goobi host** (Goobi, Tomcat, Apache, MariaDB, LDAP). Goobi's
   own installer targets an older stack and breaks on Ubuntu 24.04. Work
   through [`PROVISIONING.md`](integration/goobi/PROVISIONING.md) as a
   checklist. It covers the Tomcat start failures, the duplicated config
   file, LDAP, mounts and host suitability. Use a real VM, not WSL2, for
   shared use.

2. **Deploy a pinned checkout of this repo.** Goobi runs `<app-dir>/src`
   directly, so check out a tag or merge commit, not a development working
   tree. On the server the venv must be named `venv` (not `.venv`):

   ```bash
   sudo apt install tesseract-ocr python3-venv
   git clone https://github.itap.purdue.edu/schipp0/alto-ocr.git /srv/alto-ocr
   cd /srv/alto-ocr && git checkout <tag-or-commit>
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt -r requirements-web.txt
   ```

   The `tomcat` user must be able to read and execute the app and venv. Check
   it as that user:

   ```bash
   sudo -u tomcat env PYTHONPATH=/srv/alto-ocr/src /srv/alto-ocr/venv/bin/python -c "import alto_ocr"
   ```

3. **Install the kit** from that checkout:

   ```bash
   sudo integration/goobi/install.sh --all --app-dir /srv/alto-ocr \
       --review-url https://<goobi-host>/ocr-review/
   ```

   This installs the OCR wrapper and its `goobi_scripts.xml` registration,
   the config overlay, the task link and the review server's systemd unit and
   Apache conf. It is idempotent and backs up anything it replaces. Set
   `ALTO_OCR_APP_DIR=/srv/alto-ocr` in `/etc/default/tomcat10`, because the
   wrapper's default is the original developer's checkout. It does **not**
   restart services, enable the review server's Apache conf or import
   templates. Those steps are left to you:
   - Fill in the `Auth*` lines in `apache-ocr-review.conf` to match Goobi's
     vhost, then `a2enconf apache-ocr-review`. The review server
     authenticates nobody, so never expose it without them.
   - Install the plugins listed in `integration/goobi/plugins.txt`.
   - Import the process templates as described in
     `integration/goobi/templates/README.md`.
   - Restart Tomcat and Apache, and start `alto-ocr-review`.

4. **Configure the OCR task** in the process template: Script step =
   `ALTO-OCR Processing`, Automatic task = `Processing queue for slow jobs`,
   Read images checked.

5. **Verify** as `tomcat`. Both the exit code and the stderr byte count must
   be 0, because any stderr output fails a Goobi step:

   ```bash
   sudo -u tomcat /opt/digiverso/goobi/scripts/run_alto_ocr.sh \
       999 TestRef /path/to/tifs /tmp/ocr-check 2>/tmp/ocr-check.err
   echo "exit=$?  stderr_bytes=$(wc -c < /tmp/ocr-check.err)"
   ```

6. **Keep the server and repo in sync.** Change the kit first, then the
   server. `integration/goobi/drift_check.sh` reports anything that differs.

These are covered in
[`integration/goobi/README.md`](integration/goobi/README.md):
- Creating processes in bulk from tracking sheets (`tracking_to_goobi.py`)
- The environment variables that tune OCR (`ALTO_OCR_LANG`,
  `ALTO_OCR_EXTRA_ARGS`, …)
- The output layout under `<process>/ocr/`
- Why the wrapper is built the way it is
