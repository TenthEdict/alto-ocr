"""CLI entry point: ``python -m src.alto_ocr.main {process,batch} ...``"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .glmocr import (
    DEFAULT_NUM_PREDICT, GLMOCRConfig, GLMOCRError, check_endpoint, resolve_endpoint,
)
from .line_detect import DetectParams
from .alto_format import convert_to_institution_format
from .ocr import (
    GLMOCREngine,
    OCREngine,
    OCROptions,
    TesseractEngine,
    check_tesseract_version,
    find_tesseract,
)
from .pipeline import (
    PageProgressLogger,
    PageResult,
    RefIdResult,
    failed_ref_result,
    process_ref_id,
    record_ref_failure,
    skipped_ref_result,
)
from ._util import force_utf8_output
from .preprocess import PreprocessConfig
from .remediate import iter_object_dirs, repackage_object
from .triage import (
    DEFAULT_LOW_WC,
    DEFAULT_REVIEW_PC,
    ObjectConfidence,
    scan_object,
    scan_output_root,
    worst_pages,
    write_csvs,
)
from .walker import SkippedRefId, discover_collection, discover_ref_id, natural_key

app = typer.Typer(
    name="alto-ocr",
    help="OCR archival TIF scans → METS/ALTO XML + plain text.",
    add_completion=False,
    no_args_is_help=True,
)


# Run before constructing the Console so Rich picks up the UTF-8 code page.
force_utf8_output()
console = Console()
logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, console=console, show_path=False)],
    )


def _build_engine(
    engine_name: str,
    *,
    tesseract_path: Path | None,
    ocr_options: OCROptions,
    glmocr_endpoint: str | None,
    glmocr_model: str,
    glmocr_timeout: int,
    glmocr_num_predict: int = DEFAULT_NUM_PREDICT,
) -> OCREngine:
    """Resolve an engine name into a concrete OCREngine. Errors are reported
    to the user via typer.Exit so the message is actionable."""
    name = engine_name.lower().strip()
    if name == "tesseract":
        binary = find_tesseract(tesseract_path)
        console.print(f"[dim]engine:[/dim] tesseract  "
                      f"[dim]binary:[/dim] {binary} ({check_tesseract_version(binary)})")
        return TesseractEngine(binary=binary, options=ocr_options)
    if name == "glmocr":
        endpoint = resolve_endpoint(glmocr_endpoint)
        config = GLMOCRConfig(
            endpoint=endpoint,
            model=glmocr_model,
            timeout_s=glmocr_timeout,
            num_predict=glmocr_num_predict,
        )
        try:
            check_endpoint(config)
        except GLMOCRError as e:
            console.print(f"[red]glmocr setup error:[/red] {e}")
            raise typer.Exit(code=2)
        console.print(f"[dim]engine:[/dim] glmocr  "
                      f"[dim]endpoint:[/dim] {endpoint}  "
                      f"[dim]model:[/dim] {config.model}")
        return GLMOCREngine(config=config, detect_params=DetectParams())
    console.print(f"[red]unknown engine: {engine_name!r} (valid: tesseract, glmocr)[/red]")
    raise typer.Exit(code=2)


def _build_preprocess_config(
    min_dpi: int,
    no_deskew: bool, no_denoise: bool, no_clahe: bool,
    binarize: bool, no_border_crop: bool,
) -> PreprocessConfig:
    return PreprocessConfig(
        min_dpi=min_dpi,
        do_deskew=not no_deskew,
        do_denoise=not no_denoise,
        do_clahe=not no_clahe,
        do_binarize=binarize,
        do_border_crop=not no_border_crop,
    )


def _scan_confidence(results: list[RefIdResult]) -> dict[str, ObjectConfidence | None]:
    """Post-run confidence scan of each result's output directory.

    Reads the WC/PC attributes the Tesseract engine wrote into the ALTO
    files (report-time defaults for the thresholds; the ``triage`` command
    exposes the dials). ``None`` for a Ref ID whose ALTO folder doesn't
    exist (aborted before output)."""
    return {r.ref_id: scan_object(r.output_dir) for r in results}


def _summary_dict(
    results: list[RefIdResult],
    conf_by_ref: dict[str, ObjectConfidence | None] | None = None,
) -> dict:
    summary = {
        "ref_id_count": len(results),
        "ok_count": sum(1 for r in results if r.ok),
        "failed_page_total": sum(r.failed_page_count for r in results),
        "ref_ids": [
            {
                "ref_id": r.ref_id,
                "output_dir": str(r.output_dir),
                "ok": r.ok,
                "page_count": r.page_count,
                # failed_page_count == page_count for an aborted Ref ID (all
                # pages unproduced); the failed_pages list below still carries a
                # single descriptive error, so the two can legitimately differ.
                "failed_page_count": r.failed_page_count,
                "failed_pages": [
                    {"stem": p.stem, "order": p.order, "error": p.error}
                    for p in r.failed_pages
                ],
            }
            for r in results
        ],
    }
    if conf_by_ref is not None:
        objs = [c for c in conf_by_ref.values() if c is not None]
        total_scored_words = sum(o.scored_words for o in objs)
        summary["mean_wc"] = (
            round(sum(p.wc_sum for o in objs for p in o.pages) / total_scored_words, 3)
            if total_scored_words else None
        )
        summary["review_page_total"] = sum(len(o.review_pages) for o in objs)
        summary["unscored_page_total"] = sum(o.unscored_pages for o in objs)
        for entry in summary["ref_ids"]:
            c = conf_by_ref.get(entry["ref_id"])
            entry["confidence"] = c.summary_dict() if c is not None else None
    return summary


def _mean_wc_cell(conf: ObjectConfidence | None) -> str:
    """Rich-formatted Mean WC cell: red < 0.75, yellow < 0.90, dim n/a."""
    if conf is None or conf.mean_wc is None:
        return "[dim]n/a[/dim]"
    v = conf.mean_wc
    style = "red" if v < 0.75 else ("yellow" if v < 0.90 else "green")
    return f"[{style}]{v:.3f}[/{style}]"


def _print_summary(
    results: list[RefIdResult],
    conf_by_ref: dict[str, ObjectConfidence | None] | None = None,
) -> None:
    with_conf = conf_by_ref is not None
    table = Table(title="Summary")
    table.add_column("Ref ID", style="bold")
    table.add_column("Pages", justify="right")
    table.add_column("Failed", justify="right")
    if with_conf:
        table.add_column("Mean WC", justify="right")
        table.add_column("Review pgs", justify="right")
    table.add_column("Output")
    for r in results:
        failed = r.failed_page_count
        style = "red" if failed else "green"
        row = [
            r.ref_id,
            str(r.page_count),
            f"[{style}]{failed}[/{style}]",
        ]
        if with_conf:
            c = conf_by_ref.get(r.ref_id)
            n_review = len(c.review_pages) if c is not None else 0
            row.append(_mean_wc_cell(c))
            row.append(f"[yellow]{n_review}[/yellow]" if n_review else "0")
        row.append(str(r.output_dir))
        table.add_row(*row)
    console.print(table)


def _print_triage_hint(
    conf_by_ref: dict[str, ObjectConfidence | None], output_root: Path,
) -> None:
    """One-line pointer to the full triage report after a run."""
    objs = [c for c in conf_by_ref.values() if c is not None]
    if not any(o.scored_pages for o in objs):
        return  # nothing scored (e.g. a GLMOCR run) — no queue to point at
    n_review = sum(len(o.review_pages) for o in objs)
    cmd = f'python -m src.alto_ocr.main triage "{output_root}"'
    if n_review:
        console.print(
            f"[yellow]{n_review} page(s) below the review threshold "
            f"(PC < {DEFAULT_REVIEW_PC:.2f}).[/yellow] Worst-first queue: [bold]{cmd}[/bold]"
        )
    else:
        console.print(
            f"[dim]No pages below the review threshold (PC < {DEFAULT_REVIEW_PC:.2f}); "
            f"full report: {cmd}[/dim]"
        )


# ---------- Common option types -------------------------------------------------

# Typer 0.25 requires Optional[...] (not "X | None") for option types.
EngineOpt = typer.Option("tesseract", "--engine",
                         help="OCR engine: 'tesseract' (default; its single pass also "
                              "emits the per-word confidences written to ALTO as WC/PC "
                              "for remediation triage) or 'glmocr' (multimodal; better "
                              "raw accuracy on handwriting but no confidence values; "
                              "requires local Ollama).")
TesseractOpt = typer.Option(None, "--tesseract-path",
                            help="Path to tesseract binary (overrides PATH/TESSERACT_PATH).")
GLMOCREndpointOpt = typer.Option(None, "--glmocr-endpoint",
                                 help="Ollama HTTP endpoint for the glm-ocr engine. "
                                      "Default: $OLLAMA_HOST or http://localhost:11434.")
GLMOCRModelOpt = typer.Option("glm-ocr:q8_0", "--glmocr-model",
                              help="Ollama model tag for the glm-ocr engine.")
GLMOCRTimeoutOpt = typer.Option(120, "--glmocr-timeout",
                                help="Per-page glm-ocr request timeout in seconds.")
GLMOCRNumPredictOpt = typer.Option(
    DEFAULT_NUM_PREDICT, "--glmocr-num-predict",
    help="Max tokens glm-ocr may generate per block (default 8192). Raise (e.g. "
         "16384) if dense pages truncate — look for 'stopped with reason=length' "
         "in the log; costs proportionally more time only on those dense pages.")
LangOpt = typer.Option("eng", "--ocr-lang", help="Tesseract language code, e.g. 'eng', 'fra', 'eng+lat'.")
PsmOpt = typer.Option(3, "--psm", help="Tesseract page segmentation mode (3=auto, 4=column, 6=block).")
OemOpt = typer.Option(3, "--oem", help="Tesseract OCR engine mode (3=default LSTM).")
MinDpiOpt = typer.Option(300, "--min-dpi", help="Upscale low-DPI scans to at least this DPI before OCR.")
NoDeskewOpt = typer.Option(False, "--no-deskew", help="Skip deskew stage.")
NoDenoiseOpt = typer.Option(False, "--no-denoise", help="Skip bilateral denoise stage.")
NoClaheOpt = typer.Option(False, "--no-clahe", help="Skip CLAHE contrast normalization.")
BinarizeOpt = typer.Option(False, "--binarize/--no-binarize",
                           help="Adaptive-threshold the image before OCR. OFF by default since "
                                "v0.8.8: Tesseract 5 thresholds internally, and pre-binarized "
                                "input measurably lowered WC on every collection tried. Both "
                                "spellings accepted so existing --no-binarize scripts keep working.")
NoBorderCropOpt = typer.Option(False, "--no-border-crop", help="Skip black-border auto-crop.")
NoChecksumsOpt = typer.Option(False, "--no-checksums", help="Skip MD5 checksums in METS (faster on large objects).")
NoMetsOpt = typer.Option(False, "--no-mets",
                         help="Don't write the per-Ref-ID METS file (ALTO/Text are unaffected).")
NoPlaintextOpt = typer.Option(False, "--no-plaintext",
                              help="Skip the Text/ folder. ALTO is still produced; METS (if written) "
                                   "omits the Text fileGrp.")
ForceReocrOpt = typer.Option(False, "--force-reocr",
                             help="Overwrite output pages that carry human-review corrections. "
                                  "Without this flag, an object whose existing ALTO has review "
                                  "provenance is refused (reported as failed, nothing touched) so "
                                  "a re-run cannot silently destroy correction work.")
DebugDirOpt = typer.Option(None, "--debug-preprocessed",
                           help="If set, dump the preprocessed images Tesseract actually saw to this directory.")
VerboseOpt = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging.")
SummaryOpt = typer.Option(None, "--summary-json",
                          help="Write a machine-readable summary to this path.")


# ---------- Commands ------------------------------------------------------------

@app.command()
def process(
    object_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True,
                                      help="One Ref ID directory containing TIFs (at any nesting depth)."),
    output_root: Path = typer.Argument(..., help="Output root; <output_root>/<RefID>/ will be created."),
    engine: str = EngineOpt,
    tesseract_path: Optional[Path] = TesseractOpt,
    glmocr_endpoint: Optional[str] = GLMOCREndpointOpt,
    glmocr_model: str = GLMOCRModelOpt,
    glmocr_timeout: int = GLMOCRTimeoutOpt,
    glmocr_num_predict: int = GLMOCRNumPredictOpt,
    ocr_lang: str = LangOpt,
    psm: int = PsmOpt,
    oem: int = OemOpt,
    min_dpi: int = MinDpiOpt,
    no_deskew: bool = NoDeskewOpt,
    no_denoise: bool = NoDenoiseOpt,
    no_clahe: bool = NoClaheOpt,
    binarize: bool = BinarizeOpt,
    no_border_crop: bool = NoBorderCropOpt,
    no_checksums: bool = NoChecksumsOpt,
    no_mets: bool = NoMetsOpt,
    no_plaintext: bool = NoPlaintextOpt,
    force_reocr: bool = ForceReocrOpt,
    debug_preprocessed: Optional[Path] = DebugDirOpt,
    summary_json: Optional[Path] = SummaryOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Process one Ref ID."""
    _setup_logging(verbose)

    found = discover_ref_id(object_dir)
    if found is None:
        console.print(f"[red]No TIFs or PDFs found in {object_dir}[/red]")
        raise typer.Exit(code=1)
    if isinstance(found, SkippedRefId):
        # Not "no TIFs" — the object has pages but can't be processed as-is.
        console.print(f"[red]{found.ref_id}:[/red] {found.reason}")
        raise typer.Exit(code=1)
    inputs = found

    options = OCROptions(language=ocr_lang, psm=psm, oem=oem)
    engine_obj = _build_engine(
        engine,
        tesseract_path=tesseract_path,
        ocr_options=options,
        glmocr_endpoint=glmocr_endpoint,
        glmocr_model=glmocr_model,
        glmocr_timeout=glmocr_timeout,
        glmocr_num_predict=glmocr_num_predict,
    )

    cfg = _build_preprocess_config(min_dpi, no_deskew, no_denoise, no_clahe, binarize, no_border_crop)

    progress = PageProgressLogger(inputs.page_count)
    result = process_ref_id(
        inputs, output_root,
        engine=engine_obj,
        preprocess_config=cfg,
        debug_preprocessed_dir=debug_preprocessed,
        with_checksums=not no_checksums,
        write_mets_file=not no_mets,
        write_plaintext=not no_plaintext,
        force_reocr=force_reocr,
        on_page_done=progress.tick,
    )

    conf = _scan_confidence([result])
    _print_summary([result], conf)
    if summary_json:
        summary_json.write_text(json.dumps(_summary_dict([result], conf), indent=2),
                                encoding="utf-8")
        console.print(f"[dim]summary:[/dim] {summary_json}")
    _print_triage_hint(conf, output_root)
    raise typer.Exit(code=0 if result.ok else 1)


def _process_one_for_pool(args: tuple) -> RefIdResult:
    """Top-level helper so ProcessPoolExecutor can pickle it."""
    (inputs, output_root, engine, cfg, debug_dir,
     with_checksums, write_mets_file, write_plaintext, force_reocr) = args
    return process_ref_id(
        inputs, output_root,
        engine=engine,
        preprocess_config=cfg,
        debug_preprocessed_dir=debug_dir,
        with_checksums=with_checksums,
        write_mets_file=write_mets_file,
        write_plaintext=write_plaintext,
        force_reocr=force_reocr,
    )


def _pool_worker_init(verbose: bool) -> None:
    """Logging bootstrap for spawned pool workers.

    Workers start with no logging configured, so without this their INFO
    lines (per-Ref-ID "done" progress, page-failure context) vanish and
    warnings fall to Python's bare lastResort handler. A plain stderr
    handler keeps worker output visible and PID-labeled; Rich formatting
    stays parent-only.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[worker %(process)d] %(levelname)s %(message)s",
    )


def _classify_broken_pool_futures(futures: dict, harvested_ids: set) -> tuple[list, list, list]:
    """Partition the not-yet-harvested pool futures after a BrokenProcessPool.

    A BrokenProcessPool marks *every* still-pending future broken, but a future
    can be DONE with a real result (its ALTO/Text/METS already written to disk by
    process_ref_id) yet simply not have been yielded by ``as_completed`` when the
    break fired. Harvesting those is what stops a fully-OCR'd object from being
    falsely reported failed and needlessly re-run. Returns three lists:

      * ``completed`` — RefIdResults from futures that genuinely finished; keep.
      * ``failed`` — ``(inputs, exc)`` for futures that raised a *real* per-Ref-ID
        error (something other than the pool death); record as failed.
      * ``not_run`` — inputs whose future produced no result (pending, running,
        cancelled, or killed by the pool death); must be re-run.

    Skips any future whose Ref ID is already in ``harvested_ids`` (i.e. yielded
    by ``as_completed`` before the break).
    """
    completed: list = []
    failed: list = []
    not_run: list = []
    for fut, inputs in futures.items():
        if inputs.ref_id in harvested_ids:
            continue
        if fut.done() and not fut.cancelled():
            try:
                completed.append(fut.result())  # finished before the pool broke
                continue
            except BrokenProcessPool:
                pass  # this future was killed by the pool death -> re-run
            except Exception as exc:  # noqa: BLE001 - a real per-Ref-ID error
                failed.append((inputs, exc))
                continue
        not_run.append(inputs)  # pending / running / cancelled -> re-run
    return completed, failed, not_run


@app.command()
def batch(
    collection_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True,
                                          help="Collection directory containing per-Ref-ID subdirectories."),
    output_root: Path = typer.Argument(..., help="Output root; one subdirectory per Ref ID."),
    workers: int = typer.Option(1, "--workers", "-w",
                                help="Parallel Ref-ID workers. Tesseract is single-threaded per call; "
                                     "tune to physical CPU count."),
    engine: str = EngineOpt,
    tesseract_path: Optional[Path] = TesseractOpt,
    glmocr_endpoint: Optional[str] = GLMOCREndpointOpt,
    glmocr_model: str = GLMOCRModelOpt,
    glmocr_timeout: int = GLMOCRTimeoutOpt,
    glmocr_num_predict: int = GLMOCRNumPredictOpt,
    ocr_lang: str = LangOpt,
    psm: int = PsmOpt,
    oem: int = OemOpt,
    min_dpi: int = MinDpiOpt,
    no_deskew: bool = NoDeskewOpt,
    no_denoise: bool = NoDenoiseOpt,
    no_clahe: bool = NoClaheOpt,
    binarize: bool = BinarizeOpt,
    no_border_crop: bool = NoBorderCropOpt,
    no_checksums: bool = NoChecksumsOpt,
    no_mets: bool = NoMetsOpt,
    no_plaintext: bool = NoPlaintextOpt,
    force_reocr: bool = ForceReocrOpt,
    debug_preprocessed: Optional[Path] = DebugDirOpt,
    summary_json: Optional[Path] = SummaryOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Process every Ref ID in a collection directory."""
    _setup_logging(verbose)

    items, skipped = discover_collection(collection_dir)
    if not items and not skipped:
        console.print(f"[yellow]No Ref IDs with TIFs found in {collection_dir}[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"Discovered [bold]{len(items)}[/bold] Ref ID(s) "
                  f"({sum(i.page_count for i in items)} pages total).")
    for sk in skipped:
        console.print(f"[red]SKIPPED {sk.ref_id}:[/red] {sk.reason}")

    options = OCROptions(language=ocr_lang, psm=psm, oem=oem)
    engine_obj = _build_engine(
        engine,
        tesseract_path=tesseract_path,
        ocr_options=options,
        glmocr_endpoint=glmocr_endpoint,
        glmocr_model=glmocr_model,
        glmocr_timeout=glmocr_timeout,
        glmocr_num_predict=glmocr_num_predict,
    )

    cfg = _build_preprocess_config(min_dpi, no_deskew, no_denoise, no_clahe, binarize, no_border_crop)

    results: list[RefIdResult] = []
    if workers <= 1:
        # Page-level progress across all Ref IDs. Workers>1 path uses
        # subprocesses, so a shared in-memory counter wouldn't work there —
        # the existing per-Ref-ID "[refid] done: ..." lines cover that case.
        total_pages = sum(i.page_count for i in items)
        progress = PageProgressLogger(total_pages)
        for inputs in items:
            try:
                results.append(process_ref_id(
                    inputs, output_root,
                    engine=engine_obj,
                    preprocess_config=cfg,
                    debug_preprocessed_dir=debug_preprocessed,
                    with_checksums=not no_checksums,
                    write_mets_file=not no_mets,
                    write_plaintext=not no_plaintext,
                    force_reocr=force_reocr,
                    on_page_done=progress.tick,
                ))
            except Exception as e:  # noqa: BLE001 - one bad Ref ID must not abort the batch
                record_ref_failure(results, inputs, output_root, e)
    else:
        payloads = [
            (inputs, output_root, engine_obj, cfg,
             debug_preprocessed, not no_checksums, not no_mets, not no_plaintext,
             force_reocr)
            for inputs in items
        ]
        # Map each future to its RefIdInputs so a worker-side failure (a
        # Ref-ID-level exception, or a crashed/broken pool worker) is recorded
        # as a failed Ref ID and the batch keeps going — a single bad object
        # can't throw away hours of an unattended run.
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_pool_worker_init,
                                 initargs=(verbose,)) as ex:
            futures = {ex.submit(_process_one_for_pool, p): p[0] for p in payloads}
            for fut in as_completed(futures):
                inputs = futures[fut]
                try:
                    results.append(fut.result())
                except BrokenProcessPool:
                    # The POOL itself died (a worker crashed / was OOM-killed),
                    # not a single bad object. Every still-pending future raises
                    # this — but some may have ALREADY finished and written their
                    # output before the break, so harvest those instead of
                    # blanket-failing them (a finished object must not be flagged
                    # failed and needlessly re-run). Then record the genuinely
                    # un-run Ref IDs with a clear "pool died, re-run" reason and
                    # stop. (Distinct from the per-Ref-ID isolation below.)
                    harvested = {r.ref_id for r in results}
                    completed, failed, not_run = _classify_broken_pool_futures(futures, harvested)
                    results.extend(completed)
                    for ri, exc in failed:
                        record_ref_failure(results, ri, output_root, exc)
                    if not_run:
                        logger.error("Worker pool died (a worker likely crashed or was OOM-killed). "
                                     "%d Ref ID(s) did NOT run and must be re-run: %s",
                                     len(not_run), ", ".join(ri.ref_id for ri in not_run))
                        console.print(f"[red]FATAL: worker pool died — {len(not_run)} Ref ID(s) not "
                                      f"processed; re-run them (lower --workers if it recurs).[/red]")
                        for ri in not_run:
                            results.append(failed_ref_result(
                                ri, output_root,
                                RuntimeError("worker pool died before this Ref ID was processed "
                                             "(not OCR'd) — re-run it")))
                    else:
                        logger.warning("Worker pool reported broken, but all Ref IDs had already "
                                       "completed or been recorded; nothing to re-run.")
                    break
                except Exception as e:  # noqa: BLE001 - isolate per-Ref-ID failures
                    record_ref_failure(results, inputs, output_root, e)

    # Objects dropped at discovery (stem collisions) surface as failed rows:
    # a batch must never exit 0 while an object silently vanished from it.
    for sk in skipped:
        results.append(skipped_ref_result(sk, output_root))
    results.sort(key=lambda r: r.ref_id)

    conf = _scan_confidence(results)
    _print_summary(results, conf)
    summary = _summary_dict(results, conf)
    if summary_json:
        summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        console.print(f"[dim]summary:[/dim] {summary_json}")
    _print_triage_hint(conf, output_root)

    n_failed = sum(1 for r in results if not r.ok)
    raise typer.Exit(code=0 if n_failed == 0 else 1)


@app.command()
def triage(
    output_root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True,
                                       help="Output root from process/batch (contains <RefID>/ALTO/), "
                                            "or a single Ref ID output directory."),
    low_wc: float = typer.Option(DEFAULT_LOW_WC, "--low-wc",
                                 help="Words with WC below this count as low-confidence (0..1)."),
    review_pc: float = typer.Option(DEFAULT_REVIEW_PC, "--review-pc",
                                    help="Scored pages with PC below this are flagged for review (0..1)."),
    top: int = typer.Option(15, "--top",
                            help="How many worst pages to print (the CSV always has every page)."),
    csv_dir: Optional[Path] = typer.Option(None, "--csv-dir",
                                           help="Where to write triage_objects.csv / triage_pages.csv "
                                                "(default: the output root itself)."),
    no_csv: bool = typer.Option(False, "--no-csv", help="Print only; skip the CSV files."),
    verbose: bool = VerboseOpt,
) -> None:
    """Rank existing OCR output by confidence, worst-first, for remediation.

    Reads the WC/PC attributes Tesseract runs write into the ALTO files.
    Re-runnable any time against any output tree; thresholds are report-time
    dials, not baked into the output. Pages show as: scored (has WC — the
    correction queue), unscored (GLMOCR output, no confidence — needs full
    proofread), empty (blank page), or failed (pipeline stub — re-run it).
    """
    _setup_logging(verbose)

    objects = scan_output_root(output_root, low_wc=low_wc, review_pc=review_pc)
    if not objects:
        console.print(f"[red]No ALTO output found under {output_root} "
                      f"(expected <RefID>/ALTO/*.xml or ALTO/*.xml)[/red]")
        raise typer.Exit(code=1)

    obj_table = Table(title=f"Objects, worst-first (low WC < {low_wc:.2f}, review PC < {review_pc:.2f})")
    obj_table.add_column("Ref ID", style="bold")
    obj_table.add_column("Pages", justify="right")
    obj_table.add_column("Mean WC", justify="right")
    obj_table.add_column("Min PC (page)", justify="right")
    obj_table.add_column("Low words", justify="right")
    obj_table.add_column("Review pgs", justify="right")
    obj_table.add_column("Unscored", justify="right")
    obj_table.add_column("Failed", justify="right")
    for o in objects:
        pct_low = (100.0 * o.low_words / o.scored_words) if o.scored_words else None
        min_pc = f"{o.min_pc:.2f} ({o.min_pc_stem})" if o.min_pc is not None else "—"
        n_review = len(o.review_pages)
        obj_table.add_row(
            o.ref_id,
            str(o.page_count),
            _mean_wc_cell(o),
            min_pc,
            f"{pct_low:.1f}%" if pct_low is not None else "—",
            f"[yellow]{n_review}[/yellow]" if n_review else "0",
            f"[magenta]{o.unscored_pages}[/magenta]" if o.unscored_pages else "0",
            f"[red]{o.failed_pages}[/red]" if o.failed_pages else "0",
        )
    console.print(obj_table)

    queue = [rp for rp in worst_pages(objects) if rp[1].status in ("failed", "scored")]
    if queue:
        page_table = Table(title=f"Review queue — worst {min(top, len(queue))} of {len(queue)} pages")
        page_table.add_column("Ref ID", style="bold")
        page_table.add_column("Page")
        page_table.add_column("Status")
        page_table.add_column("PC", justify="right")
        page_table.add_column("Words", justify="right")
        page_table.add_column("Low words", justify="right")
        for ref_id, p in queue[:top]:
            page_table.add_row(
                ref_id,
                p.stem,
                "[red]failed[/red]" if p.status == "failed" else "scored",
                f"{p.pc:.2f}" if p.pc is not None else "—",
                str(p.words),
                f"{p.pct_low:.1f}%" if p.pct_low is not None else "—",
            )
        console.print(page_table)

    n_unscored = sum(o.unscored_pages for o in objects)
    if n_unscored:
        console.print(f"[magenta]{n_unscored} unscored page(s)[/magenta] (no WC — GLMOCR output "
                      f"or pre-confidence runs): no signal to narrow the proofread.")
    all_scored = [o for o in objects if o.mean_wc is not None]
    handwriting = [o for o in all_scored if o.mean_wc < 0.60]
    if handwriting:
        console.print(f"[yellow]Handwriting candidates (mean WC < 0.60):[/yellow] "
                      f"{', '.join(o.ref_id for o in handwriting)} — consider re-running "
                      f"these Ref IDs with --engine glmocr.")

    if not no_csv:
        obj_csv, pages_csv = write_csvs(objects, csv_dir or output_root)
        console.print(f"[dim]csv:[/dim] {obj_csv}")
        console.print(f"[dim]csv:[/dim] {pages_csv}")


@app.command()
def repackage(
    output_root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True,
                                       help="Output root (contains <RefID>/ALTO/), or a "
                                            "single object output directory."),
    no_checksums: bool = NoChecksumsOpt,
    no_mets: bool = NoMetsOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Regenerate Text/ transcripts and the METS from hand-corrected ALTO.

    Run after review (the GUI, or an external editor such as eScriptorium):
    the corrected ALTO is the source of truth. Transcripts re-render through
    the same canonical renderer the review GUI's save uses, and the METS is
    rebuilt so its file sizes and checksums match the edited files, keeping
    the existing structMap page order. Idempotent — byte-identical
    transcripts are not rewritten, so re-running is free. Objects without a
    METS or Text/ folder (runs that opted out) are left that way.
    """
    _setup_logging(verbose)

    objects = iter_object_dirs(output_root)
    if not objects:
        console.print(f"[red]No objects with an ALTO folder found under {output_root}[/red]")
        raise typer.Exit(code=1)

    table = Table(title="Repackage")
    table.add_column("Ref ID", style="bold")
    table.add_column("Pages", justify="right")
    table.add_column("Txt updated", justify="right")
    table.add_column("METS")
    table.add_column("Notes")
    n_problems = 0
    for obj in objects:
        r = repackage_object(
            obj, with_checksums=not no_checksums, write_mets_file=not no_mets,
        )
        n_problems += len(r.problems)
        table.add_row(
            r.ref_id,
            str(r.pages),
            str(r.txt_written),
            "rewritten" if r.mets_written else "—",
            f"[yellow]{'; '.join(r.problems)}[/yellow]" if r.problems else "",
        )
    console.print(table)
    raise typer.Exit(code=0 if n_problems == 0 else 1)


_TIF_EXTS = (".tif", ".tiff", ".TIF", ".TIFF")


def _resolve_dpi_from_tif(tif_path: Path | None, *, min_dpi: int) -> int:
    """Read DPI from a TIF; if missing or below ``min_dpi``, return ``min_dpi``.

    Mirrors ``preprocess.preprocess()``'s effective-DPI logic so the
    reformat command converts coordinates to mm10 with the same DPI the
    pipeline used at OCR time.
    """
    if tif_path is None or not tif_path.is_file():
        return min_dpi
    try:
        from PIL import Image
        with Image.open(tif_path) as img:
            d = img.info.get("dpi")
            if d:
                src = int(round(float(d[0])))
                return max(src, min_dpi)
    except Exception:
        pass
    return min_dpi


def _find_source_tif(alto_path: Path, source_tiff_dir: Path | None) -> Path | None:
    """Locate the source TIF for an ALTO file.

    Search order:
      1. ``source_tiff_dir`` if the caller provided one (typical case:
         a ``preservation-files/preservation/<RefID>/`` directory).
      2. A sibling ``TIFF/`` folder next to ``ALTO/`` — left in place for
         older outputs produced before the TIFF copy was removed.
    """
    stem = alto_path.stem
    search_dirs = []
    if source_tiff_dir is not None and source_tiff_dir.is_dir():
        search_dirs.append(source_tiff_dir)
    sibling = alto_path.parent.parent / "TIFF"
    if sibling.is_dir():
        search_dirs.append(sibling)
    for d in search_dirs:
        for ext in _TIF_EXTS:
            candidate = d / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
    return None


@app.command()
def reformat(
    target: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True,
        help="A Ref ID directory (containing ALTO/) or an ALTO/ folder directly.",
    ),
    dpi: Optional[int] = typer.Option(
        None, "--dpi",
        help="Override the DPI used for px→mm10 conversion. Default: read each "
             "page's DPI from the matching source TIF (see --source-tiff-dir), "
             "with --min-dpi as the floor. Use this when no source TIFs are "
             "available or the embedded DPI is wrong.",
    ),
    min_dpi: int = typer.Option(
        300, "--min-dpi",
        help="Floor for the DPI used during conversion (matches the pipeline's "
             "preprocessing default; coordinates were OCR'd at this DPI even if "
             "the source TIF was lower).",
    ),
    source_tiff_dir: Optional[Path] = typer.Option(
        None, "--source-tiff-dir",
        help="Directory containing the source TIFs (e.g. preservation-files/"
             "preservation/<RefID>/) used to read per-page DPI. If omitted, "
             "the command falls back to a sibling TIFF/ folder when present, "
             "or to --min-dpi otherwise.",
    ),
    verbose: bool = VerboseOpt,
) -> None:
    """Rewrite existing ALTO files to the institution's reference format.

    Each file is normalized end-to-end: ALTO v2 namespace + schema, mm10
    measurement unit (with HPOS/VPOS/WIDTH/HEIGHT converted from pixels
    using each page's effective DPI), sequential ``P{N}`` Page IDs with
    ``P{N}_TB00001`` / ``P{N}_TL00001`` / ``P{N}_ST00001`` etc. child IDs,
    Top/Left/Right/BottomMargin elements, ``<sourceImageInformation>``
    referencing the source TIF basename, per-element-type attribute
    ordering, tab indentation, and a double-quoted XML declaration.

    Files are processed in natural-sort order; their 1-based position
    becomes the Page ID's number. Files are overwritten in place — back
    up the directory first if you need a rollback path. The converter is
    idempotent: re-running on already-converted files reproduces the
    same output.
    """
    _setup_logging(verbose)

    alto_dir = target / "ALTO" if (target / "ALTO").is_dir() else target
    xml_files = sorted(
        [p for p in alto_dir.glob("*.xml") if p.is_file()],
        key=natural_key,
    )
    if not xml_files:
        console.print(f"[red]No ALTO XML files found under {alto_dir}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Reformatting {len(xml_files)} ALTO file(s) in [cyan]{alto_dir}[/cyan]")
    n_ok = 0
    for idx, path in enumerate(xml_files, start=1):
        try:
            tif = _find_source_tif(path, source_tiff_dir)
            page_dpi = dpi if dpi is not None else _resolve_dpi_from_tif(tif, min_dpi=min_dpi)
            source_filename = tif.name if tif is not None else f"{path.stem}.tiff"
            convert_to_institution_format(
                path, page_index=idx, dpi=page_dpi, source_filename=source_filename,
            )
            n_ok += 1
            console.print(f"  [green]OK[/green] {path.name} -> P{idx} (dpi={page_dpi})")
        except Exception as e:
            console.print(f"  [red]FAIL[/red] {path.name}: {e}")

    console.print(f"[bold]Done:[/bold] {n_ok}/{len(xml_files)} reformatted")
    raise typer.Exit(code=0 if n_ok == len(xml_files) else 1)


@app.command()
def doctor(
    tesseract_path: Optional[Path] = TesseractOpt,
    glmocr_endpoint: Optional[str] = GLMOCREndpointOpt,
    glmocr_model: str = GLMOCRModelOpt,
) -> None:
    """Verify the runtime environment: Python deps + both OCR engines."""
    _setup_logging(False)
    console.print("[bold]alto_ocr — environment check[/bold]")

    # Python deps are the only hard requirement.
    dep_failed = False
    for dep in ("cv2", "PIL", "numpy", "lxml", "typer", "rich"):
        try:
            __import__(dep)
            console.print(f"[green]OK[/green] {dep}")
        except ImportError as e:
            console.print(f"[red]FAIL[/red] {dep}: {e}")
            dep_failed = True

    # Tesseract is the DEFAULT engine — its per-word confidences drive the
    # remediation triage. Absence is still only a warning: glm-ocr alone can
    # run handwriting-only jobs.
    try:
        binary = find_tesseract(tesseract_path)
        console.print(f"[green]OK[/green] tesseract (default engine): {binary} "
                      f"({check_tesseract_version(binary)})")
        tesseract_ok = True
    except Exception as e:
        console.print(f"[yellow]WARN[/yellow] tesseract (default engine): {e}")
        tesseract_ok = False

    # glm-ocr is the optional handwriting engine (--engine glmocr): confirm
    # Ollama is reachable and the model is pulled. A miss is a warning —
    # Tesseract runs never touch it.
    endpoint = resolve_endpoint(glmocr_endpoint)
    try:
        model = check_endpoint(GLMOCRConfig(endpoint=endpoint, model=glmocr_model))
        console.print(f"[green]OK[/green] glm-ocr (handwriting engine): {endpoint}, model {model!r}")
        glmocr_ok = True
    except GLMOCRError as e:
        console.print(f"[yellow]WARN[/yellow] glm-ocr (handwriting engine): {e}")
        glmocr_ok = False

    if not (glmocr_ok or tesseract_ok):
        console.print("[red]FAIL[/red] no usable OCR engine "
                      "(need Tesseract, or Ollama + glm-ocr for handwriting).")

    raise typer.Exit(code=1 if (dep_failed or not (glmocr_ok or tesseract_ok)) else 0)


if __name__ == "__main__":
    app()
