Alto OCR — quick start
======================

This bundle contains everything needed to OCR archival TIF scans into
METS/ALTO XML + plain-text transcripts, EXCEPT for the OCR engines
themselves (GLMOCR via Ollama, optionally Tesseract), which are separate
one-time installs.

What's in this folder
---------------------
  alto-ocr-gui.exe   <- DOUBLE-CLICK THIS to launch the desktop app.
  alto-ocr.exe       <- Optional command-line interface (advanced users).
  _internal\         <- Bundled Python runtime + dependencies. Do not
                        delete or move; both .exe files need it.
  README.txt         <- This file.

One-time setup (per machine)
----------------------------
1. Install Ollama and pull the glm-ocr model (this is the default
   engine — used for everything unless you explicitly pick Tesseract):

       https://ollama.com/download
       ollama pull glm-ocr:q8_0

   By default Alto OCR talks to http://localhost:11434. The Advanced
   settings tab has an "Endpoint" field if Ollama runs on a different
   machine.

2. (Optional) Install Tesseract 5+ if you want the fallback engine
   available (faster setup, lower accuracy on this institution's
   material, but supports non-English language packs):

   - Download the UB-Mannheim Windows installer:
       https://github.com/UB-Mannheim/tesseract/wiki
   - Run it. The default install location
       C:\Program Files\Tesseract-OCR\
     is auto-detected by Alto OCR even when it's not on PATH.

   (If you install Tesseract somewhere non-standard, you can point
   Alto OCR at it from the Run tab's "Tesseract path" field, or pass
   --tesseract-path on the CLI.)

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

Running the CLI
---------------
  Open a terminal in this folder and run, for example:

    alto-ocr.exe process "C:\path\to\object" "C:\path\to\output"
    alto-ocr.exe batch   "C:\path\to\collection" "C:\path\to\output"
    alto-ocr.exe doctor

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
  "GLMOCR endpoint check failed"
    The default engine couldn't reach Ollama. Make sure Ollama is
    running and the glm-ocr:q8_0 model is pulled (see step 1 above).
    The Advanced tab's Endpoint field accepts a remote URL like
    http://other-machine:11434 if Ollama runs elsewhere.

  "Tesseract not found"
    Only matters when you've selected Tesseract. Install the
    UB-Mannheim build (see step 2 above), or point at an existing
    install via the Tesseract path field.

  Antivirus flags the bundle
    PyInstaller binaries are sometimes false-positive flagged. The
    bundle is just Python + this app's source code; have your IT
    whitelist the folder if needed.
