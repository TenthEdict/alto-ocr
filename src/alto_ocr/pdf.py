"""PDF rasterization for the OCR pipeline.

Some collections mix PDF originals in with the canonical TIFs. Rather than
teach the rest of the pipeline about PDFs (which would mean a PDF code path
through preprocess, OCR, and METS), we rasterize each PDF up front to a
sequence of TIFs that the existing TIF-only pipeline can consume unchanged.

Library choice: ``pypdfium2`` (Apache 2.0, pre-built wheels for Windows +
Linux, no system prerequisites — statically links Google's PDFium). The
faster ``PyMuPDF`` / ``fitz`` alternative is AGPL-licensed which is awkward
for a Purdue redistribution; ``pdf2image`` would force a poppler system
install on Windows.

Output naming: a PDF named ``Minutes.pdf`` with N pages becomes
``Minutes_page001.tif``, ``Minutes_page002.tif``, …, in the supplied output
directory. Three-digit zero-padding handles the common case (≤ 999 pages);
files past page 999 still sort correctly under natural sort.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Width of the page-number suffix in the rasterized filenames. Three digits
# covers the realistic upper bound for archival documents; natural-sort
# handles wider numbers if we ever exceed 999.
_PAGE_DIGITS = 3


def count_pages(pdf_path: Path) -> int:
    """Return the number of pages in a PDF without rendering.

    Cheap — opens the document, reads the page count, closes. Used during
    discovery so the total page count for progress reporting is accurate
    before any rasterization runs.
    """
    import pypdfium2 as pdfium  # deferred: the GUI imports this module at startup

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def rasterize_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int = 300,
) -> list[Path]:
    """Render each page of ``pdf_path`` to a grayscale TIF in ``out_dir``.

    Returns the list of written TIF paths in page order. DPI is baked into
    the TIF metadata so the existing preprocess step's DPI-sanity check
    treats these like any other scanned TIF.

    Grayscale at render time (rather than converting after) avoids a color
    → grayscale step in preprocess and keeps the temp-file footprint down.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    import pypdfium2 as pdfium  # deferred: the GUI imports this module at startup

    pdf = pdfium.PdfDocument(str(pdf_path))
    written: list[Path] = []
    try:
        n_pages = len(pdf)
        if n_pages == 0:
            logger.warning("PDF has zero pages, skipping: %s", pdf_path)
            return []
        # PDFium scale = pixels per PDF user unit; 1 user unit = 1/72 inch.
        scale = dpi / 72.0
        stem = pdf_path.stem
        for i in range(n_pages):
            page = pdf[i]
            try:
                bitmap = page.render(scale=scale, grayscale=True)
                pil_img = bitmap.to_pil()
                out_path = out_dir / f"{stem}_page{i + 1:0{_PAGE_DIGITS}d}.tif"
                # Save with DPI tag so preprocess.read_dpi sees the right value.
                # tiff_lzw keeps the temp footprint reasonable without losing data.
                pil_img.save(
                    out_path,
                    format="TIFF",
                    compression="tiff_lzw",
                    dpi=(dpi, dpi),
                )
                written.append(out_path)
            finally:
                page.close()
    finally:
        pdf.close()
    logger.debug("Rasterized %s → %d TIF(s) at %d DPI in %s",
                 pdf_path.name, len(written), dpi, out_dir)
    return written


PDF_SUFFIXES = frozenset({".pdf"})
