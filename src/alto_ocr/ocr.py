"""OCR engines.

The pipeline talks to OCR engines through the ``OCREngine`` Protocol —
a single ``ocr(image, output_base, *, dpi)`` method that produces an ALTO
``.xml`` and a plain ``.txt`` alongside ``output_base`` and returns their
paths. ``TesseractEngine`` is the only engine (it emits the word
confidences the remediation workflow triages on). The GLMOCR engine was
removed in v0.11.0.

Tesseract notes:

* One invocation per page with three output configs — ``alto tsv txt`` —
  so a single recognition pass produces the ALTO (``.xml``), a word-level
  TSV with confidences (``.tsv``), and the transcript (``.txt``).
  Tesseract appends each extension itself, so we always pass an
  *extension-less* base. The TSV's per-word confidences are merged into
  the ALTO as ``WC`` attributes (plus a mean-derived ``PC`` on ``Page``)
  by ``_merge_tsv_confidence`` and the TSV is then deleted — it is an
  intermediate, not part of the output contract.
* The binary is located via ``shutil.which`` and the ``TESSERACT_PATH``
  env var, with an explicit override accepted by ``find_tesseract``. On
  Windows a non-PATH install (e.g.
  ``C:\\Program Files\\Tesseract-OCR\\tesseract.exe``) is reached via the
  override.
* ``subprocess.run`` is called with a list of args (never ``shell=True``)
  so paths with spaces — common in archival filenames — pass cleanly.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from lxml import etree

from dataclasses import replace as dc_replace

from .alto_format import (
    alto_namespace,
    convert_to_institution_format,
    hardened_parser,
    qname,
    serialize_with_tabs,
)
from .config import OCROptions  # noqa: F401 - re-exported; lives in stdlib-only config
from .preprocess import PreprocessConfig

logger = logging.getLogger(__name__)

# Suppress the console window Windows pops up for each Tesseract invocation.
# Without this, every page steals focus from the user's foreground app, which
# makes the host machine unusable for the duration of a batch. No-op on POSIX
# (the constant only exists on Windows; ``getattr`` falls back to 0 there).
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Output ALTO v2 to match the institution's reference format. Tesseract
# emits v3; alto_format.convert_to_institution_format swaps the namespace +
# schema location and normalizes everything else (units, attribute order,
# indentation, etc.) — the whole serialization layer lives in alto_format.


class TesseractNotFoundError(RuntimeError):
    """Raised when no Tesseract binary can be located."""


def _windows_install_candidates() -> list[Path]:
    """Standard Windows Tesseract install locations to probe as a last resort.

    UB-Mannheim's installer offers two layouts: system-wide under
    ``Program Files`` (admin install) and per-user under
    ``%LOCALAPPDATA%\\Programs`` (non-admin install). Neither is on PATH
    unless the user opts in during install, so we probe both so the GUI
    "just works" for the common case.
    """
    roots: list[str] = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        v = os.environ.get(env_var)
        if v:
            roots.append(v)
    suffixes = (
        Path("Tesseract-OCR") / "tesseract.exe",
        Path("Programs") / "Tesseract-OCR" / "tesseract.exe",
    )
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        for suf in suffixes:
            p = Path(r) / suf
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def find_tesseract(override: str | Path | None = None) -> Path:
    """Locate the Tesseract binary.

    Resolution order:
      1. ``override`` argument (if provided).
      2. ``TESSERACT_PATH`` environment variable.
      3. ``shutil.which('tesseract')`` — finds it on PATH on either OS.
      4. Standard Windows install locations (admin + per-user UB-Mannheim).

    Raises ``TesseractNotFoundError`` with an actionable message if all
    four fail.
    """
    candidates: list[str | Path | None] = [
        override,
        os.environ.get("TESSERACT_PATH"),
        shutil.which("tesseract"),
    ]
    candidates.extend(_windows_install_candidates())
    for c in candidates:
        if not c:
            continue
        path = Path(c)
        if path.is_file():
            return path
    raise TesseractNotFoundError(
        "Tesseract not found. Install it (Linux: 'apt-get install tesseract-ocr'; "
        "Windows: https://github.com/UB-Mannheim/tesseract/wiki) and either add it "
        "to PATH, set TESSERACT_PATH, pass --tesseract-path on the CLI, or set "
        "the Tesseract program under Tools > Advanced settings in the GUI."
    )


def check_tesseract_version(binary: Path) -> str:
    """Return the version string. Used at startup for a smoke check."""
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW_FLAGS,
    )
    # Tesseract prints version to stderr on some builds, stdout on others.
    output = (result.stdout + result.stderr).strip().splitlines()
    return output[0] if output else "unknown"


def _tesseract_env() -> dict[str, str]:
    """Environment for a Tesseract child: OpenMP held to one thread.

    Tesseract builds with OpenMP (Ubuntu's apt package is one) run ~4 threads
    per page for almost no gain, and several such processes at once — ``batch
    --workers N``, or Goobi running two OCR steps together — oversubscribe
    the cores and spin-wait against each other. Measured 2026-11-11 on a
    6-core host, Tesseract 5.3.4, one 2.6 s page: 2 concurrent pages took
    7.4 s, 3 took 39 s, 4 took 117 s, and 6 had not finished after 327 s —
    long enough for real pages to hit the per-page timeout and become failure
    stubs. With ``OMP_THREAD_LIMIT=1`` the same 6 pages took 4-5 s, and a
    lone page costs 7% more wall time for 2.6x less CPU. Parallelism comes
    from ``--workers``, not from inside one page.

    An ``OMP_THREAD_LIMIT`` already set in the environment wins, so a host
    that measures differently can override it without a code change.
    """
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return env


def _run_tesseract(
    binary: Path,
    image_path: Path,
    output_base: Path,
    options: OCROptions,
    dpi: int,
    configs: tuple[str, ...],
) -> None:
    """Single Tesseract invocation. ``configs`` are output configs appended to
    the command line (e.g. ``("alto", "tsv", "txt")``); each one enables a
    renderer, and the single recognition pass feeds all of them.
    """
    cmd: list[str] = [
        str(binary),
        str(image_path),
        str(output_base),
        "-l", options.language,
        "--psm", str(options.psm),
        "--oem", str(options.oem),
        "--dpi", str(dpi),
        *configs,
    ]

    logger.debug("tesseract: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW_FLAGS,
            timeout=options.timeout_s,
            env=_tesseract_env(),
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child; the RuntimeError
        # routes the page to the pipeline's failure stub.
        raise RuntimeError(
            f"Tesseract timed out after {options.timeout_s}s on {image_path.name}"
        ) from None
    if proc.returncode != 0:
        # Tesseract logs warnings to stderr even on success; only escalate on failure.
        raise RuntimeError(
            f"Tesseract failed (rc={proc.returncode}) on {image_path.name}\n"
            f"stdout: {proc.stdout.strip()}\n"
            f"stderr: {proc.stderr.strip()}"
        )


def _merge_tsv_confidence(alto_path: Path, tsv_path: Path) -> None:
    """Merge word confidences from a Tesseract TSV into the sibling ALTO.

    Tesseract's ``alto`` renderer emits no confidence values; its ``tsv``
    renderer emits one 0–100 confidence per word. Both renderers walk the
    same recognition result, so their word bounding boxes are identical.
    Each level-5 (word) TSV row is keyed by its pixel bbox and matched to
    the ALTO ``String`` with the same HPOS/VPOS/WIDTH/HEIGHT; each match
    gains ``WC`` (ALTO word confidence, 0..1). ``Page`` gains ``PC`` — the
    mean word confidence — which is what remediation triage sorts on to
    find pages (and handwriting-heavy objects) that need human attention.

    Must run *before* ``convert_to_institution_format``: matching relies
    on the ALTO coordinates still being in pixels. The added attributes
    survive conversion (they're not pixel attrs, and the attribute-order
    tables already reserve WC/PC slots from the reference samples).

    Confidence is an enhancement, not part of the output contract —
    callers should treat a failure here as non-fatal.
    """
    confs: dict[tuple[str | None, ...], list[float]] = {}
    all_confs: list[float] = []
    for row in tsv_path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":  # level 5 = word rows
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        # Key on the raw bbox strings (left, top, width, height): both
        # renderers print the same integers, so no numeric round-trip.
        confs.setdefault(tuple(parts[6:10]), []).append(conf)
        all_confs.append(conf)

    parser = hardened_parser(remove_blank_text=True)
    tree = etree.parse(str(alto_path), parser)
    root = tree.getroot()
    ns = alto_namespace(root)

    unmatched = 0
    for el in root.iter(qname(ns, "String")):
        key = (el.get("HPOS"), el.get("VPOS"), el.get("WIDTH"), el.get("HEIGHT"))
        bucket = confs.get(key)
        if bucket:
            # Identical bboxes (rare) pair up in document order.
            el.set("WC", f"{min(bucket.pop(0), 100.0) / 100:.2f}")
        else:
            unmatched += 1
    if unmatched:
        logger.debug(
            "%s: %d ALTO String(s) had no TSV confidence match", alto_path.name, unmatched
        )

    if all_confs:
        page = root.find(f".//{qname(ns, 'Page')}")
        if page is not None:
            page.set("PC", f"{sum(all_confs) / len(all_confs) / 100:.2f}")

    serialize_with_tabs(tree, alto_path)


# (The attribute-order tables, tab serializer, and institution-format
# converter that used to live here moved verbatim to alto_format.py.)


class OCREngine(Protocol):
    """OCR engines produce ALTO XML + plain text for one preprocessed page.

    Implementations write ``{output_base}.xml`` (ALTO) and ``{output_base}.txt``
    (plain text) and return the two paths. Failures must propagate as
    exceptions; the pipeline catches them per-page and writes failure stubs
    so the ALTO/Text page count stays aligned with the source TIF count.

    The engine is constructed once with whatever it needs (binary path,
    HTTP endpoint, options); each ``ocr`` call processes one page.

    Engines that need a different preprocessing recipe than the user's
    default can override ``adjust_preprocess`` to return a modified config
    (Tesseract uses it to force the frame-changing stages off).
    """

    name: str  # short identifier used in logs / METS metadata

    def ocr(
        self,
        image: np.ndarray,
        output_base: Path,
        *,
        dpi: int,
        page_index: int,
        source_filename: str | None = None,
    ) -> tuple[Path, Path]: ...

    def adjust_preprocess(self, config: PreprocessConfig) -> PreprocessConfig: ...


@dataclass
class TesseractEngine:
    """Default OCR engine: one Tesseract pass (``alto tsv txt`` configs),
    then TSV word confidences are merged into the ALTO as ``WC``/``PC``."""

    binary: Path
    options: OCROptions = None  # type: ignore[assignment]  # set in __post_init__
    name: str = "tesseract"

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = OCROptions()

    def adjust_preprocess(self, config: PreprocessConfig) -> PreprocessConfig:
        # Drop the two FRAME-CHANGING stages. ALTO coordinates are emitted in
        # the preprocessed image's frame, and every consumer — the public
        # archive's hit boxes, the Review tab, the QA overlays — maps them
        # onto the ORIGINAL image via the image-px / Page-dims ratio; only
        # the uniform DPI upscale cancels in that mapping. Deskew (rotation)
        # and border-crop (translation + resize) do not cancel: measured on
        # ZeffPapers 2026-07-30, deskew fired on 36% of pages (median 1.1°,
        # max 4.4°) and put word boxes a full line off their words near the
        # page edges. The A/B on the worst-skew pages showed disabling it
        # also RAISES mean WC (+0.03 overall, +0.11 on the 4.4° page):
        # Tesseract's own baseline fitting reads these angles fine, while
        # the deskew rotation's resampling blurred glyphs before binarize.
        # Border-crop fired on 0/119 probed pages (adaptive binarize hollows
        # out solid borders before the crop ever sees them) — no accuracy
        # cost either. Denoise/CLAHE/binarize stay per user config: they
        # change pixel VALUES, not positions. If a collection ever appears whose
        # skew genuinely breaks recognition, the fix is to keep deskew for
        # recognition and inverse-transform coordinates at emission (the
        # handoff's "v2") — not to re-enable deskew here.
        return dc_replace(config, do_deskew=False, do_border_crop=False)

    def ocr(
        self,
        image: np.ndarray,
        output_base: Path,
        *,
        dpi: int,
        page_index: int,
        source_filename: str | None = None,
    ) -> tuple[Path, Path]:
        """OCR one preprocessed image; produce ALTO + plain text.

        ``image`` is a numpy uint8 array (the preprocessed page from
        ``preprocess()``). It is written to a temporary PNG before
        invoking Tesseract, then deleted.
        """
        output_base = Path(output_base)
        output_base.parent.mkdir(parents=True, exist_ok=True)

        work_dir = output_base.parent
        tmp = work_dir / f".{output_base.name}.tess.png"
        if not cv2.imwrite(str(tmp), image):
            raise IOError(f"Failed to write temporary preprocessed image: {tmp}")

        try:
            _run_tesseract(
                self.binary, tmp, output_base, self.options, dpi,
                configs=("alto", "tsv", "txt"),
            )
        finally:
            try:
                tmp.unlink()
            except OSError:
                logger.debug("Could not remove temp file: %s", tmp)

        alto = output_base.with_suffix(".xml")
        txt = output_base.with_suffix(".txt")
        tsv = output_base.with_suffix(".tsv")
        if not alto.is_file() or not txt.is_file():
            raise RuntimeError(
                f"Tesseract reported success but expected outputs are missing: "
                f"alto={alto.is_file()}, txt={txt.is_file()}"
            )
        # Confidence merge is best-effort: a failure here must not turn a
        # successfully OCR'd page into a failure stub.
        try:
            if tsv.is_file():
                _merge_tsv_confidence(alto, tsv)
            else:
                logger.warning(
                    "%s: Tesseract wrote no .tsv — ALTO will lack WC/PC "
                    "confidence values (Tesseract 5+ expected)", alto.name,
                )
        except Exception:
            logger.warning(
                "Confidence merge failed for %s; ALTO left without WC/PC values",
                alto.name, exc_info=True,
            )
        finally:
            try:
                tsv.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove TSV file: %s", tsv)

        convert_to_institution_format(
            alto, page_index, dpi=dpi, source_filename=source_filename,
        )
        return alto, txt


# --- shared engine construction -----------------------------------------------

@dataclass(frozen=True)
class BuiltEngine:
    """A constructed, startup-checked engine plus the facts callers log.

    Presentation stays caller-side (the CLI prints a rich console line, the
    GUI logs through its queue handler); only construction and the startup
    checks are shared.
    """

    engine: TesseractEngine
    kind: str  # "tesseract"
    tesseract_binary: Path | None = None
    tesseract_version: str | None = None


def build_engine(
    engine_name: str = "tesseract",
    *,
    tesseract_path: str | Path | None = None,
    ocr_options: OCROptions | None = None,
) -> BuiltEngine:
    """Resolve an engine name into a ready, startup-checked engine.

    The single construction path for the CLI and the GUI. Performs the
    Tesseract binary lookup + version probe so failures surface before the
    first page, and raises ``TesseractNotFoundError`` / ``ValueError``
    (unknown name — including the removed ``glmocr``) for callers to wrap
    in their own error-reporting idiom.
    """
    name = engine_name.lower().strip()
    if name == "tesseract":
        binary = find_tesseract(tesseract_path)
        return BuiltEngine(
            engine=TesseractEngine(binary=binary,
                                   options=ocr_options or OCROptions()),
            kind="tesseract",
            tesseract_binary=binary,
            tesseract_version=check_tesseract_version(binary),
        )
    if name == "glmocr":
        raise ValueError("The glmocr engine was removed in v0.11.0; "
                         "Tesseract is the only engine.")
    raise ValueError(f"Unknown engine: {engine_name!r} (valid: tesseract)")
