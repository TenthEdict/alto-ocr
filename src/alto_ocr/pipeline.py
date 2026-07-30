"""Per-Ref-ID orchestration.

For one Ref ID:

1. Discover TIFs (walker).
2. Preprocess each page in memory.
3. OCR (Tesseract → ALTO + TXT).
4. Write METS.

The source TIFs are read but never copied into the output tree: the
preservation copies in ``preservation-files/preservation/<RefID>/`` are
the canonical TIFs the upload workflow ingests, so any copy alongside
ALTO/Text would just duplicate them on storage.

Per-page failures are isolated: a single bad page is logged, recorded in
the summary, and replaced with a stub ALTO/TXT so the ALTO/Text page
counts stay aligned with the source TIF count. The Ref ID continues to
a successful METS write.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as xml_escape

from ._util import atomic_write_text
from .mets import PageEntry, build_mets, write_mets
from .ocr import OCREngine
from .remediate import has_review_provenance
from .pdf import rasterize_pdf
from .preprocess import PreprocessConfig, preprocess
from .walker import RefIdInputs, SkippedRefId, _rel_natural_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PageInput:
    """One OCR-able page after PDF expansion.

    ``real_path`` is the actual TIF on disk — either the original source
    TIF or a rasterized PDF page in a temp directory. ``sort_path`` is a
    synthetic path rooted at the Ref ID's source directory, used solely
    for ordering so rasterized PDF pages sort into the position the PDF
    itself would have occupied. ``stem`` is the output filename stem.
    """
    real_path: Path
    sort_path: Path
    stem: str


def _expand_sources(
    inputs: RefIdInputs,
    temp_dir: Path | None,
    *,
    dpi: int,
) -> list[_PageInput]:
    """Expand each source into one or more ``_PageInput`` entries.

    TIFs map 1:1. PDFs are rasterized into a per-PDF subdirectory of
    ``temp_dir`` (keyed by the PDF's relative path inside the Ref ID, so
    two PDFs named ``Minutes.pdf`` in different sub-folders never collide
    in temp storage). Returns the unified list sorted by the same rel-path
    natural-key the walker uses on plain TIFs — a Ref ID containing both
    raw TIFs and PDFs comes out in a consistent, predictable order.
    """
    pages: list[_PageInput] = []
    for s in inputs.sources:
        if not s.is_pdf:
            pages.append(_PageInput(
                real_path=s.path,
                sort_path=s.path,
                stem=s.path.stem,
            ))
            continue
        if temp_dir is None:
            # Shouldn't happen: process_ref_id allocates temp_dir when
            # inputs.has_pdfs. Defensive assertion in case the contract drifts.
            raise RuntimeError(
                f"PDF source {s.path} requires a temp directory but none was supplied"
            )
        rel = s.path.relative_to(inputs.source_dir)
        pdf_temp = temp_dir / rel.parent / rel.stem
        tifs = rasterize_pdf(s.path, pdf_temp, dpi=dpi)
        for tif in tifs:
            # tif.stem is already "<pdfstem>_pageNNN" — match it in the
            # synthetic sort_path so ordering is intuitive (the rasterized
            # page sits where its parent PDF lived).
            pages.append(_PageInput(
                real_path=tif,
                sort_path=s.path.parent / f"{tif.stem}.tif",
                stem=tif.stem,
            ))

    pages.sort(key=lambda p: _rel_natural_key(p.sort_path, inputs.source_dir))
    return pages


class PageProgressLogger:
    """Reports OCR progress at fixed completion thresholds.

    Thresholds: 1%, 5%, then every 5% up to 100%. Each threshold fires at
    most once. When a single page advances completion past multiple
    thresholds (small total page counts), only the highest crossed
    threshold is logged in that step — keeps the log signal-to-noise high.

    Thread-safe: usable as an ``on_page_done`` callback from worker threads.
    Not designed to span processes; for ``ProcessPoolExecutor`` callers
    the existing per-Ref-ID "done" lines already give Ref-ID-granular
    progress.
    """

    THRESHOLDS = (1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                  55, 60, 65, 70, 75, 80, 85, 90, 95, 100)

    def __init__(self, total_pages: int, *, log: logging.Logger | None = None) -> None:
        self.total = max(int(total_pages), 0)
        self._log = log or logger
        self._lock = threading.Lock()
        self._done = 0
        self._next_idx = 0

    def tick(self) -> None:
        """Record one completed page; emit a log line if a threshold was crossed."""
        with self._lock:
            if self.total <= 0:
                return
            self._done += 1
            pct = self._done * 100 // self.total
            crossed: int | None = None
            while (self._next_idx < len(self.THRESHOLDS)
                   and self.THRESHOLDS[self._next_idx] <= pct):
                crossed = self.THRESHOLDS[self._next_idx]
                self._next_idx += 1
            if crossed is not None:
                self._log.info(
                    "Progress: %d%% (%d/%d pages)",
                    pct, self._done, self.total,
                )

ALTO_FAILURE_STUB = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v2#" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.loc.gov/standards/alto/ns-v2# http://www.loc.gov/standards/alto/v2/alto-2-0.xsd">
\t<Description>
\t\t<MeasurementUnit>mm10</MeasurementUnit>
\t\t<Processing ID="failed">
\t\t\t<processingDateTime>{when}</processingDateTime>
\t\t\t<processingAgency>alto_ocr</processingAgency>
\t\t\t<processingStepDescription>OCR failed: {reason}</processingStepDescription>
\t\t</Processing>
\t</Description>
\t<Layout/>
</alto>
"""


@dataclass
class PageResult:
    stem: str
    order: int
    ok: bool
    error: str | None = None
    stages: tuple[str, ...] = ()
    effective_dpi: int | None = None


@dataclass
class RefIdResult:
    ref_id: str
    output_dir: Path
    pages: list[PageResult] = field(default_factory=list)
    # Set only when a whole Ref ID aborted before per-page results existed, so
    # the summary can report the object's true input page count instead of the
    # single synthetic failure page. None on a normally-processed Ref ID.
    expected_page_count: int | None = None

    @property
    def page_count(self) -> int:
        return self.expected_page_count if self.expected_page_count is not None else len(self.pages)

    @property
    def failed_pages(self) -> list[PageResult]:
        return [p for p in self.pages if not p.ok]

    @property
    def failed_page_count(self) -> int:
        # When a whole Ref ID aborted, ALL of its expected input pages went
        # unproduced even though a single synthetic PageResult carries the
        # error — so the failed count is the full page_count, not 1. On a
        # normally-processed Ref ID this is just len(failed_pages). Keeps
        # "succeeded = page_count - failed_page_count" true on both paths.
        if self.expected_page_count is not None:
            return self.expected_page_count
        return len(self.failed_pages)

    @property
    def ok(self) -> bool:
        return bool(self.pages) and not self.failed_pages


def _write_failure_stubs(alto_path: Path, txt_path: Path | None, reason: str) -> None:
    from datetime import datetime, timezone

    when = datetime.now(timezone.utc).isoformat()
    # Escape the reason: exception text routinely contains <, >, & (Tesseract
    # stderr, OS errors) and would otherwise make the stub malformed XML.
    atomic_write_text(alto_path, ALTO_FAILURE_STUB.format(
        when=when, reason=xml_escape(reason)))
    if txt_path is not None:
        atomic_write_text(txt_path, "")


def failed_ref_result(inputs: RefIdInputs, output_root: Path,
                      exc: BaseException) -> RefIdResult:
    """Synthesize a failed RefIdResult when a whole Ref ID aborts.

    ``process_ref_id`` isolates per-*page* errors itself; this covers a
    Ref-ID-level exception (or a crashed pool worker) so one bad object can't
    abort an unattended multi-hour batch. ``expected_page_count`` is set so the
    summary reports the object's true input page count (not 1), and a single
    synthetic failed page carries the error. Shared by the CLI and the GUI.
    """
    return RefIdResult(
        ref_id=inputs.ref_id,
        output_dir=(output_root / inputs.ref_id).resolve(),
        expected_page_count=inputs.page_count,
        pages=[PageResult(
            stem=f"<Ref ID aborted before page output ({inputs.page_count} page(s))>",
            order=0,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )],
    )


def record_ref_failure(results: list[RefIdResult], inputs: RefIdInputs,
                       output_root: Path, exc: BaseException) -> None:
    """Log a Ref-ID-level failure (with traceback) and append a synthetic
    failed result so the batch continues.

    Uses ``exc_info=exc`` (not bare ``logger.exception``) so the *passed*
    exception's traceback is logged even when called outside its except
    block — the CLI's pool-recovery path records errors while handling a
    BrokenProcessPool."""
    logger.error("[%s] Ref ID aborted (recorded as failed, continuing): %s",
                 inputs.ref_id, exc, exc_info=exc)
    results.append(failed_ref_result(inputs, output_root, exc))


def skipped_ref_result(skip: SkippedRefId, output_root: Path) -> RefIdResult:
    """RefIdResult for an object dropped at discovery (stem collisions), so
    summaries and exit codes reflect it instead of the object silently
    vanishing from the run."""
    return RefIdResult(
        ref_id=skip.ref_id,
        output_dir=(output_root / skip.ref_id).resolve(),
        expected_page_count=skip.page_count,
        pages=[PageResult(stem="<skipped at discovery>", order=0, ok=False,
                          error=skip.reason)],
    )


def process_ref_id(
    inputs: RefIdInputs,
    output_root: Path,
    *,
    engine: OCREngine,
    preprocess_config: PreprocessConfig | None = None,
    debug_preprocessed_dir: Path | None = None,
    with_checksums: bool = True,
    write_mets_file: bool = True,
    write_plaintext: bool = True,
    force_reocr: bool = False,
    on_page_done: Callable[[], None] | None = None,
) -> RefIdResult:
    """Run the full pipeline for one Ref ID. Always returns a result object;
    individual page failures are reported in ``result.pages`` rather than
    raised, so a batch run sees them all rather than aborting on the first.

    ``force_reocr=False`` (the default) arms the review-provenance guard:
    when the object's existing output contains ALTO pages carrying
    ``<Processing ID="review">`` — human corrections from the Review tab —
    the whole Ref ID is refused (returned as a failed result, no file
    touched) so a re-run cannot silently destroy correction work. Pass
    ``force_reocr=True`` to deliberately discard the corrections and
    re-OCR; moving the object's output folder aside also works.

    ``write_mets_file=False`` skips the per-Ref-ID METS document. The
    ALTO/Text outputs are unaffected — only the ``<RefID>.mets.xml``
    file is suppressed, for downstream workflows (e.g. LibNova) that don't
    consume METS.

    ``write_plaintext=False`` suppresses the ``Text/`` folder: the OCR
    engine still produces a transcript internally, but the .txt is
    discarded after ALTO is written, and the METS (if produced) omits the
    Text fileGrp.
    """
    out_dir = (output_root / inputs.ref_id).resolve()
    alto_dir = out_dir / "ALTO"
    text_dir = out_dir / "Text"

    # Review-provenance guard: never silently overwrite pages a human has
    # corrected. Cost on a fresh run is zero (no existing ALTO dir).
    if not force_reocr and alto_dir.is_dir():
        reviewed = [p.stem for p in sorted(alto_dir.glob("*.xml"))
                    if has_review_provenance(p)]
        if reviewed:
            preview = ", ".join(reviewed[:3]) + (", …" if len(reviewed) > 3 else "")
            msg = (f"{len(reviewed)} already-reviewed page(s) in the existing "
                   f"output ({preview}) — refusing to overwrite human "
                   f"corrections. Re-run with --force-reocr to discard them, "
                   f"or move {out_dir} aside first.")
            logger.error("[%s] %s", inputs.ref_id, msg)
            guarded = RefIdResult(
                ref_id=inputs.ref_id, output_dir=out_dir,
                expected_page_count=inputs.page_count,
            )
            guarded.pages.append(PageResult(
                stem="<review-guard>", order=0, ok=False, error=msg,
            ))
            return guarded

    required_dirs = (alto_dir, text_dir) if write_plaintext else (alto_dir,)
    for d in required_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Engines may need a different preprocessing recipe than the user's
    # default — e.g. GLMOCR's vision projector trips a GGML assertion on
    # binarized inputs and needs continuous grayscale. The hook is a no-op
    # for engines that don't override it.
    effective_preprocess = preprocess_config or PreprocessConfig()
    if hasattr(engine, "adjust_preprocess"):
        adjusted = engine.adjust_preprocess(effective_preprocess)
        if adjusted != effective_preprocess:
            logger.debug(
                "[%s] engine %r adjusted preprocess: %s -> %s",
                inputs.ref_id, engine.name, effective_preprocess, adjusted,
            )
            effective_preprocess = adjusted

    result = RefIdResult(ref_id=inputs.ref_id, output_dir=out_dir)
    page_entries: list[PageEntry] = []

    # PDF sources need a temp dir for rasterized pages; pure-TIF Ref IDs
    # skip the temp-dir allocation entirely so the common path stays fast.
    with ExitStack() as stack:
        temp_dir: Path | None = None
        if inputs.has_pdfs:
            td = stack.enter_context(tempfile.TemporaryDirectory(
                prefix=f"alto_ocr_{inputs.ref_id}_"
            ))
            temp_dir = Path(td)
            logger.debug("[%s] using temp dir for rasterized PDFs: %s",
                         inputs.ref_id, temp_dir)

        pages = _expand_sources(
            inputs, temp_dir,
            dpi=effective_preprocess.min_dpi,
        )

        for idx, page in enumerate(pages, start=1):
            src_tif = page.real_path
            stem = page.stem
            page_entries.append(PageEntry(stem=stem, order=idx))

            alto_dest = alto_dir / f"{stem}.xml"
            text_dest = text_dir / f"{stem}.txt" if write_plaintext else None

            try:
                # Step 1: preprocess in memory (with any engine-specific overrides).
                pre = preprocess(src_tif, effective_preprocess)

                # Optional: dump what Tesseract actually sees, for debugging accuracy.
                if debug_preprocessed_dir is not None:
                    debug_dir = debug_preprocessed_dir / inputs.ref_id
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    from .preprocess import write_image
                    write_image(pre.image, debug_dir / f"{stem}.png")

                # Step 2: OCR. Engine writes alto/txt next to output_base.
                # ``page_index`` seeds the sequential P{N} Page IDs;
                # ``source_filename`` is set as ./<basename> in
                # <sourceImageInformation> so downstream metadata mirrors
                # the institution's reference convention.
                engine.ocr(
                    pre.image,
                    alto_dir / stem,  # ALTO base path; engine writes .xml + .txt
                    dpi=pre.effective_dpi,
                    page_index=idx,
                    source_filename=src_tif.name,
                )

                # The engine's ALTO and TXT both land next to the same base path.
                # Relocate or discard the .txt sibling depending on write_plaintext.
                produced_txt = alto_dir / f"{stem}.txt"
                if write_plaintext:
                    if produced_txt.exists():
                        shutil.move(str(produced_txt), str(text_dest))
                    else:
                        logger.warning("Expected text output missing for %s/%s", inputs.ref_id, stem)
                        atomic_write_text(text_dest, "")
                else:
                    # User opted out of plaintext — drop the engine's .txt rather
                    # than leaving it stranded next to the ALTO file.
                    if produced_txt.exists():
                        try:
                            produced_txt.unlink()
                        except OSError:
                            logger.debug("Could not remove unused TXT: %s", produced_txt)

                # Belt-and-suspenders: every page must end with the requested
                # outputs present so upload packages stay consistent — even
                # blank/no-text TIFs need the XML (and TXT, if enabled) companions.
                expected = [alto_dest]
                if write_plaintext:
                    expected.append(text_dest)
                missing = [p for p in expected if not p.is_file()]
                if missing:
                    raise RuntimeError(
                        "Missing expected outputs after OCR: "
                        + ", ".join(str(p.relative_to(out_dir)) for p in missing)
                    )

                result.pages.append(PageResult(
                    stem=stem, order=idx, ok=True,
                    stages=pre.stages_applied, effective_dpi=pre.effective_dpi,
                ))
                logger.debug(
                    "[%s] %s ← %s | dpi=%d stages=%s",
                    inputs.ref_id, stem, src_tif.name, pre.effective_dpi, pre.stages_applied,
                )

            except Exception as e:
                logger.error("[%s] page %d (%s) failed: %s", inputs.ref_id, idx, src_tif.name, e)
                _write_failure_stubs(alto_dest, text_dest, reason=str(e).replace("\n", " ")[:200])
                result.pages.append(PageResult(
                    stem=stem, order=idx, ok=False, error=str(e),
                ))

            # Page is done (success or failure). Fire the progress callback so
            # callers can report cross-Ref-ID milestone progress.
            if on_page_done is not None:
                try:
                    on_page_done()
                except Exception:
                    # Progress reporting must never break the pipeline.
                    logger.debug("on_page_done callback raised; continuing", exc_info=True)

    # Step 3: METS for the object (optional).
    if write_mets_file:
        try:
            tree = build_mets(
                ref_id=inputs.ref_id,
                ref_id_dir=out_dir,
                pages=page_entries,
                with_checksums=with_checksums,
                with_plaintext=write_plaintext,
            )
            write_mets(tree, out_dir / f"{inputs.ref_id}.mets.xml")
        except Exception as e:
            logger.error("[%s] METS build failed: %s", inputs.ref_id, e)
            # Don't tank the result — pages were processed; surface the METS error.
            result.pages.append(PageResult(
                stem="<mets>", order=0, ok=False, error=f"METS build failed: {e}",
            ))
    else:
        logger.debug("[%s] METS write skipped (write_mets_file=False)", inputs.ref_id)

    n_ok = sum(1 for p in result.pages if p.ok)
    n_fail = len(result.failed_pages)
    logger.info(
        "[%s] done: %d/%d pages OK, %d failed → %s",
        inputs.ref_id, n_ok, inputs.page_count, n_fail, out_dir,
    )
    return result
