Alto OCR — quick start
======================

This bundle contains everything needed to OCR archival TIF scans into
METS/ALTO XML + plain-text transcripts, and to review and correct the
results — EXCEPT for the OCR engines themselves (Tesseract, optionally
GLMOCR via Ollama), which are separate one-time installs.

What's in this folder
---------------------
  alto-ocr-gui.exe   <- DOUBLE-CLICK THIS to launch the desktop app.
  alto-ocr.exe       <- Optional command-line interface (advanced users).
  _internal\         <- Bundled Python runtime + dependencies. Do not
                        delete or move; both .exe files need it.
  README.txt         <- This file.

One-time setup (per machine)
----------------------------
1. Install Tesseract 5+ (this is the default engine — used for
   everything unless you explicitly pick GLMOCR):

   - Download the UB-Mannheim Windows installer:
       https://github.com/UB-Mannheim/tesseract/wiki
   - Run it. The default install location
       C:\Program Files\Tesseract-OCR\
     is auto-detected by Alto OCR even when it's not on PATH.

   (If you install Tesseract somewhere non-standard, you can point
   Alto OCR at it from the Run tab's "Tesseract path" field, or pass
   --tesseract-path on the CLI.)

2. (Optional) Install Ollama and pull the glm-ocr model if you need
   the GLMOCR engine for handwriting-heavy material — Tesseract tends
   to produce low-confidence garble on handwriting:

       https://ollama.com/download
       ollama pull glm-ocr:q8_0

   By default Alto OCR talks to http://localhost:11434. The Advanced
   settings tab has an "Endpoint" field if Ollama runs on a different
   machine.

Running the GUI
---------------
  Double-click alto-ocr-gui.exe.

  Run tab:
    - Pick the folder of TIF scans (or a parent folder containing many
      Ref ID subfolders, with "OCR all subfolders" checked).
    - Pick an output folder (auto-fills next to the input by default).
    - Choose the OCR engine: Tesseract (default — also records per-word
      confidence values in the ALTO for later transcript review) or
      GLMOCR (for handwriting-heavy material; needs a local Ollama).
    - Click "Run OCR".

  Advanced settings tab:
    - Tune preprocessing stages (deskew, denoise, CLAHE, binarize,
      border crop) and OCR options (language, PSM, OEM).
    - Configure GLMOCR endpoint / model / timeout if using GLMOCR
      for handwritten material.
    - All settings persist across launches in %USERPROFILE%\.alto_ocr.json.

  Review tab (correct transcripts after a run):
    - Pick the OCR output object folder (the one containing ALTO\) and
      the matching source-image folder (the preservation TIFs — they
      are not copied into the output).
    - The page list sorts worst-first by page confidence (PC). Pick a
      page to see the scan with word boxes colored by confidence
      (red = low, green = high).
    - Click a word — or press n / p to step through flagged words —
      and correct it in the editor bar; a magnified snippet shows the
      scan region. Enter commits and jumps to the next flagged word,
      Ctrl+Z undoes, "Delete word" removes a hallucinated word.
    - "Add word (a)": drag a box around a word the OCR missed, type
      it, press Enter. The glyph palette under the editor inserts
      math/engineering characters you can't type on a US keyboard.
    - "Clear page…": removes ALL words on the page in one step. Use it
      when the whole page is hallucinated OCR — covers, photographs,
      or blank pages where the engine read texture or noise as
      thousands of garbage words. The page becomes empty (and sorts
      as such in the review queue); words it actually has can then be
      added back by hand with "Add word".
    - Zoom: the dropdown, or Ctrl+scroll on the image (zooms around
      the mouse cursor). Plain scroll pans vertically, Shift+scroll
      pans horizontally.
    - "Save page" writes the corrected ALTO and keeps the plain-text
      transcript in sync. Corrected pages are protected: a later OCR
      run refuses to overwrite that object unless explicitly forced.

Running the CLI
---------------
  Open a terminal in this folder and run, for example:

    alto-ocr.exe process   "C:\path\to\object" "C:\path\to\output"
    alto-ocr.exe batch     "C:\path\to\collection" "C:\path\to\output"
    alto-ocr.exe triage    "C:\path\to\output"
    alto-ocr.exe repackage "C:\path\to\output"
    alto-ocr.exe doctor

  triage ranks existing output worst-first for review and writes
  triage_objects.csv / triage_pages.csv next to the outputs.
  repackage regenerates the transcripts + METS from corrected ALTO
  (run it after editing ALTO in an external tool).

  alto-ocr.exe --help            (overview)
  alto-ocr.exe process --help    (process-command flags)
  alto-ocr.exe batch --help      (batch-command flags)

Output layout
-------------
For each Ref ID processed, you get:

  <output>\<RefID>\ALTO\         OCR result, one ALTO XML per page
  <output>\<RefID>\Text\         plain-text transcripts
  <output>\<RefID>\<RefID>.mets.xml

The source TIFs are read but NOT copied into the output. The canonical
preservation copies in preservation-files\preservation\<RefID>\ remain
the upload-ready masters; duplicating them here would just waste storage
on the network drive. LibNova pairs the preservation tree and the
ALTO/Text output tree by Ref ID externally.

Troubleshooting
---------------
  "Tesseract not found"
    The default engine isn't installed, or isn't somewhere Alto OCR
    can see it. Install the UB-Mannheim build (see step 1 above), or
    point at an existing install via the Tesseract path field.

  "GLMOCR endpoint check failed"
    Only matters when you've selected GLMOCR. Make sure Ollama is
    running and the glm-ocr:q8_0 model is pulled (see step 2 above).
    The Advanced tab's Endpoint field accepts a remote URL like
    http://other-machine:11434 if Ollama runs elsewhere.

  Antivirus flags the bundle
    PyInstaller binaries are sometimes false-positive flagged. The
    bundle is just Python + this app's source code; have your IT
    whitelist the folder if needed.
