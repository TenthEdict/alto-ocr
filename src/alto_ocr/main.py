"""CLI entry point: ``python -m src.alto_ocr.main {process,batch} ...``"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .glmocr import GLMOCRConfig, GLMOCRError, check_endpoint, resolve_endpoint
from .line_detect import DetectParams
from .ocr import (
    GLMOCREngine,
    OCREngine,
    OCROptions,
    TesseractEngine,
    _convert_to_institution_format,
    check_tesseract_version,
    find_tesseract,
)
from .pipeline import PageProgressLogger, RefIdResult, process_ref_id
from .preprocess import PreprocessConfig
from .walker import _natural_key, discover_collection, discover_ref_id

app = typer.Typer(
    name="alto-ocr",
    help="OCR archival TIF scans → METS/ALTO XML + plain text.",
    add_completion=False,
    no_args_is_help=True,
)


def _force_utf8_output() -> None:
    """Make stdout/stderr tolerate non-ASCII output on Windows consoles.

    Rich (the logging handler + the summary ``Table``) and a handful of log
    lines emit characters — ``->`` rendered as ``→``, ``←``, em
    dashes — that the legacy Windows cp1252 console codec can't encode,
    which otherwise crashes the CLI at the summary step (observed under
    redirected output). Reconfigure the streams to UTF-8 with a lenient
    error handler, and on Windows switch the console output code page to
    UTF-8 so Rich's legacy-console renderer (which encodes via the code
    page, not the Python stream) succeeds too.

    All best-effort: every step is guarded so a locked-down environment
    can't stop the tool from running. No-op on POSIX, where stdout is
    already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            # 65001 = CP_UTF8. Output code page only; leave the input code
            # page alone so console input handling is unaffected.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # best effort; never fatal
            pass


# Run before constructing the Console so Rich picks up the UTF-8 code page.
_force_utf8_output()
console = Console()


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
    no_binarize: bool, no_border_crop: bool,
) -> PreprocessConfig:
    return PreprocessConfig(
        min_dpi=min_dpi,
        do_deskew=not no_deskew,
        do_denoise=not no_denoise,
        do_clahe=not no_clahe,
        do_binarize=not no_binarize,
        do_border_crop=not no_border_crop,
    )


def _summary_dict(results: list[RefIdResult]) -> dict:
    return {
        "ref_id_count": len(results),
        "ok_count": sum(1 for r in results if r.ok),
        "failed_page_total": sum(len(r.failed_pages) for r in results),
        "ref_ids": [
            {
                "ref_id": r.ref_id,
                "output_dir": str(r.output_dir),
                "ok": r.ok,
                "page_count": r.page_count,
                "failed_pages": [
                    {"stem": p.stem, "order": p.order, "error": p.error}
                    for p in r.failed_pages
                ],
            }
            for r in results
        ],
    }


def _print_summary(results: list[RefIdResult]) -> None:
    table = Table(title="Summary")
    table.add_column("Ref ID", style="bold")
    table.add_column("Pages", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Output")
    for r in results:
        failed = len(r.failed_pages)
        style = "red" if failed else "green"
        table.add_row(
            r.ref_id,
            str(r.page_count),
            f"[{style}]{failed}[/{style}]",
            str(r.output_dir),
        )
    console.print(table)


# ---------- Common option types -------------------------------------------------

# Typer 0.25 requires Optional[...] (not "X | None") for option types.
EngineOpt = typer.Option("glmocr", "--engine",
                         help="OCR engine: 'glmocr' (default, high-accuracy multimodal; "
                              "requires local Ollama) or 'tesseract' (fallback; faster "
                              "setup, lower accuracy on this institution's material).")
TesseractOpt = typer.Option(None, "--tesseract-path",
                            help="Path to tesseract binary (overrides PATH/TESSERACT_PATH).")
GLMOCREndpointOpt = typer.Option(None, "--glmocr-endpoint",
                                 help="Ollama HTTP endpoint for the glm-ocr engine. "
                                      "Default: $OLLAMA_HOST or http://localhost:11434.")
GLMOCRModelOpt = typer.Option("glm-ocr:latest", "--glmocr-model",
                              help="Ollama model tag for the glm-ocr engine.")
GLMOCRTimeoutOpt = typer.Option(120, "--glmocr-timeout",
                                help="Per-page glm-ocr request timeout in seconds.")
LangOpt = typer.Option("eng", "--ocr-lang", help="Tesseract language code, e.g. 'eng', 'fra', 'eng+lat'.")
PsmOpt = typer.Option(3, "--psm", help="Tesseract page segmentation mode (3=auto, 4=column, 6=block).")
OemOpt = typer.Option(3, "--oem", help="Tesseract OCR engine mode (3=default LSTM).")
MinDpiOpt = typer.Option(300, "--min-dpi", help="Upscale low-DPI scans to at least this DPI before OCR.")
NoDeskewOpt = typer.Option(False, "--no-deskew", help="Skip deskew stage.")
NoDenoiseOpt = typer.Option(False, "--no-denoise", help="Skip bilateral denoise stage.")
NoClaheOpt = typer.Option(False, "--no-clahe", help="Skip CLAHE contrast normalization.")
NoBinarizeOpt = typer.Option(False, "--no-binarize", help="Skip adaptive thresholding (feed grayscale to OCR).")
NoBorderCropOpt = typer.Option(False, "--no-border-crop", help="Skip black-border auto-crop.")
NoChecksumsOpt = typer.Option(False, "--no-checksums", help="Skip MD5 checksums in METS (faster on large objects).")
NoMetsOpt = typer.Option(False, "--no-mets",
                         help="Don't write the per-Ref-ID METS file (ALTO/Text are unaffected).")
NoPlaintextOpt = typer.Option(False, "--no-plaintext",
                              help="Skip the Text/ folder. ALTO is still produced; METS (if written) "
                                   "omits the Text fileGrp.")
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
    ocr_lang: str = LangOpt,
    psm: int = PsmOpt,
    oem: int = OemOpt,
    min_dpi: int = MinDpiOpt,
    no_deskew: bool = NoDeskewOpt,
    no_denoise: bool = NoDenoiseOpt,
    no_clahe: bool = NoClaheOpt,
    no_binarize: bool = NoBinarizeOpt,
    no_border_crop: bool = NoBorderCropOpt,
    no_checksums: bool = NoChecksumsOpt,
    no_mets: bool = NoMetsOpt,
    no_plaintext: bool = NoPlaintextOpt,
    debug_preprocessed: Optional[Path] = DebugDirOpt,
    summary_json: Optional[Path] = SummaryOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Process one Ref ID."""
    _setup_logging(verbose)

    inputs = discover_ref_id(object_dir)
    if inputs is None:
        console.print(f"[red]No TIFs found in {object_dir}[/red]")
        raise typer.Exit(code=1)

    options = OCROptions(language=ocr_lang, psm=psm, oem=oem)
    engine_obj = _build_engine(
        engine,
        tesseract_path=tesseract_path,
        ocr_options=options,
        glmocr_endpoint=glmocr_endpoint,
        glmocr_model=glmocr_model,
        glmocr_timeout=glmocr_timeout,
    )

    cfg = _build_preprocess_config(min_dpi, no_deskew, no_denoise, no_clahe, no_binarize, no_border_crop)

    progress = PageProgressLogger(inputs.page_count)
    result = process_ref_id(
        inputs, output_root,
        engine=engine_obj,
        preprocess_config=cfg,
        debug_preprocessed_dir=debug_preprocessed,
        with_checksums=not no_checksums,
        write_mets_file=not no_mets,
        write_plaintext=not no_plaintext,
        on_page_done=progress.tick,
    )

    _print_summary([result])
    if summary_json:
        summary_json.write_text(json.dumps(_summary_dict([result]), indent=2), encoding="utf-8")
        console.print(f"[dim]summary:[/dim] {summary_json}")
    raise typer.Exit(code=0 if result.ok else 1)


def _process_one_for_pool(args: tuple) -> RefIdResult:
    """Top-level helper so ProcessPoolExecutor can pickle it."""
    (inputs, output_root, engine, cfg, debug_dir,
     with_checksums, write_mets_file, write_plaintext) = args
    return process_ref_id(
        inputs, output_root,
        engine=engine,
        preprocess_config=cfg,
        debug_preprocessed_dir=debug_dir,
        with_checksums=with_checksums,
        write_mets_file=write_mets_file,
        write_plaintext=write_plaintext,
    )


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
    ocr_lang: str = LangOpt,
    psm: int = PsmOpt,
    oem: int = OemOpt,
    min_dpi: int = MinDpiOpt,
    no_deskew: bool = NoDeskewOpt,
    no_denoise: bool = NoDenoiseOpt,
    no_clahe: bool = NoClaheOpt,
    no_binarize: bool = NoBinarizeOpt,
    no_border_crop: bool = NoBorderCropOpt,
    no_checksums: bool = NoChecksumsOpt,
    no_mets: bool = NoMetsOpt,
    no_plaintext: bool = NoPlaintextOpt,
    debug_preprocessed: Optional[Path] = DebugDirOpt,
    summary_json: Optional[Path] = SummaryOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Process every Ref ID in a collection directory."""
    _setup_logging(verbose)

    items = discover_collection(collection_dir)
    if not items:
        console.print(f"[yellow]No Ref IDs with TIFs found in {collection_dir}[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"Discovered [bold]{len(items)}[/bold] Ref ID(s) "
                  f"({sum(i.page_count for i in items)} pages total).")

    options = OCROptions(language=ocr_lang, psm=psm, oem=oem)
    engine_obj = _build_engine(
        engine,
        tesseract_path=tesseract_path,
        ocr_options=options,
        glmocr_endpoint=glmocr_endpoint,
        glmocr_model=glmocr_model,
        glmocr_timeout=glmocr_timeout,
    )

    cfg = _build_preprocess_config(min_dpi, no_deskew, no_denoise, no_clahe, no_binarize, no_border_crop)

    results: list[RefIdResult] = []
    if workers <= 1:
        # Page-level progress across all Ref IDs. Workers>1 path uses
        # subprocesses, so a shared in-memory counter wouldn't work there —
        # the existing per-Ref-ID "[refid] done: ..." lines cover that case.
        total_pages = sum(i.page_count for i in items)
        progress = PageProgressLogger(total_pages)
        for inputs in items:
            results.append(process_ref_id(
                inputs, output_root,
                engine=engine_obj,
                preprocess_config=cfg,
                debug_preprocessed_dir=debug_preprocessed,
                with_checksums=not no_checksums,
                write_mets_file=not no_mets,
                write_plaintext=not no_plaintext,
                on_page_done=progress.tick,
            ))
    else:
        payloads = [
            (inputs, output_root, engine_obj, cfg,
             debug_preprocessed, not no_checksums, not no_mets, not no_plaintext)
            for inputs in items
        ]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_process_one_for_pool, p): p[0].ref_id for p in payloads}
            for fut in as_completed(futures):
                results.append(fut.result())
        results.sort(key=lambda r: r.ref_id)

    _print_summary(results)
    summary = _summary_dict(results)
    if summary_json:
        summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        console.print(f"[dim]summary:[/dim] {summary_json}")

    n_failed = sum(1 for r in results if not r.ok)
    raise typer.Exit(code=0 if n_failed == 0 else 1)


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
        key=_natural_key,
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
            _convert_to_institution_format(
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
) -> None:
    """Verify the runtime environment (Tesseract, Python deps)."""
    _setup_logging(False)
    console.print("[bold]alto_ocr — environment check[/bold]")
    try:
        binary = find_tesseract(tesseract_path)
        console.print(f"[green]OK[/green] tesseract: {binary} ({check_tesseract_version(binary)})")
    except Exception as e:
        console.print(f"[red]FAIL[/red] tesseract: {e}")
        raise typer.Exit(code=1)

    deps = ["cv2", "PIL", "numpy", "lxml", "typer", "rich"]
    failed = False
    for dep in deps:
        try:
            __import__(dep)
            console.print(f"[green]OK[/green] {dep}")
        except ImportError as e:
            console.print(f"[red]FAIL[/red] {dep}: {e}")
            failed = True
    raise typer.Exit(code=1 if failed else 0)


if __name__ == "__main__":
    app()
