Alto OCR — quick start
======================

Alto OCR turns archival page scans (TIF, and PDF) into METS/ALTO XML +
plain-text transcripts, then helps you review and correct those
transcripts before upload.

This bundle contains everything needed EXCEPT the OCR engines themselves
(Tesseract, and optionally GLMOCR via Ollama), which are separate
one-time installs — see "One-time setup" below.

Contents
--------
  1. What's in this folder
  2. One-time setup (per machine)
  3. The workflow at a glance
  4. Preparing the input folder
  5. Running OCR (Run tab)
  6. Advanced settings
  7. Correcting transcripts (Review tab)
  8. Finishing up: Repackage
  9. Output layout
 10. Command line (optional)
 11. Troubleshooting


1. What's in this folder
------------------------
  alto-ocr-gui.exe   <- DOUBLE-CLICK THIS to launch the desktop app.
  alto-ocr.exe       <- Optional command-line interface (advanced users).
  _internal\         <- Bundled Python runtime + dependencies. Do not
                        delete or move; both .exe files need it.
  README.txt         <- This file.

  Help > About Alto OCR shows which version you are running. When you
  get a newer bundle, replace the whole folder; your settings live
  elsewhere (%USERPROFILE%\.alto_ocr.json) and carry over.


2. One-time setup (per machine)
-------------------------------
1. Install Tesseract 5+ (the default engine — used for everything
   unless you explicitly pick GLMOCR):

   - Download the UB-Mannheim Windows installer:
       https://github.com/UB-Mannheim/tesseract/wiki
   - Run it. The default install location
       C:\Program Files\Tesseract-OCR\
     is auto-detected by Alto OCR even when it's not on PATH.

   If you install Tesseract somewhere non-standard, point Alto OCR at
   it under Tools > Advanced settings > "Tesseract program", or pass
   --tesseract-path on the command line.

2. (Optional) Install Ollama and pull the glm-ocr model if you need the
   GLMOCR engine for handwriting-heavy material — Tesseract tends to
   produce low-confidence garble on handwriting:

       https://ollama.com/download
       ollama pull glm-ocr:q8_0

   By default Alto OCR talks to http://localhost:11434. Tools >
   Advanced settings has an "Endpoint" field if Ollama runs on a
   different machine.

3. Check the setup. Open a terminal in this folder and run:

       alto-ocr.exe doctor

   It reports OK / WARN for each engine. A GLMOCR warning is fine if
   you only use Tesseract.


3. The workflow at a glance
---------------------------
  a. OCR the objects with Tesseract (Run tab). Tesseract records a
     confidence value for every word and every page.
  b. Look at the results worst-first (Review tab page list, or the
     "triage" command for a whole collection). Objects whose pages are
     almost all low-confidence are usually handwriting: re-run just
     those with the GLMOCR engine.
  c. Correct the low-confidence words in the Review tab. You don't have
     to correct everything — work worst pages first, and stop when the
     remaining pages are good enough.
  d. Click "Repackage" when an object is done, before it is uploaded.

  Corrections are protected: once a page has been corrected in the
  Review tab, a later OCR run will refuse to overwrite that object.


4. Preparing the input folder
-----------------------------
  - One object (Ref ID) = one folder. The folder's name becomes the
    Ref ID: it names the output folder and the METS file.
  - Scans can sit directly in that folder or in sub-folders at any
    depth (e.g. by year or by document); all of them become pages of
    the one object, in natural order ("Page 2" before "Page 10").
  - Accepted files: .tif / .tiff (any capitalization) and .pdf. Each
    PDF page becomes its own page, named <pdfname>_page001,
    <pdfname>_page002, ... Other files (Thumbs.db, .txt, .jpg, ...)
    are ignored.
  - Two files that would produce the same page name (e.g. "a\001.tif"
    and "b\001.tif" in one object) stop that object: it is reported as
    failed and nothing is written for it. Rename one and re-run.
  - Batch mode ("OCR all subfolders"): point it at the folder that
    directly CONTAINS the Ref ID folders. Each immediate subfolder is
    one object. Pointing one level too high (e.g. at a series folder
    that holds several sub-series folders) would treat each whole
    sub-series as a single giant object.
  - Source scans are never modified.


5. Running OCR (Run tab)
------------------------
  Double-click alto-ocr-gui.exe.

  - "Folder to OCR": one Ref ID folder, or — with "OCR all subfolders
    inside the target folder (batch mode)" ticked — a folder of Ref ID
    folders.
  - "Output folder": fills in automatically as "<input name>_output"
    next to the input. Use "Browse..." to pick a different one (for
    example the collection's documentation\ALTOS-Plain Text\ folder).
  - "Output formats": METS / ALTO and Plain text are both on by
    default. Leave them on unless you've been told otherwise.
  - "OCR engine": Tesseract (default — records word confidences for
    review) or GLMOCR (handwriting; needs Ollama). GLMOCR output has no
    confidence values, so its pages show up as "unscored" and need a
    full read-through rather than worst-first correction.
  - Click "Run OCR". Progress and the log appear on this tab. "Cancel"
    finishes the object in progress and skips the rest. The window
    title shows progress too, so you can keep correcting on the Review
    tab while a run goes on.
  - The last log line reports how many objects succeeded and how many
    pages failed. A page that fails still gets placeholder ALTO and
    text files so page counts stay aligned (it sorts first in the
    Review tab); the log says why it failed.


6. Advanced settings
--------------------
  Tools > Advanced settings... (Ctrl+,) opens a separate window.
  Settings are saved when you close it and apply to the next run.
  "Reset to defaults" undoes any experimenting.

  - Tesseract program: leave empty to find it automatically.
  - Language: "eng" by default; e.g. "fra", or "eng+lat" for mixed
    material (the language pack must be installed with Tesseract).
  - PSM (page segmentation): 3 = automatic (default).
      Use 1 for collections with pages scanned SIDEWAYS or UPSIDE
      DOWN (landscape tables in a portrait frame are the usual case).
      With 3, those pages come out as confident-looking garbage; with
      1, Tesseract detects the rotation and reads them correctly. It is
      not the default because it is slower and can invent a few junk
      words on blank pages — check blank pages afterwards.
      4 = single column, 6 = one uniform block of text: worth a retry
      on a problem object whose layout confuses the automatic mode.
  - OEM: leave at 3.
  - GLMOCR: endpoint, model (glm-ocr:q8_0) and timeout.
  - Preprocessing: Min DPI (low-resolution scans are enlarged to at
    least this), Denoise and CLAHE contrast (on), Binarize (OFF —
    turning it on usually makes results worse on toned or grainy
    paper; only try it if a collection measurably benefits). Deskew
    and border crop always stay off: they would move the text away
    from where the word boxes place it on the original scan.
  - "Save preprocessed images to": leave empty. For debugging only.
  - METS checksums: on by default; turning off speeds up very large
    objects.


7. Correcting transcripts (Review tab)
--------------------------------------
  Loading an object:
    - "Transcript Folder": the object's OCR output folder (the one
      containing ALTO\). "Source images": the matching source-image
      folder (the preservation TIFs — they are not copied into the
      output). Then click "Load object".
    - The page list sorts worst-first by page confidence, shown as a
      percentage. Pick a page (or use "◀ Page" / "Page ▶", Ctrl+PgUp /
      Ctrl+PgDn) to see the scan with word boxes colored by confidence
      (red = low, green = high). The list keeps its order while you
      work; re-tick "Worst first" to re-rank it.
    - "Flag below" chooses which words need review, as a percentage
      (default 50%). "All words" (on by default) draws a box around
      every word; untick it to see only the flagged ones.

  Correcting words:
    - Click a word — or press n / p (or "Next ▶" / "◀ Prev") to step
      through flagged words — and correct it in the "Selected word"
      bar; a magnified snippet shows the scan region.
    - Enter commits and jumps to the next flagged word below. Enter on
      an UNCHANGED word confirms the OCR was right (it stops being
      flagged). Esc deselects.
    - "Delete word" removes a hallucinated word at once — there is no
      "are you sure?" prompt. Ctrl+Z (or Undo) puts it back exactly.
    - "Add word (a)": drag a box around a word the OCR missed, type
      it, press Enter.
    - The symbol keyboard under the editor types characters you can't
      reach on a US keyboard: math, Greek, super/subscripts and
      fractions, units and currency, apothecary signs, typographic
      punctuation, arrows, and accented letters. Pick a category, click
      a key; rest the pointer on a key to see its Unicode name. Every
      key is checked to be safe in ALTO XML. "Pop out" gives the
      keyboard a small window of its own that stays above the page;
      untick "Symbols" to hide it.

  Page-level tools:
    - "Reading order (r)": fix transcripts whose words are right but
      in the wrong order — two columns read straight across, a caption
      or margin note read into the middle of a paragraph. The scan
      shows the order as a line running from word to word (arrows mark
      each line break), and a side panel shows the transcript exactly
      as it will be saved.
        1. Select the words that are out of place: click a word, Shift+
           click to extend to a run of words, Ctrl+click to add or drop
           a single word — or drag a box around a whole column or
           caption on the scan. (You can click in the side panel's
           transcript too.)
        2. Click the word they should come after. "Page start" puts
           them first instead; Ctrl+Up / Ctrl+Down nudge them one word.
      Ctrl+Z undoes a move; Esc clears the selection. Word boxes are
      untouched; only the transcript order changes.
    - "Clear page...": removes ALL words on the page in one step. Use it
      when the whole page is hallucinated OCR — covers, photographs,
      or blank pages where the engine read texture or noise as
      garbage words. The page becomes empty; any real words can then
      be added back with "Add word". Ctrl+Z can't undo this — to back
      out, leave the page without saving.
    - "Pop out page" moves the page — scan, toolbars, editor — into a
      window of its own: drag it to a second monitor or maximize it.
      Close that window (or "Dock page") to bring it back. Unsaved
      corrections and Undo come along.
    - Zoom: the dropdown ("Fit", "Fit width", or a percentage), or
      Ctrl+scroll on the image (zooms around the mouse cursor). Plain
      scroll pans vertically, Shift+scroll pans horizontally.

  Saving:
    - "Save page" (Ctrl+S) writes the corrected ALTO and updates the
      plain-text transcript to match. You're asked before leaving a
      page with unsaved changes.
    - If someone else (or another program) changed the page since you
      opened it, saving asks before overwriting their version.

  Keyboard summary (click the scan first so it has focus):
    n / p ............ next / previous flagged word
    Enter ............ apply correction, or confirm an unchanged word
    Esc .............. deselect / cancel an add-word box
    a ................ add-word mode on/off
    r ................ reading-order mode on/off
    Ctrl+Up/Down ..... reading order: nudge the selected words
    Ctrl+Z ........... undo (edits, confirms, deletes, adds, moves)
    Ctrl+S ........... save page
    Ctrl+PgUp/PgDn ... previous / next page
    Ctrl+scroll ...... zoom


8. Finishing up: Repackage
--------------------------
  When you're done reviewing an object, click "Repackage" (next to
  "Load object"). It refreshes the METS file's sizes and checksums so
  the package matches your corrected files — do this before the object
  is uploaded. Pointing "Transcript Folder" at the parent output folder
  repackages every object under it in one go.

  Also run it after editing ALTO files in any other tool: it
  regenerates the Text\ transcripts from the ALTO as well.


9. Output layout
----------------
For each Ref ID processed, you get:

  <output>\<RefID>\ALTO\            one ALTO XML per page
  <output>\<RefID>\Text\            one plain-text transcript per page
  <output>\<RefID>\<RefID>.mets.xml

Page files keep the source scan's name: "image001.tif" becomes
ALTO\image001.xml and Text\image001.txt.

The source TIFs are read but NOT copied into the output. The canonical
preservation copies in preservation-files\preservation\<RefID>\ remain
the upload-ready masters; duplicating them here would just waste storage
on the network drive. LibNova pairs the preservation tree and the
ALTO/Text output tree by Ref ID externally.


10. Command line (optional)
---------------------------
  Open a terminal in this folder and run, for example:

    alto-ocr.exe process   "C:\path\to\object" "C:\path\to\output"
    alto-ocr.exe batch     "C:\path\to\collection" "C:\path\to\output"
    alto-ocr.exe triage    "C:\path\to\output"
    alto-ocr.exe repackage "C:\path\to\output"
    alto-ocr.exe doctor

  - process: one Ref ID folder. batch: a folder of Ref ID folders.
  - triage: ranks existing output worst-first for review, prints the
    objects and pages that most need attention, names likely
    handwriting objects (mean confidence below 60%), and writes
    triage_objects.csv / triage_pages.csv (open in Excel) next to the
    outputs. Works on any output folder, old or new.
  - repackage: the same as the Review tab's Repackage button.
  - doctor: checks that both engines are reachable.

  Useful options for process / batch:
    --engine glmocr          use GLMOCR (handwriting) instead of Tesseract
    --psm 1                  handle sideways / upside-down pages
    --ocr-lang fra           OCR language (default eng)
    --workers 4              batch only: OCR several objects in parallel
    --ref-id NAME            process only: name the output something
                             other than the input folder's name
    --summary-json FILE      write a machine-readable run summary
    --force-reocr            OVERWRITE objects that have hand corrections
                             (they are lost). Without it such objects
                             are refused.

  After a run, the console suggests a command of the form
  "python -m src.alto_ocr.main triage ...". With this bundle, type
  "alto-ocr.exe triage ..." instead.

  Full option lists:
    alto-ocr.exe --help
    alto-ocr.exe process --help
    alto-ocr.exe batch --help
    alto-ocr.exe triage --help


11. Troubleshooting
-------------------
  "Tesseract not found"
    The default engine isn't installed, or isn't somewhere Alto OCR
    can see it. Install the UB-Mannheim build (see setup step 1), or
    point at an existing install under Tools > Advanced settings >
    "Tesseract program".

  "GLMOCR endpoint check failed"
    Only matters when you've selected GLMOCR. Make sure Ollama is
    running and the glm-ocr:q8_0 model is pulled (see setup step 2).
    The Endpoint field in Tools > Advanced settings accepts a remote
    URL like http://other-machine:11434 if Ollama runs elsewhere.

  An object fails with "refusing to overwrite human corrections"
    Someone has already corrected pages of that object in the Review
    tab, and a new OCR run would erase their work. If you really want
    fresh OCR, move or rename that object's output folder first (or
    use --force-reocr on the command line). Otherwise, leave it.

  An object fails with "output-stem collision"
    Two scans in the object would produce the same page name. See
    "Preparing the input folder" above.

  Every page of an object fails with "No such file"
    Usually the full path is too long for Windows (over ~260
    characters — very long series / sub-series folder names do this,
    especially on network drives). Copy or map the object to a
    shorter path (e.g. a drive letter close to the Ref ID folder) and
    run again.

  A page reads as fluent-looking nonsense, or as strings like
  "LUIt-9T9 OSTT-009"
    The page is probably scanned sideways or upside down. Re-run the
    object with PSM 1 (Tools > Advanced settings).

  A whole object is low confidence (mostly red boxes)
    Usually handwriting. Re-run that object with the GLMOCR engine.

  A cover, photograph or blank page is full of garbage words
    Use "Clear page..." in the Review tab.

  Antivirus flags the bundle
    PyInstaller binaries are sometimes false-positive flagged. The
    bundle is just Python + this app's source code; have your IT
    whitelist the folder if needed.
