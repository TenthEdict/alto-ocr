"""Which objects exist, where their ALTO and scans are, and what state
they are in.

Two hard rules here, both security-relevant:

* **A Ref ID from a URL is never joined into a path.** It is looked up in an
  index built by walking the configured roots, and the answer is a path the
  server discovered for itself. Joining ``ref_id`` onto a root would make
  every review URL a directory traversal, and Ref IDs arrive from a browser.
* **Page stems are resolved the same way** -- against the ALTO folder's own
  listing and the source-image index, never by concatenation.

Object state is *derived*, never stored: the same choice ``triage.py`` makes.
A scan of the ALTO on disk tells the truth about pages corrected by any tool
(this server, the Tk GUI, an external editor), where a state file would only
tell the truth about edits that went through us.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..remediate import has_review_provenance, index_source_images, iter_object_dirs
from ..triage import DEFAULT_LOW_WC, STATUS_RANK, scan_alto_file_safe
from ..walker import IMAGE_SUFFIXES, JUNK_NAMES, natural_chunks
from .config import ReviewServerConfig

logger = logging.getLogger("alto_ocr.webreview.discovery")

# Preservation-scan folder suffixes, best first. Goobi's `{origpath}` is the
# master folder, which is what the bridge OCRs and therefore what the word
# boxes are in the coordinate frame of; `_media` is a derived display copy
# that may not even share the master's pixel dimensions.
_IMAGE_FOLDER_SUFFIXES = ("_master", "_orig", "_source", "_media", "_tif")


@dataclass(frozen=True)
class ReviewObject:
    """One reviewable object: its ALTO folder and the scans that go with it."""

    ref_id: str
    object_dir: Path            # holds ALTO/ and (usually) Text/
    image_dir: Path | None      # preservation scans; None = not reviewable
    process_id: str | None = None   # Goobi process id, when Goobi-managed

    @property
    def alto_dir(self) -> Path:
        return self.object_dir / "ALTO"

    @property
    def mets_path(self) -> Path:
        return self.object_dir / f"{self.ref_id}.mets.xml"

    @property
    def goobi_task_url_hint(self) -> str | None:
        """Goobi's own process page, for a link back out of the review UI.

        Relative, so it works whatever hostname Goobi is served on -- the
        review app is reached *from* Goobi, so the browser already knows.
        """
        if self.process_id is None:
            return None
        return f"/goobi/process_edit.xhtml?id={self.process_id}"


@dataclass
class PageState:
    """Per-page facts the page list shows. All derived from the ALTO."""

    stem: str
    status: str                  # failed | scored | unscored | empty
    pc: float | None
    words: int
    low_words: int
    reviewed: bool               # carries <Processing ID="review"> provenance
    has_image: bool

    @property
    def sort_key_natural(self) -> tuple:
        return natural_chunks(self.stem)

    @property
    def sort_key_worst_first(self) -> tuple:
        """Worst first: failures, then lowest page confidence. Reviewed pages
        sink below unreviewed ones at equal confidence -- a corrector should
        never be handed a page they already finished while others wait."""
        return (
            self.reviewed,
            STATUS_RANK.get(self.status, 4),
            self.pc if self.pc is not None else 2.0,
            natural_chunks(self.stem),
        )

    def to_json(self) -> dict:
        return {
            "stem": self.stem,
            "status": self.status,
            "pc": self.pc,
            "words": self.words,
            "lowWords": self.low_words,
            "reviewed": self.reviewed,
            "hasImage": self.has_image,
        }


@dataclass
class ObjectStats:
    """Aggregate over an object's pages -- the worklist's "how much is left"."""

    pages: int = 0
    reviewed_pages: int = 0
    failed_pages: int = 0
    words: int = 0
    low_words: int = 0
    missing_images: int = 0
    pc_sum: float = 0.0
    pc_count: int = 0

    @property
    def mean_pc(self) -> float | None:
        return self.pc_sum / self.pc_count if self.pc_count else None

    @property
    def pages_to_review(self) -> int:
        return max(0, self.pages - self.reviewed_pages)

    def to_json(self) -> dict:
        return {
            "pages": self.pages,
            "reviewedPages": self.reviewed_pages,
            "pagesToReview": self.pages_to_review,
            "failedPages": self.failed_pages,
            "words": self.words,
            "lowWords": self.low_words,
            "missingImages": self.missing_images,
            "meanPc": self.mean_pc,
        }


def _has_images(directory: Path) -> bool:
    """Whether a folder holds at least one page image, anywhere below it.

    Early-exits on the first hit: this runs over folders that may hold 900+
    TIFs on an SMB share, and the question is only "is this the empty one".
    """
    try:
        for dirpath, _dirnames, filenames in os.walk(directory):
            for name in filenames:
                if name.lower() in JUNK_NAMES:
                    continue
                if Path(name).suffix.lower() in IMAGE_SUFFIXES:
                    return True
    except OSError:
        return False
    return False


def _pick_image_dir(images_root: Path, ref_id: str) -> Path | None:
    """The preservation-scan folder for *ref_id* under a Goobi ``images/``.

    Goobi names it ``<processtitle>_master``, so exact matches are tried
    first in suffix order; the sweep after them covers hand-made process
    folders and older installs whose naming predates the convention.

    **A folder only wins if it actually contains images.** Goobi creates
    ``<title>_master`` whether or not anything was imported into it
    (``createOrigFolderIfNotExists`` defaults true), so on a process where
    the scans went to ``_media`` the empty master exists and would otherwise
    shadow them -- every page would report "no source scan" with a populated
    folder sitting right next to it. Observed on this install's process 7.
    An existing-but-empty folder is still the fallback answer, so the object
    is reported with its intended location rather than none at all.
    """
    if not images_root.is_dir():
        return None
    candidates: list[Path] = []
    for suffix in _IMAGE_FOLDER_SUFFIXES:
        candidate = images_root / f"{ref_id}{suffix}"
        if candidate.is_dir():
            candidates.append(candidate)
    subdirs = [d for d in sorted(images_root.iterdir()) if d.is_dir()]
    for suffix in _IMAGE_FOLDER_SUFFIXES:
        candidates.extend(d for d in subdirs
                          if d.name.endswith(suffix) and d not in candidates)
    for candidate in candidates:
        if _has_images(candidate):
            return candidate
    if candidates:
        return candidates[0]
    return images_root if _has_images(images_root) else None


def discover_goobi_objects(metadata_root: Path) -> list[ReviewObject]:
    """Objects under a Goobi metadata root.

    Goobi lays out ``<metadata_root>/<processid>/`` with ``images/`` and
    ``ocr/``; the bridge adds ``ocr/alto_ocr/<RefID>/`` as a symlink view onto
    Goobi's own ``<RefID>_alto`` / ``_txt`` folders. Reviewing through the
    view means this server edits the exact bytes Goobi serves -- there is no
    second copy to reconcile, which is the whole reason the view exists.
    """
    objects: list[ReviewObject] = []
    if not metadata_root.is_dir():
        logger.warning("Goobi metadata root %s does not exist", metadata_root)
        return objects
    for process_dir in sorted(metadata_root.iterdir(), key=lambda p: p.name):
        if not process_dir.is_dir():
            continue
        view_root = process_dir / "ocr" / "alto_ocr"
        if not view_root.is_dir():
            continue
        for object_dir in sorted(view_root.iterdir(), key=lambda p: p.name):
            # is_dir() follows the symlinks the bridge plants, which is what
            # we want: a dangling view (Goobi folder deleted) is not an object.
            if not object_dir.is_dir() or not (object_dir / "ALTO").is_dir():
                continue
            ref_id = object_dir.name
            objects.append(
                ReviewObject(
                    ref_id=ref_id,
                    object_dir=object_dir,
                    image_dir=_pick_image_dir(process_dir / "images", ref_id),
                    process_id=process_dir.name if process_dir.name.isdigit() else None,
                )
            )
    return objects


def discover_standalone_objects(output_root: Path, source_root: Path) -> list[ReviewObject]:
    """Objects in a plain pipeline output root, scans in a preservation root.

    The scans live in ``<source_root>/<RefID>/`` when the preservation tree
    is per-object (the walker's input contract); a source root that *is* one
    object's folder is accepted too, so a corrector can point the server at a
    single object exactly as the Tk Review tab lets them.
    """
    objects: list[ReviewObject] = []
    for object_dir in iter_object_dirs(output_root):
        ref_id = object_dir.name
        per_object = source_root / ref_id
        image_dir = per_object if per_object.is_dir() else (
            source_root if source_root.is_dir() else None)
        objects.append(ReviewObject(ref_id=ref_id, object_dir=object_dir, image_dir=image_dir))
    return objects


class ObjectIndex:
    """The set of reviewable objects, refreshed on a TTL.

    Refresh is by wall clock rather than a filesystem watch on purpose: the
    ALTO can live on an SMB share, where change notification is unreliable
    and a recursive re-walk on every request is not free. Twenty seconds is
    below a corrector's tolerance for "my new object is not in the list yet"
    and far above the cost of the walk.
    """

    def __init__(self, config: ReviewServerConfig, *, ttl_seconds: float = 20.0,
                 miss_floor_seconds: float = 2.0) -> None:
        self.config = config
        self.ttl_seconds = ttl_seconds
        self.miss_floor_seconds = miss_floor_seconds
        self._objects: dict[str, ReviewObject] = {}
        self._loaded_at = 0.0
        self._image_indexes: dict[str, tuple[float, dict[str, Path]]] = {}

    # ---------- objects ---------------------------------------------------

    def refresh(self) -> None:
        found: dict[str, ReviewObject] = {}
        if self.config.output_root is not None and self.config.source_root is not None:
            for obj in discover_standalone_objects(
                    self.config.output_root, self.config.source_root):
                found[obj.ref_id] = obj
        # Goobi-managed objects are indexed last so they win a Ref ID
        # collision: that is the copy the workflow is tracking, and a
        # correction made anywhere else would not reach it.
        if self.config.goobi_metadata_root is not None:
            for obj in discover_goobi_objects(self.config.goobi_metadata_root):
                found[obj.ref_id] = obj
        self._objects = found
        self._loaded_at = time.monotonic()

    def objects(self, *, force: bool = False) -> list[ReviewObject]:
        if force or not self._objects or (
                time.monotonic() - self._loaded_at) > self.ttl_seconds:
            self.refresh()
        return sorted(self._objects.values(), key=lambda o: natural_chunks(o.ref_id))

    def get(self, ref_id: str) -> ReviewObject | None:
        """Look up a Ref ID that came from a URL. Never joins it to a path.

        A miss re-walks the roots (the object may be new since the last
        refresh), but not more often than ``miss_floor_seconds``: a stale
        bookmark or a browser retrying a dead link would otherwise re-walk
        the whole Goobi metadata share on every request.
        """
        self.objects()
        obj = self._objects.get(ref_id)
        if obj is None and (
                time.monotonic() - self._loaded_at) >= self.miss_floor_seconds:
            self.refresh()          # newly created object, or a stale index
            obj = self._objects.get(ref_id)
        return obj

    # ---------- images ---------------------------------------------------

    def image_index(self, obj: ReviewObject, *, ttl_seconds: float = 60.0
                    ) -> dict[str, Path]:
        """``{stem: scan path}`` for an object, cached per object.

        Cached because the walk is recursive over a preservation folder that
        may hold 900+ TIFs on a network share -- once per page view is too
        often. Keyed by Ref ID, so re-imported scans appear within the TTL.
        """
        if obj.image_dir is None:
            return {}
        hit = self._image_indexes.get(obj.ref_id)
        now = time.monotonic()
        if hit is not None and (now - hit[0]) <= ttl_seconds:
            return hit[1]
        index = index_source_images(obj.image_dir)
        self._image_indexes[obj.ref_id] = (now, index)
        return index

    def image_for(self, obj: ReviewObject, stem: str) -> Path | None:
        """The scan for one page stem, or None. Index lookup, never a join."""
        return self.image_index(obj).get(stem)

    def alto_for(self, obj: ReviewObject, stem: str) -> Path | None:
        """The ALTO file for one page stem, resolved against the folder's own
        listing so a crafted stem cannot escape it."""
        for path in obj.alto_dir.glob("*.xml"):
            if path.stem == stem:
                return path
        return None


def scan_object(obj: ReviewObject, image_index: dict[str, Path], *,
                low_wc: float = DEFAULT_LOW_WC
                ) -> tuple[list[PageState], ObjectStats]:
    """Read every ALTO in an object and derive its page states and totals.

    Unreadable ALTO is reported as a ``failed`` page rather than raising: one
    corrupt file in a 900-page object must not make the other 899
    unreviewable, and ``failed`` is already the status that sorts first, so
    the page surfaces at the top of the queue where someone will look at it.
    """
    pages: list[PageState] = []
    stats = ObjectStats()
    for alto_path in sorted(obj.alto_dir.glob("*.xml")):
        stem = alto_path.stem
        has_image = stem in image_index
        conf = scan_alto_file_safe(alto_path, low_wc=low_wc)
        state = PageState(
            stem=stem, status=conf.status, pc=conf.pc, words=conf.words,
            low_words=conf.low_words,
            # False for an unreadable file, which scans as failed above.
            reviewed=has_review_provenance(alto_path), has_image=has_image,
        )
        pages.append(state)
        stats.pages += 1
        stats.words += state.words
        stats.low_words += state.low_words
        if state.reviewed:
            stats.reviewed_pages += 1
        if state.status == "failed":
            stats.failed_pages += 1
        if not has_image:
            stats.missing_images += 1
        if state.pc is not None:
            stats.pc_sum += state.pc
            stats.pc_count += 1
    return pages, stats


@dataclass
class StatsCache:
    """Per-object scan results, invalidated by the ALTO folder's own state.

    Scanning a 900-page object parses 900 XML files off a share; the worklist
    would be unusable if it did that for every object on every load. The
    cache key is (page count, newest mtime_ns, total bytes) over the ALTO
    folder, so a save through this server, the Tk GUI, or an external editor
    invalidates the entry.

    Total bytes is in the key because mtime alone is not enough: filesystem
    timestamp granularity is ~4 ms on the development host and coarser over
    SMB, so a rewrite can land inside the same tick as the read that cached
    it. Size moves for essentially every real correction -- different word
    text, a provenance element gained or its count grown.

    The residual gap is honest and bounded: a *same-size* edit made by
    another tool inside one mtime tick can leave a stale **count** in the
    worklist until the next change. It can never produce a stale page,
    because opening a page re-reads its ALTO, and it can never lose a
    correction, because the save path's token is a digest of the file's
    bytes. Saves through this server invalidate their object explicitly.

    Deliberately not persisted -- a cold start costs one scan and cannot
    disagree with the disk, which a stale cache file could.
    """

    low_wc: float = DEFAULT_LOW_WC
    _entries: dict[str, tuple[tuple[int, int, int], list[PageState], ObjectStats]] = field(
        default_factory=dict)

    @staticmethod
    def fingerprint(alto_dir: Path) -> tuple[int, int, int]:
        count = 0
        newest = 0
        total = 0
        try:
            for path in alto_dir.glob("*.xml"):
                count += 1
                try:
                    st = path.stat()
                except OSError:
                    continue
                newest = max(newest, st.st_mtime_ns)
                total += st.st_size
        except OSError:
            return (0, 0, 0)
        return (count, newest, total)

    def get(self, obj: ReviewObject, image_index: dict[str, Path],
            ) -> tuple[list[PageState], ObjectStats]:
        fp = self.fingerprint(obj.alto_dir)
        hit = self._entries.get(obj.ref_id)
        if hit is not None and hit[0] == fp:
            return hit[1], hit[2]
        pages, stats = scan_object(obj, image_index, low_wc=self.low_wc)
        self._entries[obj.ref_id] = (fp, pages, stats)
        return pages, stats

    def invalidate(self, ref_id: str) -> None:
        self._entries.pop(ref_id, None)


def write_worklist_json(path: Path, rows: list[dict]) -> None:
    """Dump a worklist snapshot -- for debugging a deployment without a browser."""
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
