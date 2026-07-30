"""Institution-format ALTO: shared serialization + end-to-end conversion.

This is the layer every ALTO-writing tool shares — the reference attribute
order, the tab-indented serialization, px→mm10 conversion, and the
normalizer that rewrites engine output (Tesseract's ALTO v3, the GLMOCR
builder's v2) into the institution's reference format. Extracted from
``ocr.py`` (2026-07-30) so the remediation layer (review save, repackage)
depends on this small lxml-only module instead of importing the engine
module's privates — which also dragged cv2/numpy into every ALTO-editing
import path.

``convert_to_institution_format`` normalizes a file end-to-end:

  * Sequential ``P{N}`` page IDs and element-type-prefixed child IDs
    (``P{N}_TB00001`` / ``P{N}_TL00001`` / ``P{N}_ST00001`` etc.).
  * Explicit Top/Left/Right/BottomMargin elements derived from the
    bounding box of the printed area; PrintSpace shrunk to that bbox.
  * MeasurementUnit ``mm10`` (1/10 mm) with all HPOS/VPOS/WIDTH/HEIGHT
    values converted from the OCR's pixel space using the page's DPI.
  * ALTO v2 namespace + schema location.
  * ``<sourceImageInformation>`` referencing the source TIF basename.
  * Per-element-type attribute order matching the reference samples.
  * Tab indentation, double-quoted XML declaration.

The converter is idempotent: it detects whether coordinates are already
in mm10 (via MeasurementUnit) and skips re-conversion on subsequent
passes; existing margins are stripped before recomputing; the namespace
swap is a no-op when the input is already v2. Locked by
``tests/test_institution_format.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

from ._util import atomic_write_text

ALTO_NS_V2 = "http://www.loc.gov/standards/alto/ns-v2#"
ALTO_NS_V3 = "http://www.loc.gov/standards/alto/ns-v3#"
ALTO_V2_SCHEMA_URL = "http://www.loc.gov/standards/alto/v2/alto-2-0.xsd"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XLINK_NS = "http://www.w3.org/1999/xlink"

# Backwards-compatible alias for code that imports the old single-version
# constant.
ALTO_NS = ALTO_NS_V2


def alto_namespace(root: etree._Element) -> str | None:
    """Namespace of the document's elements, from the root's own qualified
    name — the single idiom for ALTO namespace resolution.

    Works for default namespaces, prefixed namespaces (external editors),
    and namespace-less documents (returns ``None``). ``root.nsmap.get(None)``
    silently misses prefixed docs; never use it for ALTO.
    """
    return etree.QName(root).namespace


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
ATTR_ORDER: dict[str, tuple[str, ...]] = {
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


def qname(ns: str | None, tag: str) -> str:
    return f"{{{ns}}}{tag}" if ns else tag


def px_to_mm10(px: int, dpi: int) -> int:
    """Convert a pixel value to ALTO ``mm10`` units (1/10 mm).

    1 inch = 25.4 mm = 254 mm10, so ``mm10 = px * 254 / dpi``. Result is
    rounded to the nearest integer (ALTO coordinate attributes are integer
    strings).
    """
    if dpi <= 0:
        return px  # safety net; caller should validate dpi
    return int(round(px * 254 / dpi))


def reorder_attrs(elem: etree._Element, order: tuple[str, ...]) -> None:
    current = dict(elem.attrib)
    elem.attrib.clear()
    for key in order:
        if key in current:
            elem.set(key, current.pop(key))
    for key, value in current.items():
        elem.set(key, value)


def _change_default_namespace(root: etree._Element, new_ns: str) -> etree._Element:
    """Rebuild the tree under a new default namespace, preserving xsi/xlink."""
    old_ns = alto_namespace(root)
    nsmap = {None: new_ns, "xsi": XSI_NS, "xlink": XLINK_NS}

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


def serialize_with_tabs(tree: etree._ElementTree, alto_path: Path) -> None:
    """Write tree with tab indentation and a double-quoted XML declaration.

    lxml's ``pretty_print`` uses 2-space indents and its XML declaration
    uses single quotes; the institution's reference samples use tabs and
    double quotes. We post-process the serialized string to match.

    The write is atomic (``_util.atomic_write_text``): a crash or I/O error
    mid-save can never truncate an existing ALTO — load-bearing once a file
    carries hand corrections.
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
    atomic_write_text(
        alto_path, '<?xml version="1.0" encoding="UTF-8"?>\n' + body,
    )


def convert_to_institution_format(
    alto_path: Path,
    page_index: int,
    *,
    dpi: int,
    source_filename: str | None = None,
) -> None:
    """Rewrite the ALTO file at ``alto_path`` to match the institution's
    reference format. See the module docstring for the full list of
    transformations.

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
    src_ns = alto_namespace(root)

    desc = root.find(f"./{qname(src_ns, 'Description')}")
    mu_el = desc.find(f"./{qname(src_ns, 'MeasurementUnit')}") if desc is not None else None
    needs_unit_conversion = mu_el is None or (mu_el.text or "").strip() != "mm10"

    page = root.find(f".//{qname(src_ns, 'Page')}")
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
            for el in page.findall(qname(src_ns, margin_tag)):
                page.remove(el)

        print_space = page.find(qname(src_ns, "PrintSpace"))
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
                        el = etree.Element(qname(src_ns, tag), attrib={
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
            mu_el = etree.Element(qname(src_ns, "MeasurementUnit"))
            desc.insert(0, mu_el)
        mu_el.text = "mm10"

        if source_filename is not None:
            sii = desc.find(f"./{qname(src_ns, 'sourceImageInformation')}")
            if sii is None:
                sii = etree.Element(qname(src_ns, "sourceImageInformation"))
                desc.insert(list(desc).index(mu_el) + 1, sii)
            fn = sii.find(f"./{qname(src_ns, 'fileName')}")
            if fn is None:
                fn = etree.SubElement(sii, qname(src_ns, "fileName"))
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
                el.set(attr, str(px_to_mm10(px, dpi)))

    for el in root.iter():
        local = etree.QName(el).localname
        order = ATTR_ORDER.get(local)
        if order is not None:
            reorder_attrs(el, order)

    if src_ns != ALTO_NS_V2:
        new_root = _change_default_namespace(root, ALTO_NS_V2)
        tree = etree.ElementTree(new_root)
        root = new_root

    root.set(
        f"{{{XSI_NS}}}schemaLocation",
        f"{ALTO_NS_V2} {ALTO_V2_SCHEMA_URL}",
    )

    serialize_with_tabs(tree, alto_path)
