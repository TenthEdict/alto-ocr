"""FastAPI wiring. Deliberately thin -- the logic is in the four modules
beside it and in ``remediate.py``.

Security posture (read this before deploying): **this server has no
authentication of its own and it writes to the archive's ALTO.** It is meant
to be reached the way Goobi is reached -- reverse-proxied behind the same
Apache, under the same LDAP access control -- and therefore binds to
127.0.0.1 by default. Binding it to a routable address without a
proxy in front hands anyone on the network write access to every transcript.
``--read-only`` serves the reviewing UI with the save path disabled, for a
deployment where that is the intent.

Nothing here is stateful except two caches (the object index and the page
scan), both of which are invalidated from the filesystem, so the process can
be restarted at any moment without a corrector losing saved work.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..remediate import (
    ConcurrentEditError,
    RepackageResult,
    ReviewPage,
    StubPageError,
    repackage_object,
)
from ..symbols import keyboard_payload
from . import imaging, ops
from .config import ReviewServerConfig
from .discovery import ObjectIndex, ReviewObject, StatsCache
from .imaging import WorkingCopy

logger = logging.getLogger("alto_ocr.webreview.app")

STATIC_DIR = Path(__file__).parent / "static"

# Render cache ceiling. Renders are pure functions of scans that still exist,
# so eviction only ever costs a re-decode; 8 GB holds a few thousand pages.
CACHE_MAX_BYTES = 8 * 1024 * 1024 * 1024


def create_app(config: ReviewServerConfig) -> FastAPI:
    app = FastAPI(
        title="ALTO-OCR transcript review",
        version=__version__,
        docs_url=None, redoc_url=None,   # no interactive docs on a write endpoint
    )
    index = ObjectIndex(config)
    stats = StatsCache(low_wc=config.low_wc)

    # ---------- helpers ------------------------------------------------

    def _object(ref_id: str) -> ReviewObject:
        obj = index.get(ref_id)
        if obj is None:
            raise HTTPException(404, f"No reviewable object named {ref_id!r}.")
        return obj

    def _alto(obj: ReviewObject, stem: str) -> Path:
        path = index.alto_for(obj, stem)
        if path is None:
            raise HTTPException(404, f"{obj.ref_id} has no ALTO page {stem!r}.")
        return path

    def _working_copy(obj: ReviewObject, stem: str) -> WorkingCopy:
        image = index.image_for(obj, stem)
        if image is None:
            raise HTTPException(
                404,
                f"No source scan for {stem!r} in {obj.image_dir}. The "
                "preservation scans are not in the output tree, so a page "
                "cannot be reviewed without them.",
            )
        try:
            wc = imaging.working_copy(image, config.cache_dir, max_px=config.working_px)
        except OSError as e:
            raise HTTPException(500, f"Could not read the scan {image.name}: {e}") from e
        imaging.prune_cache(config.cache_dir, max_bytes=CACHE_MAX_BYTES)
        return wc

    def _load_page(obj: ReviewObject, stem: str) -> tuple[ReviewPage, Path]:
        alto_path = _alto(obj, stem)
        wc = _working_copy(obj, stem)
        try:
            return ReviewPage(alto_path, wc.source_size), alto_path
        except StubPageError as e:
            # A failure stub or unparseable ALTO. 409, not 500: the server is
            # fine, this page needs re-OCR rather than correction, and the
            # corrector should be told which of those it is.
            raise HTTPException(409, str(e)) from e

    def _jpeg(payload: bytes) -> Response:
        # Renders are immutable for the life of their cache key (which
        # includes the scan's mtime), so they are safe to cache hard in the
        # browser -- and page images are the only large thing this UI moves.
        return Response(payload, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.middleware("http")
    async def _no_store_json(request: Request, call_next):
        """Mark JSON no-store, leave already-marked responses alone.

        A browser is free to reuse a GET with no cache headers, and a reused
        page payload carries a stale concurrency token -- the corrector would
        then be told their save conflicts with a version they are in fact
        looking at, or worse, post against bytes that have moved on. Image
        responses set their own long max-age (their cache key contains the
        scan's mtime, so they cannot go stale) and are left untouched: the
        scans are the only large thing this UI moves.
        """
        response = await call_next(request)
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---------- metadata ----------------------------------------------

    @app.get("/api/config")
    def api_config() -> dict:
        return {
            "version": __version__,
            "readOnly": config.read_only,
            "lowWc": config.low_wc,
            "goobiManaged": config.goobi_metadata_root is not None,
            # The symbol keyboard's layout: one definition for both review
            # UIs (symbols.py), so the keys cannot drift apart.
            "symbols": keyboard_payload(),
        }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "objects": len(index.objects())}

    # ---------- worklist ----------------------------------------------

    @app.get("/api/objects")
    def api_objects(refresh: bool = False) -> dict:
        rows = []
        for obj in index.objects(force=refresh):
            pages, totals = stats.get(obj, index.image_index(obj))
            rows.append({
                "refId": obj.ref_id,
                "processId": obj.process_id,
                "goobiUrl": obj.goobi_task_url_hint,
                "hasImages": obj.image_dir is not None,
                "objectDir": str(obj.object_dir),
                "imageDir": str(obj.image_dir) if obj.image_dir else None,
                "hasMets": obj.mets_path.is_file(),
                **totals.to_json(),
            })
        return {"objects": rows}

    @app.get("/api/objects/{ref_id}")
    def api_object(ref_id: str) -> dict:
        obj = _object(ref_id)
        pages, totals = stats.get(obj, index.image_index(obj))
        return {
            "refId": obj.ref_id,
            "processId": obj.process_id,
            "goobiUrl": obj.goobi_task_url_hint,
            "objectDir": str(obj.object_dir),
            "imageDir": str(obj.image_dir) if obj.image_dir else None,
            "hasMets": obj.mets_path.is_file(),
            "stats": totals.to_json(),
            "pages": [p.to_json() for p in pages],
        }

    # ---------- one page ----------------------------------------------

    @app.get("/api/objects/{ref_id}/pages/{stem}")
    def api_page(ref_id: str, stem: str) -> dict:
        obj = _object(ref_id)
        page, alto_path = _load_page(obj, stem)
        return ops.page_payload(page, stem=stem, alto_path=alto_path,
                                low_wc=config.low_wc)

    @app.post("/api/objects/{ref_id}/pages/{stem}")
    async def api_save(ref_id: str, stem: str, request: Request) -> dict:
        if config.read_only:
            raise HTTPException(403, "This review server is running read-only.")
        obj = _object(ref_id)
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, f"Body is not JSON: {e}") from e
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object.")

        force = bool(body.get("force"))
        try:
            operations = ops.parse_operations(body.get("ops"))
        except ops.OperationError as e:
            raise HTTPException(400, str(e)) from e

        alto_path = _alto(obj, stem)
        if not force:
            try:
                ops.check_token(alto_path, body.get("token"))
            except ops.OperationError as e:
                raise HTTPException(400, str(e)) from e
            except ConcurrentEditError as e:
                raise HTTPException(409, str(e)) from e

        page, alto_path = _load_page(obj, stem)
        try:
            report = ops.apply_operations(page, operations)
        except ops.OperationError as e:
            raise HTTPException(400, str(e)) from e
        except ValueError as e:
            # remediate's own argument guards (empty text, no-op add). The
            # tree is discarded unwritten, so the file on disk is untouched.
            raise HTTPException(400, str(e)) from e

        if not report.changed:
            return {"saved": False, "token": ops.load_token(alto_path),
                    "report": report.to_json()}

        try:
            txt_path = page.save(force=force)
        except ConcurrentEditError as e:
            raise HTTPException(409, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"Could not write {alto_path.name}: {e}") from e

        stats.invalidate(obj.ref_id)
        return {
            "saved": True,
            "token": ops.load_token(alto_path),
            "txt": str(txt_path) if txt_path else None,
            "report": report.to_json(),
            "page": ops.page_payload(page, stem=stem, alto_path=alto_path,
                                     low_wc=config.low_wc),
        }

    @app.post("/api/objects/{ref_id}/pages/{stem}/preview")
    async def api_preview(ref_id: str, stem: str, request: Request) -> dict:
        """Apply a batch to a throwaway load and report the reading order and
        transcript it produces. Writes nothing, so it is open in read-only
        mode too. The browser's reading-order view comes from here, so the
        order it shows is the order ``remediate.move_words`` will save."""
        obj = _object(ref_id)
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, f"Body is not JSON: {e}") from e
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object.")
        try:
            operations = ops.parse_operations(body.get("ops", []))
        except ops.OperationError as e:
            raise HTTPException(400, str(e)) from e
        page, _alto_path = _load_page(obj, stem)
        load_words = list(page.words)
        try:
            report = ops.apply_operations(page, operations)
        except ValueError as e:  # OperationError included
            raise HTTPException(400, str(e)) from e
        return {**ops.order_payload(page, load_words), "report": report.to_json()}

    # ---------- pixels -------------------------------------------------

    @app.get("/api/objects/{ref_id}/pages/{stem}/image")
    def api_image(ref_id: str, stem: str, w: int | None = None) -> Response:
        obj = _object(ref_id)
        wc = _working_copy(obj, stem)
        width = None
        if w is not None:
            if not 32 <= w <= config.working_px:
                raise HTTPException(400, f"w must be between 32 and {config.working_px}.")
            width = min(w, config.page_render_px)
        elif config.page_render_px < wc.working_size[0]:
            width = config.page_render_px
        return _jpeg(imaging.render_page(wc, width=width))

    @app.get("/api/objects/{ref_id}/pages/{stem}/crop")
    def api_crop(ref_id: str, stem: str, x: float, y: float, w: float, h: float,
                 zoom: float = 2.0, pad: int = 12) -> Response:
        obj = _object(ref_id)
        wc = _working_copy(obj, stem)
        if w <= 0 or h <= 0:
            raise HTTPException(400, "Crop width and height must be positive.")
        return _jpeg(imaging.render_crop(wc, (x, y, w, h),
                                         pad=max(0, min(pad, 200)),
                                         zoom=max(0.1, min(zoom, 8.0))))

    # ---------- repackage ----------------------------------------------

    @app.post("/api/objects/{ref_id}/repackage")
    def api_repackage(ref_id: str) -> dict:
        """Regenerate Text/ and the METS from the corrected ALTO.

        The same ``repackage_object`` the CLI and the Tk Review tab call, so
        a corrector never needs a terminal to finish an object -- and pages
        an *external* editor changed get their review provenance stamped
        here, which is the only place that gap is closed.
        """
        if config.read_only:
            raise HTTPException(403, "This review server is running read-only.")
        obj = _object(ref_id)
        try:
            result: RepackageResult = repackage_object(obj.object_dir)
        except OSError as e:
            raise HTTPException(500, f"Repackage failed for {ref_id}: {e}") from e
        stats.invalidate(obj.ref_id)
        return {
            "refId": ref_id,
            "pages": result.pages,
            "transcriptsUpdated": result.txt_written,
            "metsUpdated": result.mets_written,
            "externallyEdited": result.externally_edited,
            "problems": result.problems,
        }

    # ---------- static UI ----------------------------------------------

    @app.get("/")
    def root(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ConcurrentEditError)
    def _concurrent(_request: Request, exc: ConcurrentEditError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app
