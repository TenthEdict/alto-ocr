"""Where the review server finds ALTO, scans, and its render cache."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Goobi's default install root. Only a default -- every path is overridable,
# because the pipeline also runs standalone against a plain output root.
DEFAULT_GOOBI_METADATA_ROOT = Path("/opt/digiverso/goobi/metadata")

# Full-page render cap. Correctors read words from crops served at the
# cached image's own resolution, so the page view only has to be good
# enough to *find* a word and click it; 2400px covers a 100%-zoom laptop
# viewport with room to zoom in. Uncapped, a 177 MB oversize scan would
# hand the browser a ~14000px JPEG per page.
DEFAULT_PAGE_RENDER_PX = 2400

# Working-copy cap. One decode per page, cached, then every crop and every
# page render is resampled from this. 4000px keeps a 400 DPI letter scan
# very close to native for crop legibility without holding a 177 MB decode.
DEFAULT_WORKING_PX = 4000


@dataclass
class ReviewServerConfig:
    """Resolved server configuration.

    Two discovery modes, and they are not variants of one thing:

    ``goobi_metadata_root``
        Goobi-managed: every process directory under it is inspected for
        ``ocr/alto_ocr/<RefID>/ALTO`` (the symlink view the bridge builds)
        and its sibling ``images/<RefID>_master`` preservation scans. Ref
        IDs *are* Goobi process titles here, so a link from a Goobi task
        resolves without the task having to carry any path.

    ``output_root`` + ``source_root``
        Standalone: an ordinary pipeline output root plus the preservation
        folder its scans came from -- the same two folders the Tk Review tab
        asks a corrector to browse to. Useful before Goobi is in the loop,
        and for reviewing a re-run that never went through a workflow.

    Both may be set; objects found under the metadata root win a name
    collision, since that is the copy Goobi is tracking.
    """

    goobi_metadata_root: Path | None = None
    output_root: Path | None = None
    source_root: Path | None = None
    cache_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "alto_ocr_review_cache")
    low_wc: float = 0.50
    page_render_px: int = DEFAULT_PAGE_RENDER_PX
    working_px: int = DEFAULT_WORKING_PX
    read_only: bool = False

    def __post_init__(self) -> None:
        for name in ("goobi_metadata_root", "output_root", "source_root"):
            val = getattr(self, name)
            if val is not None:
                setattr(self, name, Path(val).expanduser())
        self.cache_dir = Path(self.cache_dir).expanduser()
        if self.goobi_metadata_root is None and self.output_root is None:
            raise ValueError(
                "Nothing to review: pass --goobi-metadata-root (Goobi-managed "
                "objects) or --output-root (a standalone pipeline output root)."
            )
        if self.output_root is not None and self.source_root is None:
            raise ValueError(
                "--output-root needs --source-root as well: the preservation "
                "scans are not in the output tree (see the output contract), "
                "and a word cannot be checked without the page image."
            )

    @classmethod
    def from_env(cls) -> ReviewServerConfig:
        """Build from ``ALTO_OCR_REVIEW_*`` environment variables.

        This is how the systemd unit configures the server: a unit file can
        carry ``Environment=`` lines, and keeping the knobs in the unit means
        the deployment is one file an operator can read, with no second
        config format to keep in sync with the CLI flags.
        """
        def _path(key: str) -> Path | None:
            raw = os.environ.get(key, "").strip()
            return Path(raw) if raw else None

        def _int(key: str, default: int) -> int:
            raw = os.environ.get(key, "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        kwargs: dict = {
            "goobi_metadata_root": _path("ALTO_OCR_REVIEW_GOOBI_ROOT"),
            "output_root": _path("ALTO_OCR_REVIEW_OUTPUT_ROOT"),
            "source_root": _path("ALTO_OCR_REVIEW_SOURCE_ROOT"),
            "page_render_px": _int("ALTO_OCR_REVIEW_PAGE_PX", DEFAULT_PAGE_RENDER_PX),
            "working_px": _int("ALTO_OCR_REVIEW_WORKING_PX", DEFAULT_WORKING_PX),
            "read_only": os.environ.get("ALTO_OCR_REVIEW_READ_ONLY", "").strip().lower()
            in ("1", "true", "yes"),
        }
        cache = _path("ALTO_OCR_REVIEW_CACHE_DIR")
        if cache is not None:
            kwargs["cache_dir"] = cache
        low = os.environ.get("ALTO_OCR_REVIEW_LOW_WC", "").strip()
        if low:
            try:
                kwargs["low_wc"] = float(low)
            except ValueError:
                pass
        if kwargs["goobi_metadata_root"] is None and kwargs["output_root"] is None:
            kwargs["goobi_metadata_root"] = DEFAULT_GOOBI_METADATA_ROOT
        return cls(**kwargs)
