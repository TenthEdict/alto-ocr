"""METS XML builder.

One METS file per Ref ID. The METS describes the object as two parallel
file groups (ALTO OCR XML, plain-text transcript) and a ``structMap`` of
type "physical" that pairs the files for each page. The master TIFs live
in the institution's preservation tree (``preservation-files/preservation/
<RefID>/``), not in the OCR output, so they are not referenced from this
METS — the upload workflow pairs the two trees by Ref ID externally.

We aim for the Library of Congress METS profile most archives consume: a
single ``mets`` root with ``metsHdr``, ``fileSec``, and ``structMap``. No
``dmdSec``/``amdSec`` content is generated here — that's collection-level
metadata the institution adds in its own pipeline. We leave well-formed
hooks so it can be merged in later without restructuring.

File references are **relative paths** inside the Ref ID output directory
(``ALTO/image001.xml`` etc.) so the METS is portable: zip the Ref ID
folder and the references still resolve.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from . import __version__
from ._util import atomic_write_bytes

logger = logging.getLogger(__name__)

METS_NS = "http://www.loc.gov/METS/"
XLINK_NS = "http://www.w3.org/1999/xlink"
NSMAP = {None: METS_NS, "xlink": XLINK_NS}

MIME_BY_GROUP = {
    "ALTO": "application/xml",
    "Text": "text/plain",
    "Image": "image/tiff",
}
USE_BY_GROUP = {
    "ALTO": "ocr",
    "Text": "transcript",
    "Image": "preservation",
}
EXT_BY_GROUP = {
    "ALTO": ".xml",
    "Text": ".txt",
    "Image": ".tif",
}
# Subdirectory each group occupies in the pipeline's working layout. Images
# live outside the object directory entirely (Goobi keeps them under
# images/<RefID>_master), so they have no working subdirectory and are only
# ever described by a delivery METS, which is flat.
SUBDIR_BY_GROUP = {
    "ALTO": "ALTO",
    "Text": "Text",
    "Image": None,
}


@dataclass(frozen=True)
class PageEntry:
    """One page's ALTO + (optional) Text files, identified by their
    shared stem. ALTO uses ``.xml``; Text uses ``.txt``.
    """

    stem: str
    order: int  # 1-based


_NCNAME_INVALID = re.compile(r"[^A-Za-z0-9_.\-]")


def _to_ncname(stem: str) -> str:
    """Coerce an arbitrary filename stem into a valid xs:ID NCName.

    xs:ID restricts values to NCName: must start with letter or ``_``;
    remaining characters may include letters, digits, ``.``, ``-``, ``_``.
    Spaces, slashes, parentheses, etc. are illegal — they appear in real
    archival filenames, so we sanitize. Replace illegal chars with ``_``;
    prefix ``f_`` if the result starts with a digit or is empty.
    """
    cleaned = _NCNAME_INVALID.sub("_", stem)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"f_{cleaned}"
    return cleaned


def _file_id(
    group: str, stem: str, taken: set[str] | None = None
) -> str:
    """Stable, valid xs:ID for a single file.

    If sanitization would collide with an ID already in ``taken``, suffix
    with a short deterministic hash of the raw stem so each file still gets
    a unique ID. The METS LABEL/href continue to carry the raw stem so a
    human can still pair files visually.
    """
    candidate = f"{group.lower()}_{_to_ncname(stem)}"
    if taken is None or candidate not in taken:
        return candidate
    suffix = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:6]
    return f"{candidate}_{suffix}"


def _checksum_md5(path: Path) -> str | None:
    """MD5 hash of a file. Returns None on read failure (logged, not raised)."""
    try:
        h = hashlib.md5()  # noqa: S324 — METS uses MD5 for fixity, not security
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        logger.warning("Could not compute MD5 for %s: %s", path, e)
        return None


def _file_element(
    group: str,
    stem: str,
    ref_id_dir: Path,
    *,
    with_checksums: bool,
    file_id: str,
    flat_hrefs: bool = False,
    source_dir: Path | None = None,
) -> etree._Element:
    """One ``<mets:file>``.

    Two paths are involved and they are deliberately separate. The *href* is
    what a consumer of the package will resolve, and depends on the layout the
    METS describes: nested (``ALTO/x.xml``) for the pipeline's working tree,
    bare (``x.xml``) for the flat delivered folder. The *source* is where this
    machine reads the bytes to size and checksum them, which for images is
    outside the object directory altogether.
    """
    ext = EXT_BY_GROUP[group]
    subdir = SUBDIR_BY_GROUP[group]
    rel = f"{stem}{ext}" if (flat_hrefs or subdir is None) else f"{subdir}/{stem}{ext}"
    if source_dir is not None:
        abs_path = source_dir / f"{stem}{ext}"
    else:
        abs_path = ref_id_dir / (f"{subdir}/{stem}{ext}" if subdir else f"{stem}{ext}")

    attrs = {
        "ID": file_id,
        "MIMETYPE": MIME_BY_GROUP[group],
    }
    if abs_path.is_file():
        attrs["SIZE"] = str(abs_path.stat().st_size)
        if with_checksums:
            cksum = _checksum_md5(abs_path)
            if cksum:
                attrs["CHECKSUM"] = cksum
                attrs["CHECKSUMTYPE"] = "MD5"

    file_el = etree.Element(f"{{{METS_NS}}}file", attrs)
    flocat = etree.SubElement(
        file_el,
        f"{{{METS_NS}}}FLocat",
        {
            "LOCTYPE": "URL",
            f"{{{XLINK_NS}}}href": rel,
        },
    )
    flocat.text = None
    return file_el


def build_mets(
    ref_id: str,
    ref_id_dir: Path,
    pages: list[PageEntry],
    *,
    agent_name: str = "alto_ocr",
    agent_version: str = __version__,
    with_checksums: bool = True,
    with_plaintext: bool = True,
    image_dir: Path | None = None,
    flat_hrefs: bool = False,
) -> etree._ElementTree:
    """Build the METS document for one Ref ID.

    Two kinds of METS come out of this, and they describe different layouts:

    * the **working** METS (defaults) describes the pipeline's own tree, with
      nested ``ALTO/`` and ``Text/`` hrefs and no images. It doubles as this
      tool's fixity record — the re-OCR guard compares its checksums to spot
      corrections made by editors that leave no provenance — so its hrefs must
      resolve against ``ref_id_dir``.
    * the **delivery** METS (``image_dir`` set, ``flat_hrefs=True``) describes
      the flat folder handed to the repository, where images, ALTO and text sit
      side by side. A manifest that listed only the OCR outputs while sitting
      beside the images would be worse than none, because it looks complete.

    Args:
        ref_id: object identifier (used as METS OBJID).
        ref_id_dir: the output directory containing ``ALTO/`` and (when
            ``with_plaintext``) ``Text/`` subdirs.
        pages: ordered page entries (one per page, in display order).
        with_checksums: include MD5 checksums for fixity (slower for large
            objects; default on).
        with_plaintext: include the ``Text`` fileGrp and structMap fptrs.
            Set False when the pipeline ran with plaintext suppressed so
            the METS doesn't reference files that aren't on disk.
        image_dir: directory holding the source TIFFs. When given, an
            ``Image`` fileGrp is added and each page's div points at its image
            as well. The images are read from here for size and checksum but
            described by filename only, since they are not inside
            ``ref_id_dir``.
        flat_hrefs: write bare filenames instead of ``ALTO/…`` / ``Text/…``,
            for a delivered folder that is flat.
    """
    groups: list[str] = ["ALTO"]
    if with_plaintext:
        groups.append("Text")
    if image_dir is not None:
        groups.insert(0, "Image")  # preservation copy leads the fileSec
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    root = etree.Element(
        f"{{{METS_NS}}}mets",
        nsmap=NSMAP,
        attrib={
            "OBJID": ref_id,
            "TYPE": "archival_object",
            "LABEL": ref_id,
        },
    )

    hdr = etree.SubElement(
        root,
        f"{{{METS_NS}}}metsHdr",
        {"CREATEDATE": now, "RECORDSTATUS": "COMPLETE"},
    )
    agent = etree.SubElement(
        hdr,
        f"{{{METS_NS}}}agent",
        {"ROLE": "CREATOR", "TYPE": "OTHER", "OTHERTYPE": "SOFTWARE"},
    )
    etree.SubElement(agent, f"{{{METS_NS}}}name").text = f"{agent_name} {agent_version}"

    # Pre-allocate xs:ID values for every (group, page) pair so collisions
    # across sanitized stems can be detected and disambiguated before the
    # structMap references them.
    taken_ids: set[str] = set()
    file_ids: dict[tuple[str, int], str] = {}
    for group in groups:
        for page in pages:
            fid = _file_id(group, page.stem, taken=taken_ids)
            taken_ids.add(fid)
            file_ids[(group, page.order)] = fid

    # fileSec: one entry per page per active group.
    file_sec = etree.SubElement(root, f"{{{METS_NS}}}fileSec")
    for group in groups:
        grp = etree.SubElement(
            file_sec,
            f"{{{METS_NS}}}fileGrp",
            {"USE": USE_BY_GROUP[group], "ID": f"grp_{group.lower()}"},
        )
        for page in pages:
            grp.append(
                _file_element(
                    group, page.stem, ref_id_dir,
                    with_checksums=with_checksums,
                    file_id=file_ids[(group, page.order)],
                    flat_hrefs=flat_hrefs,
                    source_dir=image_dir if group == "Image" else None,
                )
            )

    # structMap: physical layout — one div per page, pointing at its ALTO
    # (and Text, if enabled) files.
    struct = etree.SubElement(
        root,
        f"{{{METS_NS}}}structMap",
        {"TYPE": "physical", "LABEL": "Page order"},
    )
    obj_div = etree.SubElement(
        struct,
        f"{{{METS_NS}}}div",
        {"TYPE": "object", "LABEL": ref_id},
    )
    page_div_taken: set[str] = set()
    for page in pages:
        page_div_id = _file_id("page", page.stem, taken=page_div_taken)
        page_div_taken.add(page_div_id)
        page_div = etree.SubElement(
            obj_div,
            f"{{{METS_NS}}}div",
            {
                "TYPE": "page",
                "ORDER": str(page.order),
                "LABEL": page.stem,
                "ID": page_div_id,
            },
        )
        for group in groups:
            etree.SubElement(
                page_div,
                f"{{{METS_NS}}}fptr",
                {"FILEID": file_ids[(group, page.order)]},
            )

    return etree.ElementTree(root)


def write_mets(tree: etree._ElementTree, dest: Path) -> None:
    """Serialize METS to disk with stable formatting.

    Atomic (``_util.atomic_write_bytes``): a crash mid-write can't leave a
    truncated METS behind. ``etree.tostring`` with these arguments produces
    exactly the bytes ``tree.write`` used to.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(dest, etree.tostring(
        tree,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    ))
