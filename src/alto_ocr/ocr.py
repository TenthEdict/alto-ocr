"""OCR engines.

The pipeline talks to OCR engines through the ``OCREngine`` Protocol —
a single ``ocr(image, output_base, *, dpi)`` method that produces an ALTO
``.xml`` and a plain ``.txt`` alongside ``output_base`` and returns their
paths. ``TesseractEngine`` is the default (it emits the word confidences
the remediation workflow triages on); ``GLMOCREngine`` sits behind
``--engine glmocr`` for handwriting-heavy material.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape as xml_escape

import cv2
import numpy as np
from lxml import etree

from dataclasses import replace as dc_replace

from .glmocr import GLMOCRConfig, transcribe
from .line_detect import DetectParams, LineBox, TextBlock, _clean_mask, detect_blocks
from .markup import normalize_markup
from .preprocess import PreprocessConfig

logger = logging.getLogger(__name__)

# Suppress the console window Windows pops up for each Tesseract invocation.
# Without this, every page steals focus from the user's foreground app, which
# makes the host machine unusable for the duration of a batch. No-op on POSIX
# (the constant only exists on Windows; ``getattr`` falls back to 0 there).
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Output ALTO v2 to match the institution's reference format. Tesseract
# emits v3; the converter swaps the namespace + schema location and
# normalizes everything else (units, attribute order, indentation, etc.).
ALTO_NS_V2 = "http://www.loc.gov/standards/alto/ns-v2#"
ALTO_NS_V3 = "http://www.loc.gov/standards/alto/ns-v3#"
_ALTO_V2_SCHEMA_URL = "http://www.loc.gov/standards/alto/v2/alto-2-0.xsd"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_XLINK_NS = "http://www.w3.org/1999/xlink"

# Backwards-compatible alias for code (and tests) that still imports the
# old single-version constant.
ALTO_NS = ALTO_NS_V2


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
        "to PATH, set TESSERACT_PATH, or pass --tesseract-path on the CLI."
    )


def check_tesseract_version(binary: Path) -> str:
    """Return the version string. Used at startup for a smoke check."""
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True, text=True, check=True,
        creationflags=_NO_WINDOW_FLAGS,
    )
    # Tesseract prints version to stderr on some builds, stdout on others.
    output = (result.stdout + result.stderr).strip().splitlines()
    return output[0] if output else "unknown"


@dataclass(frozen=True)
class OCROptions:
    language: str = "eng"
    psm: int = 3  # fully automatic page segmentation
    oem: int = 3  # default LSTM engine


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
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=_NO_WINDOW_FLAGS,
    )
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

    Must run *before* ``_convert_to_institution_format``: matching relies
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

    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(str(alto_path), parser)
    root = tree.getroot()
    ns = root.nsmap.get(None)

    unmatched = 0
    for el in root.iter(_qname(ns, "String")):
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
        page = root.find(f".//{_qname(ns, 'Page')}")
        if page is not None:
            page.set("PC", f"{sum(all_confs) / len(all_confs) / 100:.2f}")

    _serialize_with_tabs(tree, alto_path)


# Both Tesseract and the GLMOCR builder emit ALTO that doesn't match the
# institution's reference format (verified to work with their downstream
# system). The post-processor below normalizes the file end-to-end:
#
#   * Sequential ``P{N}`` page IDs and element-type-prefixed child IDs
#     (``P{N}_TB00001`` / ``P{N}_TL00001`` / ``P{N}_ST00001`` etc.).
#   * Explicit Top/Left/Right/BottomMargin elements derived from the
#     bounding box of the printed area; PrintSpace shrunk to that bbox.
#   * MeasurementUnit ``mm10`` (1/10 mm) with all HPOS/VPOS/WIDTH/HEIGHT
#     values converted from the OCR's pixel space using the page's DPI.
#   * ALTO v2 namespace + schema location.
#   * ``<sourceImageInformation>`` referencing the source TIF basename.
#   * Per-element-type attribute order matching the reference samples.
#   * Tab indentation, double-quoted XML declaration.
#
# The converter is idempotent: it detects whether coordinates are already
# in mm10 (via MeasurementUnit) and skips re-conversion on subsequent
# passes; existing margins are stripped before recomputing; the namespace
# swap is a no-op when the input is already v2.
_TYPE_CODES = {
    "ComposedBlock": "CB",
    "TextBlock": "TB",
    "TextLine": "TL",
    "String": "ST",
    "SP": "SP",
}

_PIXEL_ATTRS = ("HPOS", "VPOS", "WIDTH", "HEIGHT")

# Per-element attribute order matching the reference samples. Unknown
# attributes are appended after the listed ones in their original order.
_ATTR_ORDER: dict[str, tuple[str, ...]] = {
    "Page":          ("ID", "PHYSICAL_IMG_NR", "HEIGHT", "WIDTH", "PC"),
    "TopMargin":     ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "LeftMargin":    ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "RightMargin":   ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "BottomMargin":  ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "PrintSpace":    ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "ComposedBlock": ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT", "STYLEREFS", "TYPE"),
    "TextBlock":     ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT", "language", "STYLEREFS"),
    "TextLine":      ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
    "String":        ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT", "CONTENT", "WC", "CC",
                       "STYLEREFS", "SUBS_TYPE", "SUBS_CONTENT"),
    "SP":            ("ID", "HPOS", "VPOS", "WIDTH"),
    "GraphicalElement": ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT"),
}


def _qname(ns: str | None, tag: str) -> str:
    return f"{{{ns}}}{tag}" if ns else tag


def _px_to_mm10(px: int, dpi: int) -> int:
    """Convert a pixel value to ALTO ``mm10`` units (1/10 mm).

    1 inch = 25.4 mm = 254 mm10, so ``mm10 = px * 254 / dpi``. Result is
    rounded to the nearest integer (ALTO coordinate attributes are integer
    strings).
    """
    if dpi <= 0:
        return px  # safety net; caller should validate dpi
    return int(round(px * 254 / dpi))


def _reorder_attrs(elem: etree._Element, order: tuple[str, ...]) -> None:
    current = dict(elem.attrib)
    elem.attrib.clear()
    for key in order:
        if key in current:
            elem.set(key, current.pop(key))
    for key, value in current.items():
        elem.set(key, value)


def _change_default_namespace(root: etree._Element, new_ns: str) -> etree._Element:
    """Rebuild the tree under a new default namespace, preserving xsi/xlink."""
    old_ns = root.nsmap.get(None)
    nsmap = {None: new_ns, "xsi": _XSI_NS, "xlink": _XLINK_NS}

    def remap(tag: str) -> str:
        qn = etree.QName(tag)
        if qn.namespace == old_ns:
            return f"{{{new_ns}}}{qn.localname}"
        return tag

    new_root = etree.Element(remap(root.tag), nsmap=nsmap)
    new_root.text = root.text
    for k, v in root.attrib.items():
        new_root.set(remap(k), v)

    def copy_into(src: etree._Element, dst: etree._Element) -> None:
        for child in src:
            new_child = etree.SubElement(dst, remap(child.tag))
            for k, v in child.attrib.items():
                new_child.set(remap(k), v)
            new_child.text = child.text
            new_child.tail = child.tail
            copy_into(child, new_child)

    copy_into(root, new_root)
    return new_root


def _serialize_with_tabs(tree: etree._ElementTree, alto_path: Path) -> None:
    """Write tree with tab indentation and a double-quoted XML declaration.

    lxml's ``pretty_print`` uses 2-space indents and its XML declaration
    uses single quotes; the institution's reference samples use tabs and
    double quotes. We post-process the serialized string to match.
    """
    body = etree.tostring(
        tree, pretty_print=True, encoding="utf-8", xml_declaration=False,
    ).decode("utf-8")
    body = re.sub(
        r"^( {2})+",
        lambda m: "\t" * (len(m.group(0)) // 2),
        body,
        flags=re.MULTILINE,
    )
    alto_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + body, encoding="utf-8",
    )


def _convert_to_institution_format(
    alto_path: Path,
    page_index: int,
    *,
    dpi: int,
    source_filename: str | None = None,
) -> None:
    """Rewrite the ALTO file at ``alto_path`` to match the institution's
    reference format. See the module-level comment block above for the full
    list of transformations.

    ``dpi`` is the effective DPI at which the OCR coordinates were produced
    (typically ``preprocess.PreprocessResult.effective_dpi``). It is used to
    convert pixel coordinates to mm10. If the file's ``MeasurementUnit`` is
    already ``mm10``, the conversion step is skipped (idempotency).

    ``source_filename`` is the basename of the source TIF (e.g.
    ``image001.tif``); when provided, ``<sourceImageInformation><fileName>``
    is set to ``./<source_filename>`` to match the reference convention.
    Pass ``None`` to leave any existing fileName text alone.
    """
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(str(alto_path), parser)
    root = tree.getroot()
    src_ns = root.nsmap.get(None)

    desc = root.find(f"./{_qname(src_ns, 'Description')}")
    mu_el = desc.find(f"./{_qname(src_ns, 'MeasurementUnit')}") if desc is not None else None
    needs_unit_conversion = mu_el is None or (mu_el.text or "").strip() != "mm10"

    page = root.find(f".//{_qname(src_ns, 'Page')}")
    if page is not None:
        page_id = f"P{page_index}"
        page.set("ID", page_id)
        page.set("PHYSICAL_IMG_NR", str(page_index))

        try:
            page_w = int(page.get("WIDTH", "0"))
            page_h = int(page.get("HEIGHT", "0"))
        except (TypeError, ValueError):
            page_w = page_h = 0

        for margin_tag in ("TopMargin", "LeftMargin", "RightMargin", "BottomMargin"):
            for el in page.findall(_qname(src_ns, margin_tag)):
                page.remove(el)

        print_space = page.find(_qname(src_ns, "PrintSpace"))
        if print_space is not None:
            print_space.set("ID", f"{page_id}_PS00001")
            children = list(print_space)
            if children and page_w > 0 and page_h > 0:
                try:
                    xs  = [int(c.get("HPOS", "0")) for c in children]
                    ys  = [int(c.get("VPOS", "0")) for c in children]
                    xs2 = [int(c.get("HPOS", "0")) + int(c.get("WIDTH", "0")) for c in children]
                    ys2 = [int(c.get("VPOS", "0")) + int(c.get("HEIGHT", "0")) for c in children]
                    min_x, min_y = min(xs), min(ys)
                    max_x, max_y = max(xs2), max(ys2)
                except (TypeError, ValueError):
                    min_x = None  # type: ignore[assignment]

                if isinstance(min_x, int):
                    # Clamp to page so leaked-past-edge blocks (Tesseract
                    # does this on noisy scans) don't yield negative-area
                    # margins.
                    min_x = max(0, min(min_x, page_w))
                    min_y = max(0, min(min_y, page_h))
                    max_x = max(min_x, min(max_x, page_w))
                    max_y = max(min_y, min(max_y, page_h))

                    print_space.set("HPOS", str(min_x))
                    print_space.set("VPOS", str(min_y))
                    print_space.set("WIDTH", str(max_x - min_x))
                    print_space.set("HEIGHT", str(max_y - min_y))

                    ps_idx = list(page).index(print_space)
                    margins = [
                        ("TopMargin",    f"{page_id}_TM00001", 0,     0,     page_w,         min_y),
                        ("LeftMargin",   f"{page_id}_LM00001", 0,     min_y, min_x,          max_y - min_y),
                        ("RightMargin",  f"{page_id}_RM00001", max_x, min_y, page_w - max_x, max_y - min_y),
                        ("BottomMargin", f"{page_id}_BM00001", 0,     max_y, page_w,         page_h - max_y),
                    ]
                    for offset, (tag, mid, h, v, w, ht) in enumerate(margins):
                        el = etree.Element(_qname(src_ns, tag), attrib={
                            "ID":   mid,
                            "HPOS": str(h),
                            "VPOS": str(v),
                            "WIDTH":  str(w),
                            "HEIGHT": str(ht),
                        })
                        page.insert(ps_idx + offset, el)

        counters: dict[str, int] = {tag: 0 for tag in _TYPE_CODES}
        for el in page.iter():
            local = etree.QName(el).localname
            code = _TYPE_CODES.get(local)
            if code is None:
                continue
            counters[local] += 1
            el.set("ID", f"{page_id}_{code}{counters[local]:05d}")

    # Ensure MeasurementUnit = mm10 + sourceImageInformation/fileName.
    if desc is not None:
        if mu_el is None:
            mu_el = etree.Element(_qname(src_ns, "MeasurementUnit"))
            desc.insert(0, mu_el)
        mu_el.text = "mm10"

        if source_filename is not None:
            sii = desc.find(f"./{_qname(src_ns, 'sourceImageInformation')}")
            if sii is None:
                sii = etree.Element(_qname(src_ns, "sourceImageInformation"))
                desc.insert(list(desc).index(mu_el) + 1, sii)
            fn = sii.find(f"./{_qname(src_ns, 'fileName')}")
            if fn is None:
                fn = etree.SubElement(sii, _qname(src_ns, "fileName"))
            fn.text = f"./{source_filename}"

    if needs_unit_conversion:
        for el in root.iter():
            for attr in _PIXEL_ATTRS:
                val = el.get(attr)
                if val is None:
                    continue
                try:
                    px = int(val)
                except (TypeError, ValueError):
                    continue
                el.set(attr, str(_px_to_mm10(px, dpi)))

    for el in root.iter():
        local = etree.QName(el).localname
        order = _ATTR_ORDER.get(local)
        if order is not None:
            _reorder_attrs(el, order)

    if src_ns != ALTO_NS_V2:
        new_root = _change_default_namespace(root, ALTO_NS_V2)
        tree = etree.ElementTree(new_root)
        root = new_root

    root.set(
        f"{{{_XSI_NS}}}schemaLocation",
        f"{ALTO_NS_V2} {_ALTO_V2_SCHEMA_URL}",
    )

    _serialize_with_tabs(tree, alto_path)


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
    (e.g. GLMOCR's vision transformer trips a GGML assertion on some
    binarized inputs and needs continuous grayscale instead).
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
        # Tesseract benefits from the full default preprocessing chain
        # (deskew + denoise + CLAHE + binarize + border crop).
        return config

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

        _convert_to_institution_format(
            alto, page_index, dpi=dpi, source_filename=source_filename,
        )
        return alto, txt


# --- GLMOCR engine: per-column glm-ocr transcription + per-word ALTO geometry -

# A word is any run of non-space characters; the gaps between them become
# <SP> elements. glm-ocr gives no per-word geometry, so word boxes are derived
# by splitting each line box in proportion to character offsets — approximate
# but monotonic, which is enough to highlight the right word in roughly the
# right place. (Word-level boxes are required: ALTO hit-highlighting matches a
# <String>'s CONTENT and draws *its* box, so a whole-line String can only
# highlight the whole line.)
_WORD_TOKEN_RE = re.compile(r"\S+|\s+")


# Two regimes for spreading a block's transcript over its line boxes. Tables
# and forms survive ``normalize_markup`` as one text row per visual row (HTML
# ``<tr>`` / Markdown pipe rows become newline-separated lines), so the block's
# newline count tracks its real row count. Flowing prose is the opposite:
# glm-ocr collapses a column's many visual lines into a few paragraph rows. A
# block is treated as row-structured — and mapped onto line boxes positionally
# rather than as a width-weighted word stream — only when it has several rows,
# roughly as many rows as the block has detected line boxes, AND those rows are
# short. The ratio is deliberately loose (a clean table sits near 1.0; ordinary
# prose near 0.1) so detection over/under-segmenting a few rows still trips it.
# The shortness gate is what separates genuine flowing prose from tabular data:
# table/form/list rows are short (cells, labels, list items) while prose rows
# are full sentences — without it, a block whose paragraph count happens to
# approach the line-box count (a short multi-paragraph column) would be wrongly
# pinned 1:1 and its words misplaced.
_ROW_MODE_MIN_ROWS = 3
_ROW_MODE_RATIO = 0.5
_ROW_MODE_MAX_MEDIAN_WORDS = 8


def _looks_row_structured(rows: list[str], lines: list[LineBox]) -> bool:
    """True when ``rows`` should map onto ``lines`` positionally (tables/forms).

    Row-structured text (a table, form, or list, where ``normalize_markup`` kept
    one line per visual row) has roughly as many rows as the block has detected
    line boxes AND those rows are short (cells/labels, not sentences). Flowing
    prose has far fewer, much longer rows (paragraphs) than line boxes, so it
    stays on the width-proportional word path.
    """
    n_rows, n_lines = len(rows), len(lines)
    if n_rows < _ROW_MODE_MIN_ROWS or n_lines < _ROW_MODE_MIN_ROWS:
        return False
    if n_rows < _ROW_MODE_RATIO * n_lines:
        return False
    # Short rows => tabular/list; long rows => flowing prose. Use the median so
    # one long row (a wrapped table caption) doesn't flip a real table to prose.
    words_per_row = sorted(len(r.split()) for r in rows)
    median_words = words_per_row[len(words_per_row) // 2]
    return median_words <= _ROW_MODE_MAX_MEDIAN_WORDS


def _assign_rows_to_lines(
    rows: list[str], lines: list[LineBox],
) -> list[tuple[LineBox, str]]:
    """Map text rows onto line boxes positionally, preserving reading order.

    When the counts match (the clean table case) this is a 1:1 row→box map, so
    each row's cells land in their own line box and a short row's values never
    bleed into the next row's box — the failure mode width-proportional
    distribution has on tabular data. When the counts differ (detection split or
    merged a band, or a row wrapped) rows are bucketed across boxes
    monotonically: extra boxes stay empty, extra rows pack into the nearest box,
    so text keeps its vertical order and rough position without dropping or
    duplicating any row. Within a box, ``_split_line_words`` then spreads the
    row's cells left-to-right by character offset.
    """
    n, m = len(rows), len(lines)
    if n == m:
        return list(zip(lines, rows))
    # Counts differ: detection split/merged a band or a row wrapped. Placement
    # degrades to approximate here (n>m packs several rows into one box; n<m
    # leaves gaps), so log it — a collection that trips this a lot wants its
    # DetectParams tuned rather than silently producing mis-aligned hit-boxes.
    logger.debug(
        "glmocr: row/line-box mismatch (%d rows, %d boxes) — %s; per-row "
        "placement approximate for this block",
        n, m, "packing rows" if n > m else "spreading rows (some boxes empty)",
    )
    buckets = [""] * m
    for j, row in enumerate(rows):
        k = min(m - 1, (j * m) // n)
        buckets[k] = f"{buckets[k]} {row}".strip() if buckets[k] else row
    return list(zip(lines, buckets))


def _assign_words_to_lines(
    words: list[str], lines: list[LineBox],
) -> list[tuple[LineBox, str]]:
    """Spread a word stream across line boxes, each box's share proportional to
    its width.

    For flowing prose, where glm-ocr's flattened paragraphs give no per-line
    structure to align to: a wider line holds more words. Because the block was
    transcribed on its own, top to bottom, the word order matches the boxes'
    top-to-bottom order, so each word lands on roughly its real line.
    """
    if not words:
        return [(lb, "") for lb in lines]
    widths = [max(1, lb.w) for lb in lines]
    total = sum(widths)
    n = len(words)
    out: list[tuple[LineBox, str]] = []
    start = acc = 0
    for i, lb in enumerate(lines):
        acc += widths[i]
        end = n if i == len(lines) - 1 else max(start, min(n, round(acc / total * n)))
        out.append((lb, " ".join(words[start:end])))
        start = end
    return out


def _assign_block_text(block_text: str, lines: list[LineBox]) -> list[tuple[LineBox, str]]:
    """Spread a block's transcript across its detected line boxes.

    glm-ocr supplies no per-word geometry, so both paths are positional
    approximations laid over the exact line-box geometry. The placement strategy
    depends on the block's shape:

    * **Row-structured** (tables, forms): ``normalize_markup`` keeps one text row
      per visual row, so rows map onto line boxes positionally — 1:1 when the
      counts match. This keeps each row's cells in their own line band instead of
      letting a short row's values bleed down into the next row's box.
    * **Prose**: glm-ocr flattens a column into a few paragraph rows, far fewer
      than the detected line boxes, so the block's words are distributed across
      the boxes in reading order, each box's share proportional to its width.

    Within whichever line box it lands in, ``_split_line_words`` then places each
    word by character offset. Boxes that receive no words are dropped by the
    ALTO builder.
    """
    if not lines:
        return []
    rows = [r.strip() for r in block_text.splitlines() if r.strip()]
    if not rows:
        return [(lb, "") for lb in lines]
    if _looks_row_structured(rows, lines):
        logger.debug(
            "glmocr: row-structured block (%d rows, %d line boxes) -> positional map",
            len(rows), len(lines),
        )
        return _assign_rows_to_lines(rows, lines)
    # Reuse the already-split rows for the word stream (same words, same order
    # as block_text.split()) so both paths read one consistent view of the text.
    return _assign_words_to_lines([w for r in rows for w in r.split()], lines)


def _split_line_words(box: LineBox, text: str) -> list[tuple[str, int, int]]:
    """Split a line box + its text into ordered ``(token, hpos, width)`` tuples.

    Tokens alternate words and the spaces between them; a space token has an
    empty string (it becomes an ``<SP>``). x/width are proportional to character
    offsets within ``box`` and accumulate left to right, so the final token ends
    near ``box.x2``. Positions are approximate (the model has no word geometry)
    but ordered, so a search highlights the correct word at roughly its spot.
    """
    text = text.strip()
    if not text:
        return []
    char_w = box.w / len(text)
    out: list[tuple[str, int, int]] = []
    offset = 0
    for m in _WORD_TOKEN_RE.finditer(text):
        seg = m.group(0)
        x = int(round(box.x + offset * char_w))
        w = max(1, int(round(len(seg) * char_w)))
        out.append(("" if seg.isspace() else seg, x, w))
        offset += len(seg)
    return out


def _build_alto_words(
    *,
    page_w: int,
    page_h: int,
    page_index: int,
    blocks: list[tuple[TextBlock, list[tuple[LineBox, str]]]],
    engine_name: str,
    engine_version: str,
) -> str:
    """Build an ALTO v2 document: one TextBlock per detected column, a TextLine
    per detected line, and a per-word ``<String>`` (with ``<SP>`` between words)
    so the public archive UI can highlight individual search hits.

    Lines with no assigned text are skipped, and a block with no text-bearing
    lines is skipped; a page with nothing left produces a valid empty Layout
    (matching Tesseract on blank pages). Element IDs here are placeholders —
    ``_convert_to_institution_format`` renumbers every element afterwards.

    ``page_index`` is the 1-based page number within the Ref ID; it seeds the
    Page ID + PHYSICAL_IMG_NR so child IDs stay unique when downstream consumers
    consolidate a Ref ID's ALTO files into one tree for hit-box rendering.
    """
    when = datetime.now(timezone.utc).isoformat()
    page_id = f"page_{page_index}"

    def esc(s: str) -> str:
        return xml_escape(s, {'"': "&quot;", "'": "&apos;"})

    blocks_xml: list[str] = []
    for b_idx, (_blk, line_assign) in enumerate(blocks):
        lines_xml: list[str] = []
        used: list[LineBox] = []
        for lb, text in line_assign:
            tokens = _split_line_words(lb, text)
            if not any(tok for tok, _, _ in tokens):
                continue  # no words on this line — drop it (geometry only, nothing to find)
            l_idx = len(used)
            inner: list[str] = []
            wi = 0
            for tok, x, w in tokens:
                if tok:
                    inner.append(
                        f'          <String ID="{page_id}_b{b_idx}_l{l_idx}_s{wi}" '
                        f'HPOS="{x}" VPOS="{lb.y}" WIDTH="{w}" HEIGHT="{lb.h}" '
                        f'CONTENT="{esc(tok)}"/>'
                    )
                    wi += 1
                else:
                    inner.append(
                        f'          <SP ID="{page_id}_b{b_idx}_l{l_idx}_sp{wi}" '
                        f'HPOS="{x}" VPOS="{lb.y}" WIDTH="{w}"/>'
                    )
            lines_xml.append(
                f'        <TextLine ID="{page_id}_b{b_idx}_l{l_idx}" '
                f'HPOS="{lb.x}" VPOS="{lb.y}" WIDTH="{lb.w}" HEIGHT="{lb.h}">\n'
                + "\n".join(inner)
                + "\n        </TextLine>"
            )
            used.append(lb)
        if not used:
            continue
        tb_x = min(lb.x for lb in used)
        tb_y = min(lb.y for lb in used)
        tb_w = max(lb.x2 for lb in used) - tb_x
        tb_h = max(lb.y2 for lb in used) - tb_y
        blocks_xml.append(
            f'      <TextBlock ID="{page_id}_block_{b_idx}" '
            f'HPOS="{tb_x}" VPOS="{tb_y}" WIDTH="{tb_w}" HEIGHT="{tb_h}">\n'
            + "\n".join(lines_xml)
            + "\n      </TextBlock>"
        )

    text_block_xml = ("\n".join(blocks_xml) + "\n") if blocks_xml else ""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<alto xmlns="{ALTO_NS_V2}" '
        f'xmlns:xsi="{_XSI_NS}" '
        f'xsi:schemaLocation="{ALTO_NS_V2} {_ALTO_V2_SCHEMA_URL}" '
        f'xmlns:xlink="{_XLINK_NS}">\n'
        '  <Description>\n'
        '    <MeasurementUnit>pixel</MeasurementUnit>\n'
        f'    <OCRProcessing ID="OCR_0">\n'
        f'      <ocrProcessingStep>\n'
        f'        <processingDateTime>{when}</processingDateTime>\n'
        f'        <processingSoftware>\n'
        f'          <softwareName>{xml_escape(engine_name)}</softwareName>\n'
        f'          <softwareVersion>{xml_escape(engine_version)}</softwareVersion>\n'
        f'        </processingSoftware>\n'
        f'      </ocrProcessingStep>\n'
        f'    </OCRProcessing>\n'
        '  </Description>\n'
        '  <Layout>\n'
        f'    <Page WIDTH="{page_w}" HEIGHT="{page_h}" '
        f'PHYSICAL_IMG_NR="{page_index}" ID="{page_id}">\n'
        f'      <PrintSpace HPOS="0" VPOS="0" '
        f'WIDTH="{page_w}" HEIGHT="{page_h}">\n'
        + text_block_xml
        + '      </PrintSpace>\n'
        '    </Page>\n'
        '  </Layout>\n'
        '</alto>\n'
    )


# glm-ocr fabricates a plausible-looking fake transcript when handed a crop that
# is mostly blank or noise — it does NOT reliably return "" or the "---" blank
# sentinel, and the fabrication is indistinguishable from real text downstream
# (observed: a blank page yielding an invented paragraph about "sunlight and
# plant growth"). The only robust defense is to gate on image ink BEFORE
# transcribing: a real text line band runs ~10-35% ink while a blank/noise band
# is well under 1% (a >10x gap). Skip any block whose median line-band density
# is below this floor; prefer an empty page over fabricated content.
_MIN_LINE_INK_DENSITY = 0.03


def _block_has_real_text(cleaned: np.ndarray, block: TextBlock) -> bool:
    """True if a block's detected line bands are dense enough to be real text
    (not the near-empty/noise crops that make glm-ocr hallucinate)."""
    dens: list[float] = []
    for ln in block.lines:
        crop = cleaned[ln.y : ln.y2 + 1, ln.x : ln.x2 + 1]
        if crop.size:
            dens.append(float((crop > 0).mean()))
    if not dens:
        return False
    dens.sort()
    return dens[len(dens) // 2] >= _MIN_LINE_INK_DENSITY


@dataclass
class GLMOCREngine:
    """OCR engine for typewritten + handwritten material, via local glm-ocr.

    glm-ocr is a whole-page OCR model with no spatial output, and it cannot
    transcribe thin single-line crops (a full-width line shrunk to the model's
    <=1024 px longest edge collapses to ~30 px tall and returns empty). It
    transcribes *column-shaped* crops fine, though — they are tall strips of
    stacked lines, like a skinny page.

    Pipeline per page:
      1. Decompose the page into full-height single-column blocks via cv2
         layout analysis (``line_detect.detect_blocks`` — vertical projection
         for columns, horizontal projection for lines; no Tesseract, whose
         layout analysis fails on the cursive material glm-ocr exists for).
      2. Transcribe each block's crop through glm-ocr (Ollama), then flatten
         the markup it emits (HTML tables, LaTeX math) to plain text via
         ``markup.normalize_markup``. Transcribing per block means a block's
         text pairs with that block's own geometry, so we never have to guess
         glm-ocr's cross-column reading order — which also makes the .txt
         (blocks joined in reading order) correct on multi-column pages.
      3. Within each block, distribute its text over its line boxes — table/
         form rows map onto line boxes positionally (1:1 when counts match),
         prose is spread as a width-weighted word stream — then split each line
         into per-word ``<String>`` boxes (+ ``<SP>``) so a search highlights
         the individual word, not the whole line.

    Word x-positions are proportional (the model gives no word geometry), so
    they are approximate but ordered — the right word highlights at roughly the
    right spot. Column and line placement are exact. Blank or noise pages — where
    glm-ocr would otherwise fabricate a plausible fake transcript — are caught by
    an ink-density gate and emit an empty Layout; a page with real text that
    block detection missed still gets a whole-page fallback.
    """

    config: GLMOCRConfig = field(default_factory=GLMOCRConfig)
    detect_params: DetectParams = field(default_factory=DetectParams)
    name: str = "glmocr"

    def adjust_preprocess(self, config: PreprocessConfig) -> PreprocessConfig:
        # Two reasons to diverge from the user's default preprocessing:
        #
        # 1. Drop binarize. glm-ocr's vision projector hits ``GGML_ASSERT(
        #    a->ne[2] * 4 == b->ne[0])`` on certain binarized inputs
        #    (content-dependent — only some pages, even at the documented
        #    1024-edge sweet spot). Reproduced on McKennaPapers
        #    MSP00213_84c635...:_00022/_00026; disabling binarization makes
        #    both succeed. The model is a vision transformer that wants
        #    continuous grayscale.
        #
        # 2. Drop deskew + border-crop — the two frame-changing stages.
        #    Line boxes are detected on, and emitted in, the preprocessed
        #    image frame. Deskew (rotation) and border-crop (translation)
        #    move pixels relative to the original TIF, so with them on the
        #    ALTO coordinates no longer land on the original image the access
        #    platform displays. With them off, the preprocessed frame differs
        #    from the original only by the uniform DPI upscale, which cancels
        #    in the px→mm10 conversion — so boxes map directly onto the
        #    displayed page. (If a collection turns out to be skewed enough
        #    that detection/transcription suffers, the fix is to keep deskew
        #    on and invert the transform when emitting coordinates — see the
        #    remediation handoff's "v2" — not to leave deskew off here.)
        #
        # Denoise + CLAHE stay on: they sharpen the image for glm-ocr without
        # moving any pixels.
        return dc_replace(
            config, do_binarize=False, do_deskew=False, do_border_crop=False,
        )

    def ocr(
        self,
        image: np.ndarray,
        output_base: Path,
        *,
        dpi: int,
        page_index: int,
        source_filename: str | None = None,
    ) -> tuple[Path, Path]:
        output_base = Path(output_base)
        output_base.parent.mkdir(parents=True, exist_ok=True)

        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        page_h, page_w = gray.shape[:2]

        # Decompose into single-column blocks and transcribe each block's crop
        # on its own, so its text pairs with its own geometry (no guessing
        # glm-ocr's cross-column reading order). Per-word String boxes come from
        # splitting each line box proportionally. See line_detect.detect_blocks.
        blocks = detect_blocks(gray, self.detect_params)
        # Ink-density gate (load-bearing): only transcribe blocks that actually
        # contain dense text — glm-ocr hallucinates a fake transcript from a
        # blank/noise crop. See _block_has_real_text / _MIN_LINE_INK_DENSITY.
        cleaned = _clean_mask(gray, self.detect_params)
        real_blocks = [b for b in blocks if _block_has_real_text(cleaned, b)]
        if len(real_blocks) != len(blocks):
            logger.debug("glmocr: skipped %d near-empty block(s) (blank/noise) of %d",
                         len(blocks) - len(real_blocks), len(blocks))

        assigned: list[tuple[TextBlock, list[tuple[LineBox, str]]]] = []
        texts: list[str] = []
        for blk in real_blocks:
            crop = image[blk.y : blk.y + blk.h, blk.x : blk.x + blk.w]
            # glm-ocr renders tables as HTML and math as LaTeX; flatten that to
            # plain text so the .txt stays plain and word-splitting yields real
            # word tokens (not <td>/$ fragments). See markup.normalize_markup.
            block_text = normalize_markup(transcribe(crop, self.config))
            if block_text.strip():
                texts.append(block_text)
            assigned.append((blk, _assign_block_text(block_text, list(blk.lines))))

        if not real_blocks:
            if blocks:
                # Blocks were detected but all were near-empty -> blank/noise
                # page; emit an empty Layout rather than risk a hallucination.
                logger.debug("glmocr: all %d block(s) near-empty — blank page", len(blocks))
            else:
                # No layout at all. A whole-page pass can still recover real text
                # that block detection missed, but only when a genuinely dense
                # text band exists — a blank/noise page is left empty (glm-ocr
                # would otherwise fabricate a transcript).
                row_dens = (cleaned > 0).mean(axis=1)
                has_text = bool(row_dens.size) and (
                    float(np.percentile(row_dens, 99)) >= _MIN_LINE_INK_DENSITY
                )
                if has_text:
                    full = normalize_markup(transcribe(image, self.config))
                    if full.strip():
                        span = LineBox(0, 0, page_w, page_h)
                        assigned = [(TextBlock(0, 0, page_w, page_h, (span,)), [(span, full)])]
                        texts = [full]
                        logger.debug("glmocr: no blocks detected; whole-page fallback line")
                else:
                    logger.debug("glmocr: blank page (no dense text band) — empty layout")

        txt_out = "\n\n".join(texts)

        alto_xml = _build_alto_words(
            page_w=page_w, page_h=page_h,
            page_index=page_index,
            blocks=assigned,
            engine_name=self.name,
            engine_version=self.config.model,
        )

        alto_path = output_base.with_suffix(".xml")
        txt_path = output_base.with_suffix(".txt")
        alto_path.write_text(alto_xml, encoding="utf-8")
        txt_path.write_text(txt_out, encoding="utf-8")
        _convert_to_institution_format(
            alto_path, page_index, dpi=dpi, source_filename=source_filename,
        )
        return alto_path, txt_path
