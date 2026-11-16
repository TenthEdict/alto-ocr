#!/usr/bin/env python3
"""Turn digitization tracking sheets into a Goobi Excel mass-import file.

    python integration/goobi/tracking_to_goobi.py --collection MSA00005 \\
        msa5-box_103-105_digitization_tracking.xlsx \\
        msa5-box_106-108_digitization_tracking.xlsx \\
        -o MSA00005_goobi_import.xlsx

The output is what Goobi's *Mass import* screen takes with the
``intranda_import_excel`` plugin and ``config/plugin_intranda_import_excel.xml``
(one sheet, one header row, one row per process, every cell text).

Why a converter instead of uploading the tracking sheets directly:

* **The Ref ID column carries no collection prefix**, while Goobi's process
  title -- which ``import_images.sh`` and the OCR step key on -- is
  ``<collection>_<RefID>``. The collection is given once per run with
  ``--collection``; it is never guessed from the file name.
* **Tracking sheets drift in layout** ("Box Number" vs "Box", "Notes" vs
  "Archivist's Notes", columns present in one and absent in the other), and
  the plugin maps columns by exact header name. ``FIELDS`` below folds the
  variants into one fixed set of columns.
* **The plugin imports any row with any value in it**, so a totals row at the
  bottom of a sheet would become a process. Rows without a Ref ID are dropped
  here and reported.
* **Rows already in Goobi, or already through digitization, should not
  become new processes.** Both are skipped by default and listed;
  ``--include-started`` keeps the second kind.

Nothing here is specific to one collection: the collection is an argument,
the Ref ID is checked only for being usable as a folder name, and a Ref ID
whose *shape* differs from the rest of its sheet (``16354`` among 32-character
hashes) is warned about, not rejected -- a person decides.

Needs ``openpyxl`` (``pip install -r integration/goobi/requirements.txt``).
Exit status: 0 output written, 1 nothing importable or an input error,
2 usage error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Output columns, in order, and the tracking-sheet headers each one accepts.
# Header matching ignores case, spacing and punctuation, so "Ref ID", "RefID"
# and "Ref-ID" are the same header. ProcessTitle and Collection are made
# here, never read. Keep the output names in step with
# config/plugin_intranda_import_excel.xml.
FIELDS: dict[str, tuple[str, ...]] = {
    "ProcessTitle": (),
    "RefID": ("Ref ID",),
    "Collection": (),
    "Title": ("Title",),
    "Box": ("Box", "Box Number", "Box No"),
    "Folder": ("Folder",),
    "Series": ("Series",),
    "Subseries": ("Subseries",),
    "SubGroup": ("SubGroup", "Sub Group"),
    "TN": ("TN",),
    "Notes": ("Notes", "Archivist's Notes", "Notes to Digital Programs (pre-imaging)"),
}
# A row with a value here has been through capture: Goobi's Capture task
# would be a step behind the work.
STARTED_HEADERS = ("Date Digitized",)

DEFAULT_SHEET = "ProcessTracking"
DEFAULT_METADATA_ROOT = Path("/opt/digiverso/goobi/metadata")

# The collection and Ref ID become one directory name (the process title).
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Goobi image folders are "<title>_<suffix>"; any of these gives the title.
_IMAGE_FOLDER = re.compile(r"^(?P<title>.+)_(?:master|media|tif|orig)$")
_CATALOG_ID = re.compile(r'name="CatalogIDDigital">([^<]+)<')


def _norm(header: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower()) if header is not None else ""


def cell_text(value: object) -> str:
    """A cell value as the text Goobi should see.

    Whole-number floats lose their ".0" (Excel stores 103 as 103.0), dates
    become ISO, everything is stripped.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def ref_id_shape(ref_id: str) -> tuple[str, int]:
    """A coarse shape for outlier detection: character classes and length."""
    classes = "".join(sorted({"d" if c.isdigit() else "a" if c.isalpha() else "p" for c in ref_id}))
    return classes, len(ref_id)


@dataclass
class Row:
    source: str
    row_number: int
    values: dict[str, str]
    started: bool

    @property
    def ref_id(self) -> str:
        return self.values["RefID"]


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    no_ref_id: list[str] = field(default_factory=list)
    unsafe: list[str] = field(default_factory=list)
    no_title: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    odd_shape: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)


def header_map(header: Iterable[object]) -> tuple[dict[str, int], list[int]]:
    """Map output field -> column index; and the started-marker columns.

    The first column matching a field wins (tracking sheets can repeat a
    header, e.g. two "Upload by" columns).
    """
    index: dict[str, int] = {}
    started: list[int] = []
    wanted = {_norm(alias): name for name, aliases in FIELDS.items() for alias in aliases}
    started_keys = {_norm(h) for h in STARTED_HEADERS}
    for i, cell in enumerate(header):
        key = _norm(cell)
        if key in wanted and wanted[key] not in index:
            index[wanted[key]] = i
        if key in started_keys:
            started.append(i)
    return index, started


def read_sheet(path: Path, sheet: Optional[str], collection: str, report: Report) -> None:
    import openpyxl  # deferred so --help works without it

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet and sheet in wb.sheetnames:
            ws = wb[sheet]
        elif sheet and sheet != DEFAULT_SHEET:
            raise SystemExit(f"{path.name}: no sheet named {sheet!r} (has {', '.join(wb.sheetnames)})")
        else:
            # The default name is a convention, not a contract: fall back to
            # the first sheet, which is also the one the plugin itself reads.
            ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise SystemExit(f"{path.name}: sheet {ws.title!r} is empty")
        index, started_cols = header_map(header)
        if "RefID" not in index:
            raise SystemExit(f"{path.name}: sheet {ws.title!r} has no Ref ID column")
        for name, aliases in FIELDS.items():
            if aliases and name not in index:
                report.missing_columns.append(f"{path.name}: {name}")
        for number, raw in enumerate(rows, start=2):
            values = {name: cell_text(raw[i]) if i < len(raw) else "" for name, i in index.items()}
            where = f"{path.name} row {number}"
            if not any(cell_text(v) for v in raw):
                continue
            ref_id = values.get("RefID", "")
            if not ref_id:
                report.no_ref_id.append(where)
                continue
            if not _SAFE_TOKEN.fullmatch(ref_id):
                report.unsafe.append(f"{where}: {ref_id!r}")
                continue
            if not values.get("Title"):
                report.no_title.append(f"{where}: {ref_id}")
                continue
            values = {name: values.get(name, "") for name in FIELDS}
            values["RefID"] = ref_id
            values["Collection"] = collection
            values["ProcessTitle"] = f"{collection}_{ref_id}"
            started = any(i < len(raw) and cell_text(raw[i]) for i in started_cols)
            report.rows.append(Row(path.name, number, values, started))
    finally:
        wb.close()


def existing_titles(metadata_root: Optional[Path]) -> set[str]:
    """Process titles already present on this Goobi host.

    Read from the metadata tree, not the database, so it needs no
    credentials: each process folder's image folders are named
    ``<title>_master`` / ``<title>_media``, and its meta.xml carries the title
    as CatalogIDDigital. A process with neither (a template, or one whose
    metadata was never written) is not seen; Goobi's own title check is the
    backstop.
    """
    titles: set[str] = set()
    if metadata_root is None or not metadata_root.is_dir():
        return titles
    for proc in metadata_root.iterdir():
        images = proc / "images"
        if images.is_dir():
            for child in images.iterdir():
                m = _IMAGE_FOLDER.match(child.name)
                if m and child.is_dir():
                    titles.add(m.group("title"))
        meta = proc / "meta.xml"
        if meta.is_file():
            try:
                titles.update(_CATALOG_ID.findall(meta.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return titles


def select(report: Report, existing: set[str], include_started: bool) -> list[Row]:
    """The rows to import, recording why each other row was left out."""
    keep: list[Row] = []
    seen: set[str] = set()
    for row in report.rows:
        where = f"{row.source} row {row.row_number}: {row.ref_id}"
        title = row.values["ProcessTitle"]
        if title in seen:
            report.duplicate.append(where)
            continue
        seen.add(title)
        if title in existing:
            report.existing.append(where)
            continue
        if row.started and not include_started:
            report.started.append(where)
            continue
        keep.append(row)
    # Shape outliers, judged per source sheet against its most common shape.
    by_source: dict[str, list[Row]] = {}
    for row in report.rows:
        by_source.setdefault(row.source, []).append(row)
    for rows in by_source.values():
        shapes = Counter(ref_id_shape(r.ref_id) for r in rows)
        if len(shapes) < 2:
            continue
        common, count = shapes.most_common(1)[0]
        if count * 2 <= len(rows):
            continue  # no clear majority; nothing stands out
        for r in rows:
            if ref_id_shape(r.ref_id) != common:
                report.odd_shape.append(f"{r.source} row {r.row_number}: {r.ref_id}")
    return keep


def write_import(rows: list[Row], out: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Import"
    columns = list(FIELDS)
    for c, name in enumerate(columns, start=1):
        ws.cell(row=1, column=c, value=name)
    for r, row in enumerate(rows, start=2):
        for c, name in enumerate(columns, start=1):
            value = row.values[name]
            if not value:
                continue
            cell = ws.cell(row=r, column=c, value=value)
            # Every cell is text. openpyxl turns a string starting with "="
            # into a formula, which the plugin reads as an empty title or
            # crashes on; and a number cell would lose leading zeros.
            cell.data_type = "s"
            cell.number_format = "@"
    ws.freeze_panes = "A2"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    wb.save(tmp)
    tmp.replace(out)


def print_report(report: Report, kept: list[Row], out: Optional[Path], stream=None) -> None:
    stream = stream or sys.stdout
    def section(title: str, items: list[str]) -> None:
        if items:
            print(f"\n{title} ({len(items)}):", file=stream)
            for item in items:
                print(f"  {item}", file=stream)

    section("Skipped, already a Goobi process", report.existing)
    section("Skipped, already digitized (use --include-started to import)", report.started)
    section("Skipped, Ref ID repeated", report.duplicate)
    section("Skipped, no Title (the plugin requires one)", report.no_title)
    section("Skipped, Ref ID not usable as a folder name", report.unsafe)
    section("Ignored, no Ref ID (totals or notes rows)", report.no_ref_id)
    section("CHECK: Ref ID shaped unlike the rest of its sheet", report.odd_shape)
    section("Note: column not in sheet, left blank", report.missing_columns)
    target = f" -> {out}" if out else ""
    print(f"\n{len(kept)} process(es) to create{target}", file=stream)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sheets", nargs="+", type=Path, help="tracking .xlsx files, all from one collection")
    ap.add_argument("--collection", required=True,
                    help="collection prefix for the process titles, e.g. MSA00005")
    ap.add_argument("-o", "--output", type=Path,
                    help="import file to write (default: <collection>_goobi_import.xlsx here)")
    ap.add_argument("--sheet", default=DEFAULT_SHEET,
                    help=f"worksheet to read (default {DEFAULT_SHEET}, else the first sheet)")
    ap.add_argument("--include-started", action="store_true",
                    help="also import rows that already have a Date Digitized")
    ap.add_argument("--goobi-metadata-root", type=Path, default=DEFAULT_METADATA_ROOT,
                    help="Goobi metadata tree, to skip existing processes (default %(default)s; "
                         "ignored when absent)")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args(argv)

    if not _SAFE_TOKEN.fullmatch(args.collection) or "_" in args.collection:
        ap.error(f"--collection {args.collection!r}: letters, digits, '.' and '-' only "
                 "(it is the part of the process title before the first '_')")
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("openpyxl is not installed: pip install -r integration/goobi/requirements.txt",
              file=sys.stderr)
        return 1

    report = Report()
    try:
        for path in args.sheets:
            if not path.is_file():
                raise SystemExit(f"{path}: no such file")
            read_sheet(path, args.sheet, args.collection, report)
    except SystemExit as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    kept = select(report, existing_titles(args.goobi_metadata_root), args.include_started)
    out = None if args.dry_run else (args.output or Path(f"{args.collection}_goobi_import.xlsx"))
    if kept and out is not None:
        write_import(kept, out)
    print_report(report, kept, out)
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
