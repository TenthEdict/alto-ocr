"""Input discovery: walk a collection directory and group sources by Ref ID.

A "Ref ID" is one immediate subdirectory of the collection directory (e.g.
``MSP00074_14698``, ``UA00058_17072``). All TIFs **and PDFs** found anywhere
under that subdirectory — at any nesting depth — are collected into a single
ordered list and become one output object. This covers every layout
encountered in practice:

  - TIFs directly inside the Ref ID folder.
  - TIFs inside a single child folder (e.g. ``ZeffPapers``).
  - TIFs split across year/document sub-folders, possibly several layers
    deep (e.g. ``UA00058_17072/1927/UA58i19270613SM/*.tif``).
  - PDFs mixed in with TIFs at any depth; each PDF expands to one OCR'd
    page per PDF page during pipeline execution.

Ordering is by *relative path components* from the Ref ID root, each
naturally sorted. This groups pages from the same source sub-folder
together, and orders sub-folders themselves in a sensible way when their
names encode dates (``UA58i19270104SM`` < ``UA58i19270413SM``).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .pdf import PDF_SUFFIXES, count_pages as count_pdf_pages

logger = logging.getLogger(__name__)

# Raster page formats: one OCR page per file. JPEG is accepted alongside TIF
# for collections that export both — point a run at the format-specific folder
# (e.g. .../JPEG for glm-ocr, .../TIFF for Tesseract). A TIF and JPEG of the
# same page in the *same* folder share an output stem; that's caught by the
# stem-collision check, so keep the two exports in separate folders.
IMAGE_SUFFIXES = {".tif", ".tiff", ".jpg", ".jpeg"}
JUNK_NAMES = {"thumbs.db", "thumbs.db:encryptable", ".ds_store"}

# Width of the page-number suffix in the rasterized PDF filenames. Mirrored
# from ``pdf.py`` so the stem-collision check can predict output stems for
# PDFs without actually rasterizing.
_PDF_PAGE_DIGITS = 3

_NUM_RE = re.compile(r"(\d+)")


def _natural_chunks(s: str) -> tuple:
    """Split a string into alternating text/int chunks for natural sort.

    ``"Page 10"`` → ``("page ", 10, "")``; ``"19270104"`` → ``("", 19270104, "")``.
    Numeric runs compare numerically; everything else is lower-cased so the
    order is case-insensitive.
    """
    return tuple(
        int(p) if p.isdigit() else p.lower()
        for p in _NUM_RE.split(s)
    )


def _natural_key(path: Path) -> tuple:
    """Sort key for a Path based on its filename stem (no directory parts).

    Useful when sorting a flat list of files that already live in the same
    directory — e.g., ALTO XML outputs being re-collated from a single
    ``ALTO/`` folder.
    """
    return _natural_chunks(path.stem)


def _rel_natural_key(path: Path, base: Path) -> tuple:
    """Sort key: relative-path components from ``base``, each naturally sorted.

    For ``base = /A`` and ``path = /A/year/meeting/001foo.tif``, returns a
    3-tuple ``(chunks('year'), chunks('meeting'), chunks('001foo'))``. Python
    compares tuples element-wise, so the directory components steer the
    coarse order (group by sub-folder, sub-folders ordered naturally) and
    the filename stem steers the fine order within a sub-folder.
    """
    rel = path.relative_to(base)
    *dir_parts, name = rel.parts
    return tuple(_natural_chunks(p) for p in (*dir_parts, Path(name).stem))


def _is_junk(path: Path) -> bool:
    return path.name.lower() in JUNK_NAMES


@dataclass(frozen=True)
class SourceFile:
    """One source file in a Ref ID: a TIF (1 page) or a PDF (N pages).

    ``page_count`` is the number of OCR'd pages this source contributes to
    the output. For TIFs that's always 1; for PDFs it's the result of
    peeking at the PDF's page count at discovery time so the total page
    budget is known before any rasterization runs.
    """

    path: Path
    page_count: int

    @property
    def is_pdf(self) -> bool:
        return self.path.suffix.lower() in PDF_SUFFIXES

    def predicted_output_stems(self) -> list[str]:
        """Stems the pipeline will produce for this source.

        For a TIF, that's the source's own stem. For a PDF, it's the
        rasterized-page names — see ``pdf.rasterize_pdf`` for the naming
        convention.
        """
        if self.is_pdf:
            stem = self.path.stem
            return [
                f"{stem}_page{i:0{_PDF_PAGE_DIGITS}d}"
                for i in range(1, self.page_count + 1)
            ]
        return [self.path.stem]


def _find_sources_in(directory: Path) -> list[SourceFile]:
    """Find every image (TIF/JPEG) or PDF anywhere under ``directory`` (recursive).

    Uses ``os.walk`` so each directory costs one ``scandir`` call and the
    files come back already classified as non-directories — no per-file
    ``stat`` is needed. This matters over network drives (SMB), where a
    per-file stat round-trip can turn a 30-second discovery into a 30-minute
    one. Filtering is by extension on the filename string.

    Each discovered PDF is opened just long enough to read its page count
    (a cheap header/xref read, no rendering) so the total page budget for
    progress reporting is accurate up front. A PDF that fails to open is
    logged and skipped — discovery does not abort the rest of the Ref ID.

    Returns the sources sorted by relative path so pages from the same source
    sub-folder stay together. Logs (does not raise) when the directory has
    no sources at all, so batch runs surface every problem at once.
    """
    if not directory.is_dir():
        logger.warning("Not a directory, skipping: %s", directory)
        return []

    sources: list[SourceFile] = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        parent = Path(dirpath)
        for fn in filenames:
            suffix = os.path.splitext(fn)[1].lower()
            full = parent / fn
            if suffix in IMAGE_SUFFIXES:
                sources.append(SourceFile(path=full, page_count=1))
            elif suffix in PDF_SUFFIXES:
                try:
                    n = count_pdf_pages(full)
                except Exception as e:
                    logger.warning(
                        "Cannot read PDF %s (%s); skipping", full, e,
                    )
                    continue
                if n <= 0:
                    logger.warning("PDF reports 0 pages, skipping: %s", full)
                    continue
                sources.append(SourceFile(path=full, page_count=n))
    if sources:
        return sorted(sources, key=lambda s: _rel_natural_key(s.path, directory))

    leftovers = [p.name for p in directory.iterdir() if not _is_junk(p)]
    if leftovers:
        logger.warning(
            "No images or PDFs found anywhere under %s. Top-level contents: %s",
            directory.name,
            leftovers,
        )
    return []


@dataclass(frozen=True)
class RefIdInputs:
    """One Ref ID's worth of inputs ready for the pipeline."""

    ref_id: str
    source_dir: Path
    sources: tuple[SourceFile, ...]

    @property
    def page_count(self) -> int:
        """Total OCR'd pages this Ref ID will produce (TIFs + expanded PDFs)."""
        return sum(s.page_count for s in self.sources)

    @property
    def has_pdfs(self) -> bool:
        return any(s.is_pdf for s in self.sources)


def _check_stem_collisions(ref_id: str, sources: list[SourceFile]) -> bool:
    """Return True if every page's output stem is unique within the Ref ID.

    Output filenames in ALTO/ and Text/ use only the stem (no source
    extension to disambiguate), so two pages sharing a stem would silently
    overwrite each other's XML/TXT outputs. This check is run on the
    *predicted output stems* — for a PDF, that's its rasterized-page names
    (``<stem>_page001`` etc.), not the PDF's own stem. So we'd catch a
    standalone ``Foo_page001.tif`` colliding with the rasterized page-1 of
    ``Foo.pdf``, but we won't false-positive on ``Foo.pdf`` + ``Foo.tif``
    (those expand to different stems and never collide in output).
    """
    seen: dict[str, str] = {}  # stem -> human-readable origin description
    collisions: list[tuple[str, str, str]] = []  # (stem, prior_origin, this_origin)
    for s in sources:
        for stem in s.predicted_output_stems():
            origin = s.path.name if not s.is_pdf else f"{s.path.name} (rasterized page)"
            prior = seen.get(stem)
            if prior is not None:
                collisions.append((stem, prior, origin))
            else:
                seen[stem] = origin
    if collisions:
        for stem, prior, this in collisions:
            logger.warning(
                "[%s] output-stem collision %r: produced by both %s and %s — "
                "ALTO/TXT outputs would overwrite. Rename at the source.",
                ref_id, stem, prior, this,
            )
        return False
    return True


def discover_ref_id(ref_id_dir: Path) -> RefIdInputs | None:
    """Build a RefIdInputs for a single Ref ID directory.

    Returns None if no sources were found (a warning is logged) or if the
    Ref ID has stem collisions that would corrupt the output.
    """
    ref_id_dir = ref_id_dir.resolve()
    sources = _find_sources_in(ref_id_dir)
    if not sources:
        return None
    if not _check_stem_collisions(ref_id_dir.name, sources):
        return None
    return RefIdInputs(
        ref_id=ref_id_dir.name,
        source_dir=ref_id_dir,
        sources=tuple(sources),
    )


def discover_collection(collection_dir: Path) -> list[RefIdInputs]:
    """Walk a collection directory; return one RefIdInputs per non-empty Ref ID.

    Any immediate subdirectory is treated as a candidate Ref ID. The caller's
    only structural assumption is "this directory contains per-object
    subdirectories" — collection-name conventions (MSP*, MSA*, UA*, …) are
    not enforced here.
    """
    collection_dir = collection_dir.resolve()
    if not collection_dir.is_dir():
        raise NotADirectoryError(f"Collection directory not found: {collection_dir}")

    candidates = sorted(
        (p for p in collection_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    )
    logger.info(
        "Scanning %d candidate Ref ID director%s under %s ...",
        len(candidates),
        "y" if len(candidates) == 1 else "ies",
        collection_dir,
    )
    results: list[RefIdInputs] = []
    for i, c in enumerate(candidates, start=1):
        # Progress line per candidate. Network-drive scans can take seconds
        # per Ref ID, and the GUI shows live logs — silence here makes the
        # tool look frozen.
        logger.info("  [%d/%d] scanning %s ...", i, len(candidates), c.name)
        item = discover_ref_id(c)
        if item is not None:
            n_pdf = sum(1 for s in item.sources if s.is_pdf)
            n_tif = len(item.sources) - n_pdf
            logger.info(
                "           %s: %d page(s) from %d TIF + %d PDF source(s)",
                item.ref_id, item.page_count, n_tif, n_pdf,
            )
            results.append(item)
    logger.info(
        "Discovered %d Ref ID(s) with TIFs/PDFs in %s (out of %d subdirs)",
        len(results),
        collection_dir,
        len(candidates),
    )
    return results


