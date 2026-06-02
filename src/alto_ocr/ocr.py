"""OCR engines.

The pipeline talks to OCR engines through the ``OCREngine`` Protocol —
a single ``ocr(image, output_base, *, dpi)`` method that produces an ALTO
``.xml`` and a plain ``.txt`` alongside ``output_base`` and returns their
paths. ``TesseractEngine`` is the default implementation; ``GLMOCREngine``
will be added behind ``--engine glmocr`` in a follow-up.

Tesseract notes:

* Two invocations per page — one with the ``alto`` config (→ ``.xml``),
  one with the default config (→ ``.txt``). Tesseract appends the extension
  based on the active config, so we always pass an *extension-less* base.
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
from .line_detect import DetectParams, LineBox, TextBlock, detect_blocks
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
    config: str | None,
) -> None:
    """Single Tesseract invocation. ``config`` is e.g. 'alto' or None (txt)."""
    cmd: list[str] = [
        str(binary),
        str(image_path),
        str(output_base),
        "-l", options.language,
        "--psm", str(options.psm),
        "--oem", str(options.oem),
        "--dpi", str(dpi),
    ]
    if config:
        cmd.append(config)

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
    """Default OCR engine: shell out to Tesseract twice (alto + txt configs)."""

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
            _run_tesseract(self.binary, tmp, output_base, self.options, dpi, config="alto")
            _run_tesseract(self.binary, tmp, output_base, self.options, dpi, config=None)
        finally:
            try:
                tmp.unlink()
            except OSError:
                logger.debug("Could not remove temp file: %s", tmp)

        alto = output_base.with_suffix(".xml")
        txt = output_base.with_suffix(".txt")
        if not alto.is_file() or not txt.is_file():
            raise RuntimeError(
                f"Tesseract reported success but expected outputs are missing: "
                f"alto={alto.is_file()}, txt={txt.is_file()}"
            )
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


def _assign_block_text(block_text: str, lines: list[LineBox]) -> list[tuple[LineBox, str]]:
    """Spread a block's transcript across its line boxes as a word stream.

    glm-ocr flattens a column into flowing paragraphs, not one string per
    visual line, so there are far fewer text "lines" than detected line boxes.
    Bucketing text-lines onto boxes would cram a whole paragraph into a single
    box (and overflow its width). Instead the block's words are distributed
    across the boxes in reading order, each box getting a share proportional to
    its width (a wider line holds more words). Because the block was transcribed
    on its own, top to bottom, glm-ocr's word order matches the boxes'
    top-to-bottom order, so each word lands on roughly its real line; within a
    line, ``_split_line_words`` places it by character offset.

    Word placement is therefore approximate (the model supplies no geometry)
    but ordered and width-weighted — enough to highlight the right word near its
    real spot. Boxes that receive no words are dropped by the ALTO builder.
    """
    words = block_text.split()
    if not lines:
        return []
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
      2. Transcribe each block's crop through glm-ocr (Ollama). Transcribing
         per block means a block's text pairs with that block's own geometry,
         so we never have to guess glm-ocr's cross-column reading order — which
         also makes the .txt (blocks joined in reading order) correct on
         multi-column pages.
      3. Within each block, distribute its text over its line boxes, then split
         each line into per-word ``<String>`` boxes (+ ``<SP>``) so a search
         highlights the individual word, not the whole line.

    Word x-positions are proportional (the model gives no word geometry), so
    they are approximate but ordered — the right word highlights at roughly the
    right spot. Column and line placement are exact. Pages with no detectable
    layout fall back to a single whole-page transcription under one line; truly
    blank pages emit an empty Layout, matching Tesseract.
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
        assigned: list[tuple[TextBlock, list[tuple[LineBox, str]]]] = []
        texts: list[str] = []
        for blk in blocks:
            crop = image[blk.y : blk.y + blk.h, blk.x : blk.x + blk.w]
            block_text = transcribe(crop, self.config)
            if block_text.strip():
                texts.append(block_text)
            assigned.append((blk, _assign_block_text(block_text, list(blk.lines))))

        if not blocks:
            # No detectable layout: try one whole-page pass so a sparse page
            # still yields searchable text under a single page-spanning line; a
            # truly blank page leaves an empty Layout, matching Tesseract.
            full = transcribe(image, self.config)
            if full.strip():
                span = LineBox(0, 0, page_w, page_h)
                assigned = [(TextBlock(0, 0, page_w, page_h, (span,)), [(span, full)])]
                texts = [full]
                logger.debug("glmocr: no blocks detected; whole-page fallback line")
            else:
                logger.debug("glmocr: blank page (no transcribed text)")

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
