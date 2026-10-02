"""Confidence triage: rank pages/objects by OCR confidence, worst-first.

The remediation workflow is triage-first — correctors should not proofread
everything, they should start where the OCR is weakest. Tesseract runs
write ``WC`` (0..1) onto every ALTO ``<String>`` and a mean ``PC`` onto
``<Page>`` (see ``ocr._merge_tsv_confidence``); this module reads those
attributes back out of an output tree and aggregates them into a
worst-first review queue.

Reading from the *output tree* (rather than collecting in-band during OCR)
is deliberate: the same scan works on a batch that just finished, on last
month's outputs, and after handwriting objects were re-run with GLMOCR —
one implementation, identical numbers everywhere.

Page classification:

* ``failed``   — a pipeline failure stub (``Processing ID="failed"`` or no
  ``<Page>`` at all). No transcript exists; the page needs a re-run, not
  proofreading. Sorts first in the review queue.
* ``scored``   — has words carrying ``WC`` (a Tesseract page). Ranked by
  ``PC`` ascending; this is the main correction queue.
* ``unscored`` — has words but no ``WC`` (a GLMOCR page; glm-ocr emits no
  confidences). Needs full proofreading — there is no signal to narrow it.
* ``empty``    — a well-formed page with zero words (blank page). Nothing
  to correct; sorts last.

Thresholds are report-time parameters, not stored state: ``low_wc`` flags
individual words below it (default 0.50), ``review_pc`` flags scored pages
below it for human review (default 0.80). Re-running with different
thresholds just re-scans — the scan is a cheap attribute read.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .alto_format import hardened_parser
from .remediate import iter_object_dirs
from .walker import natural_chunks, natural_key

logger = logging.getLogger(__name__)

DEFAULT_LOW_WC = 0.50
DEFAULT_REVIEW_PC = 0.80

# Worst-first ordering for the page-level review queue. Shared with the
# Review tab's page list so the two queues always sort statuses alike.
STATUS_RANK = {"failed": 0, "scored": 1, "unscored": 2, "empty": 3}


@dataclass(frozen=True)
class PageConfidence:
    """Confidence facts read from one ALTO file."""

    stem: str
    status: str  # "failed" | "scored" | "unscored" | "empty"
    pc: float | None
    words: int
    scored_words: int  # words carrying a WC attribute
    low_words: int     # scored words with WC < low_wc
    wc_sum: float      # sum of WC over scored words (for exact means)

    @property
    def pct_low(self) -> float | None:
        """Share of scored words below the low-WC threshold (0..100)."""
        if self.scored_words == 0:
            return None
        return 100.0 * self.low_words / self.scored_words


@dataclass
class ObjectConfidence:
    """Aggregated confidence for one Ref ID's output directory."""

    ref_id: str
    pages: list[PageConfidence] = field(default_factory=list)
    review_pc: float = DEFAULT_REVIEW_PC

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def _count(self, status: str) -> int:
        return sum(1 for p in self.pages if p.status == status)

    @property
    def scored_pages(self) -> int:
        return self._count("scored")

    @property
    def unscored_pages(self) -> int:
        return self._count("unscored")

    @property
    def empty_pages(self) -> int:
        return self._count("empty")

    @property
    def failed_pages(self) -> int:
        return self._count("failed")

    @property
    def total_words(self) -> int:
        return sum(p.words for p in self.pages)

    @property
    def low_words(self) -> int:
        return sum(p.low_words for p in self.pages)

    @property
    def scored_words(self) -> int:
        return sum(p.scored_words for p in self.pages)

    @property
    def mean_wc(self) -> float | None:
        """Exact mean of every word confidence in the object (not a mean of
        page means — a 1,000-word page counts 1,000×, a 5-word page 5×)."""
        n = self.scored_words
        if n == 0:
            return None
        return sum(p.wc_sum for p in self.pages) / n

    @property
    def min_pc(self) -> float | None:
        scored = [p.pc for p in self.pages if p.status == "scored" and p.pc is not None]
        return min(scored) if scored else None

    @property
    def min_pc_stem(self) -> str | None:
        scored = [p for p in self.pages if p.status == "scored" and p.pc is not None]
        return min(scored, key=lambda p: p.pc).stem if scored else None

    @property
    def review_pages(self) -> list[PageConfidence]:
        """Scored pages whose PC is below the review threshold, worst-first."""
        flagged = [
            p for p in self.pages
            if p.status == "scored" and p.pc is not None and p.pc < self.review_pc
        ]
        return sorted(flagged, key=lambda p: p.pc)

    def summary_dict(self) -> dict:
        """JSON-ready aggregate (used by the batch --summary-json)."""
        mean = self.mean_wc
        return {
            "scored_pages": self.scored_pages,
            "unscored_pages": self.unscored_pages,
            "empty_pages": self.empty_pages,
            "failed_pages": self.failed_pages,
            "total_words": self.total_words,
            "low_conf_words": self.low_words,
            "mean_wc": round(mean, 3) if mean is not None else None,
            "min_page_pc": self.min_pc,
            "min_page": self.min_pc_stem,
            "review_pages": len(self.review_pages),
        }


def scan_alto_file(alto_path: Path, *, low_wc: float = DEFAULT_LOW_WC) -> PageConfidence:
    """Read confidence facts from one ALTO file (namespace-agnostic)."""
    root = etree.parse(str(alto_path), hardened_parser()).getroot()

    for proc in root.iter("{*}Processing"):
        if proc.get("ID") == "failed":
            return PageConfidence(alto_path.stem, "failed", None, 0, 0, 0, 0.0)
    page = next(iter(root.iter("{*}Page")), None)
    if page is None:
        return PageConfidence(alto_path.stem, "failed", None, 0, 0, 0, 0.0)

    pc: float | None = None
    try:
        pc = float(page.get("PC"))
    except (TypeError, ValueError):
        pass

    words = scored = low = 0
    wc_sum = 0.0
    for el in root.iter("{*}String"):
        words += 1
        try:
            wc = float(el.get("WC"))
        except (TypeError, ValueError):
            continue
        scored += 1
        wc_sum += wc
        if wc < low_wc:
            low += 1

    if words == 0:
        status = "empty"
    elif scored > 0:
        status = "scored"
    else:
        status = "unscored"

    if pc is None and scored > 0:
        # A page with per-word WC but no Page/@PC means the confidence merge
        # wrote the words and then failed before stamping the page mean —
        # Tesseract's own ALTO renderer emits WC natively, ours adds PC. Left
        # as None the page drops out of review_pages entirely and no corrector
        # ever sees it, which is the worst possible failure mode for a page we
        # know nothing good about. Recompute the mean from the WCs on hand:
        # exactly what the merge would have stored, and the same derivation
        # remediate already applies when it rewrites PC after an edit.
        pc = wc_sum / scored

    return PageConfidence(alto_path.stem, status, pc, words, scored, low, wc_sum)


def scan_alto_file_safe(alto_path: Path, *,
                        low_wc: float = DEFAULT_LOW_WC) -> PageConfidence:
    """``scan_alto_file`` that classifies an unreadable file as ``failed``
    instead of raising.

    Every caller that lists pages — the triage report, the Tk Review tab,
    the browser review's discovery — goes through this, so one corrupt ALTO
    (truncated by a crash: Tesseract writes its ALTO non-atomically) shows
    up as a ``failed`` page at the top of the queue rather than taking the
    whole object down with it.
    """
    try:
        return scan_alto_file(alto_path, low_wc=low_wc)
    except Exception as e:  # noqa: BLE001 - one bad file must not kill the listing
        logger.warning("could not parse %s (%s); counting as failed", alto_path, e)
        return PageConfidence(alto_path.stem, "failed", None, 0, 0, 0, 0.0)


def scan_object(
    object_dir: Path,
    *,
    low_wc: float = DEFAULT_LOW_WC,
    review_pc: float = DEFAULT_REVIEW_PC,
) -> ObjectConfidence | None:
    """Scan one Ref ID output directory (``<object_dir>/ALTO/*.xml``).

    Returns ``None`` when there is no ALTO folder (e.g. an aborted Ref ID
    whose output was never created). An unreadable ALTO file is counted as
    a ``failed`` page rather than aborting the scan.
    """
    alto_dir = object_dir / "ALTO"
    if not alto_dir.is_dir():
        return None
    obj = ObjectConfidence(ref_id=object_dir.name, review_pc=review_pc)
    for alto_path in sorted(alto_dir.glob("*.xml"), key=natural_key):
        obj.pages.append(scan_alto_file_safe(alto_path, low_wc=low_wc))
    return obj


def scan_output_root(
    output_root: Path,
    *,
    low_wc: float = DEFAULT_LOW_WC,
    review_pc: float = DEFAULT_REVIEW_PC,
) -> list[ObjectConfidence]:
    """Scan an output tree: every ``<root>/<RefID>/ALTO/`` per the output
    contract, or ``<root>/ALTO/`` when pointed directly at one object.

    Returns objects sorted worst-first: lowest mean WC first, objects with
    no confidence data (all-GLMOCR / all-empty) after the scored ones.
    """
    objects = []
    for obj_dir in iter_object_dirs(output_root):
        obj = scan_object(obj_dir, low_wc=low_wc, review_pc=review_pc)
        if obj is None:
            continue
        # A directly-targeted object is reported even with zero pages (the
        # 0-page row is the diagnostic); in a root scan, page-less objects
        # are just noise and are dropped.
        if obj.page_count > 0 or obj_dir == output_root:
            objects.append(obj)
    objects.sort(key=lambda o: (o.mean_wc is None, o.mean_wc if o.mean_wc is not None else 0.0))
    return objects


def worst_pages(objects: list[ObjectConfidence]) -> list[tuple[str, PageConfidence]]:
    """Flatten to ``(ref_id, page)`` in review-queue order: failure stubs
    first (no transcript at all), then scored pages by PC ascending, then
    unscored (GLMOCR — full proofread), then empty pages last."""
    rows = [(o.ref_id, p) for o in objects for p in o.pages]
    rows.sort(key=lambda rp: (
        STATUS_RANK.get(rp[1].status, 9),
        rp[1].pc if rp[1].pc is not None else 2.0,
        rp[0],
        natural_chunks(rp[1].stem),
    ))
    return rows


def _fmt(v: float | None, places: int = 2) -> str:
    return "" if v is None else f"{v:.{places}f}"


def _excel_safe(value: str) -> str:
    """Neutralize spreadsheet formula injection for cells sourced from
    filesystem names: Excel executes cells starting with ``=`` ``+`` ``-``
    ``@``. A leading apostrophe marks the cell as text (the quote itself is
    not displayed). The triage CSVs are explicitly opened in Excel."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@") else value


def write_csvs(objects: list[ObjectConfidence], out_dir: Path) -> tuple[Path, Path]:
    """Write ``triage_objects.csv`` + ``triage_pages.csv`` into ``out_dir``.

    Both are worst-first, Excel-friendly (plain columns, empty cell = no
    data). Returns the two paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    obj_csv = out_dir / "triage_objects.csv"
    pages_csv = out_dir / "triage_pages.csv"

    with obj_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "ref_id", "pages", "scored", "unscored", "empty", "failed",
            "total_words", "low_conf_words", "pct_low_words",
            "mean_wc", "min_page_pc", "min_page", "review_pages",
        ])
        for o in objects:
            pct_low = (100.0 * o.low_words / o.scored_words) if o.scored_words else None
            w.writerow([
                _excel_safe(o.ref_id), o.page_count, o.scored_pages, o.unscored_pages,
                o.empty_pages, o.failed_pages,
                o.total_words, o.low_words, _fmt(pct_low, 1),
                _fmt(o.mean_wc, 3), _fmt(o.min_pc), _excel_safe(o.min_pc_stem or ""),
                len(o.review_pages),
            ])

    with pages_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "ref_id", "page", "status", "pc", "words",
            "low_conf_words", "pct_low_words",
        ])
        for ref_id, p in worst_pages(objects):
            w.writerow([
                _excel_safe(ref_id), _excel_safe(p.stem), p.status, _fmt(p.pc), p.words,
                p.low_words, _fmt(p.pct_low, 1),
            ])

    return obj_csv, pages_csv
