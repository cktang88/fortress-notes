import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import (
    ask as ask_notes,
    assets,
    backups,
    block_query,
    block_store,
    embeddings,
    documents,
    duplicates,
    http_cache,
    llm,
    media,
    ocr,
    onenote_import,
    search,
    sources,
    workspace,
)
from .portability import (
    export_all_markdown,
    export_document_markdown,
    parse_uploaded_markdown,
    safe_filename,
    import_markdown_directory,
    import_markdown_if_empty,
    preview_markdown_directory,
)
from .block_types import BlockShapeError
from .config import get_settings
from . import link_checks
from .models import (
    Note,
    NoteCreate,
    NoteStatus,
    NoteSummary,
    NoteUpdate,
    BlockTransaction,
    BlockHistoryRequest,
    BlockSearchResult,
    BlockLinkTarget,
    Backlink,
    ReviewKind,
    ReviewResponse,
    BlockReviewResponse,
    BlockReviewContextRequest,
    BlockReviewContextResponse,
    SearchResult,
    RelatedResult,
    DocumentMove,
    DocumentOrganization,
    Folder,
    FolderCreate,
    FolderMove,
    FolderRename,
    AskRequest,
    CaptureRequest,
    SavedSearchCreate,
    SourceConflictResolution,
)

settings = get_settings()

# Hold references to fire-and-forget tasks so they aren't garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():  # e.g. shutdown: t.exception() would raise CancelledError
            return
        exc = t.exception()
        if exc:
            logging.getLogger("fortress").exception("background task failed", exc_info=exc)

    task.add_done_callback(_done)


async def _reindex_loop() -> None:
    """Keep embeddings current so searches stay instant.

    Starts right away: the first pass loads the model and the saved embeddings
    (and, on a first run, encodes everything in the background).
    """
    while True:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(embeddings.warm_index)
        await asyncio.sleep(settings.reindex_interval_s)


async def _backup_loop() -> None:
    """Keep one automatic snapshot per day; checking hourly is cheap."""
    while True:
        try:
            await asyncio.to_thread(
                backups.ensure_daily_backup,
                settings.block_db_path,
                backups.backup_directory(settings.notes_path),
            )
        except Exception:
            logging.getLogger("fortress").exception("automatic backup failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.block_db_import_on_startup:
        await asyncio.to_thread(
            import_markdown_if_empty, settings.block_db_path, settings.notes_path
        )
    else:
        await asyncio.to_thread(block_store.initialize, settings.block_db_path)
    await asyncio.to_thread(
        block_store.backfill_assets, settings.block_db_path, settings.assets_path
    )
    await asyncio.to_thread(block_store.ensure_fts_integrity, settings.block_db_path)
    backup_task = (
        asyncio.create_task(_backup_loop())
        if getattr(settings, "auto_backup_enabled", True)
        else None
    )
    task = (
        asyncio.create_task(_reindex_loop()) if settings.embeddings_enabled else None
    )
    # Reads text in pictures and PDFs in the background, starting with whatever an
    # earlier run left unread.
    if settings.vision_enabled:
        ocr.start(settings.block_db_path)
        onenote_import.jobs.ocr = ocr.wake
    try:
        yield
    finally:
        await asyncio.to_thread(ocr.stop)
        for running in (task, backup_task):
            if running:
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running
        await asyncio.to_thread(documents.flush_mirrors)


app = FastAPI(title="Fortress Notes", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResponseHeadersMiddleware:
    """Headers every response of a kind needs (plain ASGI: no body buffering).

    API responses default to ``Cache-Control: no-cache`` (keep, but revalidate;
    GETs carry ETags). Stored files are never sniffed, ``.bin`` downloads as an
    attachment and imported SVG ink never runs scripts when opened directly.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                if path.startswith("/api/"):
                    if "cache-control" not in headers:
                        headers["Cache-Control"] = http_cache.API_CACHE_CONTROL
                elif path.startswith("/media/"):
                    headers["X-Content-Type-Options"] = "nosniff"
                    if path.endswith(".bin"):
                        headers["Content-Disposition"] = "attachment"
                    if path.endswith(".svg"):
                        headers["Content-Security-Policy"] = (
                            "sandbox; default-src 'none'; style-src 'unsafe-inline'"
                        )
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(ResponseHeadersMiddleware)
# gzip API JSON of 1 KB or more (not the Ask stream, not media).
app.add_middleware(http_cache.ApiCompressionMiddleware, minimum_size=1024)


@app.exception_handler(sqlite3.OperationalError)
async def database_busy(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    """A write that waited out the busy timeout: say so in the usual JSON shape."""

    message = str(exc)
    if "locked" in message or "busy" in message:
        return JSONResponse(
            {"detail": "The notes database is busy; try again."},
            status_code=503,
            headers={"Retry-After": "1"},
        )
    logging.getLogger("fortress").error("database error", exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    # Errors keep the API's JSON shape ({"detail": ...}) instead of plain text.
    logging.getLogger("fortress").error("unhandled error", exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


# Serve stored files at /media/<file> (immutable caching, ranges, real types).
app.mount("/media", media.MediaFiles(directory=settings.assets_path), name="media")

# Rendered bodies by validator: a repeated GET skips SQLite and JSON encoding.
_document_cache = http_cache.ResponseCache(max_entries=256, max_bytes=64 * 1024 * 1024)
_api_cache = http_cache.ResponseCache(max_entries=256, max_bytes=32 * 1024 * 1024)


def _clear_response_caches() -> None:
    _document_cache.clear()
    _api_cache.clear()


def _document_body(document_id: str, validator: tuple | None = None) -> tuple[str, bytes] | None:
    """(ETag, rendered tree) of a note, rendered at most once per validator."""

    db = settings.block_db_path
    key = (db, document_id)
    if validator is not None:
        tag = http_cache.etag("document", document_id, *validator)
        body = _document_cache.get(key, tag)
        if body is not None:
            return tag, body
    result = block_store.document_tree_with_validator(db, document_id)
    if result is None:
        return None
    validator, tree = result
    tag = http_cache.etag("document", document_id, *validator)
    body = http_cache.render(tree)
    _document_cache.put(key, tag, body)
    return tag, body


def _document_response(request: Request, document_id: str) -> Response:
    validator = block_store.document_validator(settings.block_db_path, document_id)
    if validator is None:
        raise HTTPException(404, "Block document not found")
    tag = http_cache.etag("document", document_id, *validator)
    if http_cache.if_none_match(request.headers.get("if-none-match"), tag):
        return http_cache.not_modified(tag)
    rendered = _document_body(document_id, validator)
    if rendered is None:
        raise HTTPException(404, "Block document not found")
    return http_cache.json_response(rendered[1], rendered[0])


def _tree_response(
    document_id: str, tree: dict, validator: tuple | None = None, cache: bool = True
) -> Response:
    """A tree a write just returned, rendered once (no jsonable_encoder pass).

    With its validator (read in the write's own transaction) the body is
    cached for the GETs that follow, and ``X-Document-ETag`` carries the ETag
    ``GET /api/block-documents/{id}`` now answers 304 to.
    """

    body = http_cache.render(tree)
    response = http_cache.json_response(body)
    if validator is not None:
        tag = http_cache.etag("document", document_id, *validator)
        if cache:
            _document_cache.put((settings.block_db_path, document_id), tag, body)
        response.headers["X-Document-ETag"] = tag
    return response


def _workspace_response(request: Request, name: str, build, *key_parts) -> Response:
    """A response that depends on the whole workspace (navigation, trash, ...)."""

    epoch, seq = block_store.change_state(settings.block_db_path)
    tag = http_cache.etag(name, epoch, seq, *key_parts)
    return http_cache.conditional(
        request.headers.get("if-none-match"),
        tag,
        lambda: http_cache.render(build()),
        _api_cache,
        (settings.block_db_path, name, *key_parts),
    )

# Batch reads and moves for the native app (app/batch.py).
from .batch import router as batch_router  # noqa: E402

app.include_router(batch_router)

@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "embeddings_enabled": settings.embeddings_enabled,
        "model_loaded": embeddings.model_loaded(),
        "vision_enabled": settings.vision_enabled,
        "vision_loaded": ocr.loaded(),
        "block_db_ready": block_store.is_ready(settings.block_db_path),
    }


@app.get("/api/ocr")
def ocr_status() -> dict:
    """Background reading of pictures and PDFs: model, queued/done/failed counts."""

    return {"enabled": settings.vision_enabled, **ocr.status_counts(settings.block_db_path)}


@app.get("/api/navigation")
def get_navigation(request: Request, recent_limit: int = Query(10, ge=1, le=50)):
    return _workspace_response(
        request,
        "navigation",
        lambda: block_store.navigation(settings.block_db_path, recent_limit),
        recent_limit,
    )


@app.post("/api/folders", response_model=Folder, status_code=201)
def create_folder(data: FolderCreate):
    try:
        return block_store.create_folder(
            settings.block_db_path, data.name, data.parent_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/folders/{folder_id}", response_model=Folder)
def rename_folder(folder_id: str, data: FolderRename):
    try:
        return block_store.rename_folder(settings.block_db_path, folder_id, data.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/folders/{folder_id}/move", response_model=Folder)
def move_folder(folder_id: str, data: FolderMove):
    try:
        parent_id = (
            data.parent_id
            if "parent_id" in data.model_fields_set
            else block_store.UNSET
        )
        return block_store.move_folder(
            settings.block_db_path, folder_id, parent_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: str):
    try:
        block_store.delete_folder(settings.block_db_path, folder_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@app.post(
    "/api/block-documents/{document_id}/move", response_model=DocumentOrganization
)
def move_block_document(document_id: str, data: DocumentMove):
    try:
        folder_id = (
            data.folder_id
            if "folder_id" in data.model_fields_set
            else block_store.UNSET
        )
        return block_store.move_document(
            settings.block_db_path, document_id, folder_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/block-documents")
def list_block_documents():
    return block_store.list_documents(settings.block_db_path)


@app.post("/api/block-documents", status_code=201)
def create_block_document(data: NoteCreate):
    try:
        note = documents.create(data)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return block_store.document_tree(settings.block_db_path, note.id)


@app.patch("/api/block-documents/{document_id}")
def update_block_document(document_id: str, data: NoteUpdate):
    """Change a document's title and/or status (the body changes through transactions)."""

    if data.body is not None:
        raise HTTPException(422, "edit the body with block transactions")
    try:
        documents.update(document_id, data)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return block_store.document_tree(settings.block_db_path, document_id)


@app.delete("/api/block-documents/{document_id}", status_code=204)
def delete_block_document(document_id: str):
    try:
        documents.trash(document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/block-documents/{document_id}")
def get_block_document(document_id: str, request: Request):
    """The note's block tree; ``If-None-Match`` with its ETag answers 304."""

    db = settings.block_db_path
    # The editor needs a block to type into, even in a note that has none.
    block_store.ensure_editable_block(db, document_id)
    # Debounced, and skipped rather than waited for while a writer is busy.
    block_store.mark_opened(db, document_id, wait=False)
    return _document_response(request, document_id)


@app.get("/api/block-documents/{document_id}/info")
def get_block_document_info(document_id: str, request: Request):
    """Metadata for an info panel: dates, provenance, counts, re-import conflicts."""

    validator = block_store.document_info_validator(settings.block_db_path, document_id)
    if validator is None:
        raise HTTPException(404, "Block document not found")

    def build() -> bytes:
        try:
            return http_cache.render(sources.document_info(settings.block_db_path, document_id))
        except KeyError as exc:
            raise HTTPException(404, "Block document not found") from exc

    return http_cache.conditional(
        request.headers.get("if-none-match"),
        http_cache.etag("info", document_id, *validator),
        build,
        _api_cache,
        (settings.block_db_path, "info", document_id),
    )


@app.post("/api/block-documents/{document_id}/blocks/{block_id}/source-conflict")
def resolve_block_source_conflict(
    document_id: str, block_id: str, data: SourceConflictResolution
):
    """Settle a re-import conflict: take the source's version or keep the local edit."""

    try:
        tree = sources.resolve_block_conflict(
            settings.block_db_path, document_id, block_id, data.keep
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    documents.write_mirror(document_id)
    return tree


@app.get("/api/block-documents/{document_id}/subtree")
def get_block_subtree(document_id: str, block_id: str | None = None):
    try:
        subtree = block_store.document_subtree(
            settings.block_db_path, document_id, block_id
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if subtree is None:
        raise HTTPException(404, "Block document not found")
    return subtree


@app.get("/api/block-documents/{document_id}/markdown")
def export_block_document_markdown(document_id: str, download: bool = False):
    try:
        markdown = export_document_markdown(settings.block_db_path, document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    headers = {}
    if download:
        tree = block_store.document_tree(settings.block_db_path, document_id)
        title = safe_filename(str(tree["title"])) if tree else document_id
        headers["Content-Disposition"] = _attachment(f"{title}.md")
    return Response(content=markdown, media_type="text/markdown", headers=headers)


MAX_IMPORT_BYTES = 5 * 1024 * 1024


@app.post("/api/markdown-import/files")
async def import_uploaded_markdown(
    files: list[UploadFile] = File(...), folder_id: str | None = Form(None)
):
    """Create one new note per uploaded Markdown file."""

    folder_id = folder_id or None
    if folder_id is not None and not await run_in_threadpool(
        block_store.folder_exists, settings.block_db_path, folder_id
    ):
        raise HTTPException(404, "folder not found")
    imported: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    def create_note(name: str, raw: bytes):
        parsed = parse_uploaded_markdown(name, raw)
        return documents.create(
            NoteCreate(
                title=parsed.title,
                body=parsed.body,
                status=parsed.status,
                folder_id=folder_id,
            ),
            created_at=parsed.created_at,
            source=parsed.provenance,
        )

    for upload in files:
        name = upload.filename or "untitled.md"
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            errors.append({"name": name, "detail": "file is larger than 5 MB"})
            continue
        try:
            note = await run_in_threadpool(create_note, name, raw)
        except ValueError as exc:
            errors.append({"name": name, "detail": str(exc)})
            continue
        imported.append({"name": name, "id": note.id, "title": note.title})
    return {"imported": imported, "errors": errors}


# --- OneNote import (jobs) ------------------------------------------------------

MAX_ONENOTE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_ONENOTE_UPLOAD_SUFFIXES = (".zip", ".onepkg", ".one", ".onetoc2")


@app.post("/api/import/onenote", status_code=202)
async def start_onenote_import(request: Request):
    """Start importing a OneNote export: multipart ``file`` or JSON ``{"path"}``."""

    content_type = request.headers.get("content-type", "")
    job_id = onenote_import.jobs.new_id()
    owned: Path | None = None
    if content_type.startswith("multipart/form-data"):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_ONENOTE_UPLOAD_BYTES + 1024 * 1024:
            raise HTTPException(413, "The file is larger than 2 GB.")
        async with request.form() as form:
            upload = form.get("file")
            folder_id = str(form.get("folder_id") or "") or None
            if upload is None or isinstance(upload, str):
                raise HTTPException(400, "Send the OneNote export as the multipart field 'file'.")
            input_name = assets.safe_filename(upload.filename or "export.zip")
            suffix = Path(input_name).suffix.lower()
            if suffix not in _ONENOTE_UPLOAD_SUFFIXES:
                raise HTTPException(415, "Choose a OneNote export: a .zip, .onepkg or .one file.")
            await run_in_threadpool(_check_import_folder, folder_id)
            owned = onenote_import.imports_dir(settings.notes_path) / f"{job_id}-upload"
            # The converter names things after the file (a loose .one's section) and
            # its folder (that section's notebook): keep the user's file name.
            (owned / "OneNote").mkdir(parents=True, exist_ok=True)
            input_path = owned / "OneNote" / input_name
            size = 0
            try:
                with input_path.open("wb") as handle:
                    while chunk := await upload.read(_UPLOAD_CHUNK):
                        size += len(chunk)
                        if size > MAX_ONENOTE_UPLOAD_BYTES:
                            raise HTTPException(413, "The file is larger than 2 GB.")
                        await asyncio.to_thread(handle.write, chunk)
            except BaseException:
                shutil.rmtree(owned, ignore_errors=True)
                raise
    else:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Send multipart 'file' or JSON {\"path\": ...}.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("path"), str) or not payload["path"].strip():
            raise HTTPException(400, "Send multipart 'file' or JSON {\"path\": ...}.")
        folder_id = payload.get("folder_id") or None
        if folder_id is not None and not isinstance(folder_id, str):
            raise HTTPException(400, "folder_id must be a string")
        input_path = Path(payload["path"]).expanduser()
        if not input_path.exists():
            raise HTTPException(400, "That file or folder does not exist.")
        await run_in_threadpool(_check_import_folder, folder_id)
        input_name = input_path.name
    record = await run_in_threadpool(
        onenote_import.jobs.submit,
        job_id,
        input_path=input_path,
        input_name=input_name,
        owned_input=owned,
        db_path=settings.block_db_path,
        notes_path=settings.notes_path,
        assets_path=settings.assets_path,
        folder_id=folder_id,
    )
    return {"job_id": record["job_id"], "status": record["status"]}


def _check_import_folder(folder_id: str | None) -> None:
    if folder_id is not None and not block_store.folder_exists(settings.block_db_path, folder_id):
        raise HTTPException(404, "folder not found")


@app.get("/api/import/onenote")
def list_onenote_imports(limit: int = Query(20, ge=1, le=100)):
    return onenote_import.jobs.list(settings.notes_path, limit)


@app.get("/api/import/onenote/{job_id}")
def get_onenote_import(job_id: str):
    record = onenote_import.jobs.get(job_id, settings.notes_path)
    if record is None:
        raise HTTPException(404, "import job not found")
    return record


@app.post("/api/import/onenote/{job_id}/cancel", status_code=202)
def cancel_onenote_import(job_id: str):
    """Stop an import between pages; imported pages are kept (status ``cancelled``)."""

    record, accepted = onenote_import.jobs.cancel(job_id, settings.notes_path)
    if record is None:
        raise HTTPException(404, "import job not found")
    if not accepted:
        raise HTTPException(409, f"The import already ended ({record.get('status')}).")
    return record


def _attachment(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@app.post("/api/capture", status_code=201)
def capture(data: CaptureRequest):
    """Save a quick thought as its own note in the Uncategorized folder."""

    try:
        return documents.capture(data.text, data.provenance)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/block-documents/{document_id}/similar")
def similar_documents(document_id: str):
    """Other notes containing most of this note's distinctive words."""

    try:
        return duplicates.similar_documents(settings.block_db_path, document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/block-documents/{document_id}/move-into/{target_id}")
def move_document_into(document_id: str, target_id: str):
    """Append this note's paragraphs to another note and trash the emptied one."""

    try:
        moved = documents.move_into(document_id, target_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"moved": moved, "document_id": target_id}


# --- Trash ---------------------------------------------------------------------


@app.get("/api/trash")
def list_trash(request: Request):
    return _workspace_response(
        request, "trash", lambda: workspace.list_trash(settings.block_db_path)
    )


@app.post("/api/trash/{document_id}/restore")
def restore_from_trash(document_id: str):
    try:
        return documents.restore(document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/trash/{document_id}", status_code=204)
def delete_forever(document_id: str):
    try:
        documents.purge(document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/trash")
def empty_trash():
    return {"deleted": documents.empty_trash()}


# --- Saved searches ------------------------------------------------------------


@app.get("/api/saved-searches")
def list_saved_searches():
    return workspace.list_saved_searches(settings.block_db_path)


@app.post("/api/saved-searches", status_code=201)
def create_saved_search(data: SavedSearchCreate):
    try:
        return workspace.create_saved_search(
            settings.block_db_path, data.name, data.query, data.mode, data.filters
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/saved-searches/{search_id}", status_code=204)
def delete_saved_search(search_id: str):
    try:
        workspace.delete_saved_search(settings.block_db_path, search_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


# --- Backups -------------------------------------------------------------------


@app.get("/api/backups")
def list_backups(request: Request):
    body = http_cache.render(backups.list_backups(backups.backup_directory(settings.notes_path)))
    return http_cache.conditional(
        request.headers.get("if-none-match"), http_cache.body_etag(body), lambda: body
    )


@app.post("/api/backups", status_code=201)
def create_backup():
    directory = backups.backup_directory(settings.notes_path)
    path = backups.create_backup(settings.block_db_path, directory, "manual")
    return next(item for item in backups.list_backups(directory) if item["name"] == path.name)


@app.get("/api/backups/{name}/download")
def download_backup(name: str):
    try:
        path = backups.resolve_backup(backups.backup_directory(settings.notes_path), name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    directory = backups.backup_directory(settings.notes_path)
    try:
        safety = backups.restore_backup(settings.block_db_path, directory, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _clear_response_caches()
    documents.rebuild_mirrors(directory / f"mirrors-before-{safety.stem}")
    return {"restored": name, "safety_backup": safety.name}


@app.get("/api/markdown-export")
def export_all_block_markdown():
    return export_all_markdown(settings.block_db_path)


@app.get("/api/markdown-import/preview")
def preview_workspace_markdown_import():
    preview = preview_markdown_directory(settings.block_db_path, settings.notes_path)
    return {
        "items": [
            {
                "source_path": item.source_path.name,
                "document_id": item.document_id,
                "status": item.status,
                "detail": item.detail,
            }
            for item in preview.items
        ],
        "ready": preview.ready_count,
        "skipped": preview.skipped_count,
        "conflicts": preview.conflict_count,
        "errors": preview.error_count,
    }


@app.post("/api/markdown-import")
def import_workspace_markdown():
    report = import_markdown_directory(settings.block_db_path, settings.notes_path)
    return {
        "backup_directory": f".fortress-import-backups/{report.backup_directory.name}",
        "items": [
            {
                "source_path": item.source_path.name,
                "document_id": item.document_id,
                "status": item.status,
                "detail": item.detail,
            }
            for item in report.items
        ],
        "imported": report.imported_count,
        "skipped": report.skipped_count,
        "errors": report.error_count,
    }


@app.get("/api/block-search", response_model=list[BlockSearchResult])
def search_blocks(
    q: str,
    limit: int = Query(50, ge=1, le=200),
    document_id: str | None = None,
    block_type: str | None = None,
    status: NoteStatus | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
):
    return block_query.search_blocks(
        settings.block_db_path,
        q,
        block_query.BlockSearchFilters(
            document_id=document_id,
            block_type=block_type,
            status=status,
            updated_after=updated_after,
            updated_before=updated_before,
        ),
        limit=limit,
    )


@app.get("/api/unified-search")
async def unified_search(
    q: str,
    limit: int = Query(30, ge=1, le=100),
    document_id: str | None = None,
    block_type: str | None = None,
    status: NoteStatus | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
):
    """Blocks matching by exact words, meaning, or note title, in one ranked list."""

    # Someone typing a search may press Ask next: have the model connection ready.
    _spawn(llm.preconnect())
    return await asyncio.to_thread(
        search.unified_search,
        q,
        block_query.BlockSearchFilters(
            document_id=document_id,
            block_type=block_type,
            status=status,
            updated_after=updated_after,
            updated_before=updated_before,
        ),
        limit=limit,
    )


@app.post("/api/ask")
async def ask(data: AskRequest, request: Request):
    """Answer a question from the notes, citing the paragraph behind each sentence.

    With ``Accept: application/x-ndjson`` the answer streams as one JSON event per
    line: ``sentence`` (each checked sentence as soon as it is complete), ``step``
    (an extra grep or read), and finally ``done`` with the full result.
    """

    if "application/x-ndjson" not in request.headers.get("accept", ""):
        return await ask_notes.ask(data.question)

    async def lines():
        async for event in ask_notes.ask_stream(data.question):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/block-search/rebuild")
def rebuild_block_search_index():
    block_store.rebuild_fts(settings.block_db_path)
    return {"ok": block_store.fts_is_consistent(settings.block_db_path)}


@app.get("/api/block-link-targets", response_model=list[BlockLinkTarget])
def block_link_targets(q: str = "", limit: int = Query(50, ge=1, le=200)):
    return block_store.block_link_targets(settings.block_db_path, q, limit)


@app.get("/api/block-documents/{document_id}/backlinks", response_model=list[Backlink])
def get_backlinks(document_id: str, request: Request, limit: int = Query(100, ge=1, le=500)):
    # (This used to build the note's whole tree just to answer 404.)
    validator = block_store.document_info_validator(settings.block_db_path, document_id)
    if validator is None:
        raise HTTPException(404, "Block document not found")
    return http_cache.conditional(
        request.headers.get("if-none-match"),
        http_cache.etag("backlinks", document_id, limit, *validator[:2]),
        lambda: http_cache.render(
            block_store.document_backlinks(settings.block_db_path, document_id, limit)
        ),
        _api_cache,
        (settings.block_db_path, "backlinks", document_id, limit),
    )


@app.post("/api/block-documents/{document_id}/link-checks")
async def check_block_document_links(document_id: str):
    try:
        return await link_checks.check_document_links(
            settings.block_db_path, document_id
        )
    except KeyError as exc:
        raise HTTPException(404, "Block document not found") from exc


@app.post("/api/block-documents/{document_id}/transactions")
def apply_block_transaction(
    document_id: str,
    transaction: BlockTransaction,
    response: str = Query("document", pattern="^(document|meta)$"),
):
    """Apply a transaction. ``?response=meta`` answers without ``children``."""

    try:
        validator: list = []
        tree = block_store.apply_transaction(
            settings.block_db_path,
            document_id,
            transaction.operations,
            transaction.base_revision,
            transaction.transaction_id,
            validator_out=validator,
            include_children=response == "document",
        )
        documents.schedule_mirror(document_id)
        return _tree_response(
            document_id, tree, validator[0] if validator else None, cache=response == "document"
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except BlockShapeError as exc:
        # A table/image/... whose content is not a documented shape (spec §2a).
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/block-documents/{document_id}/undo")
def undo_block_transaction(document_id: str, request: BlockHistoryRequest):
    try:
        tree = block_store.undo_transaction(
            settings.block_db_path, document_id, request.base_revision
        )
        documents.schedule_mirror(document_id)
        return _tree_response(document_id, tree)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/block-documents/{document_id}/redo")
def redo_block_transaction(document_id: str, request: BlockHistoryRequest):
    try:
        tree = block_store.redo_transaction(
            settings.block_db_path, document_id, request.base_revision
        )
        documents.schedule_mirror(document_id)
        return _tree_response(document_id, tree)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024


async def _read_upload(file: UploadFile, limit: int | None = None) -> bytes:
    """Read an upload in chunks, refusing it (413) as soon as it passes the limit."""

    limit = MAX_UPLOAD_BYTES if limit is None else limit
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(_UPLOAD_CHUNK):
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                413, f"File is larger than {limit // (1024 * 1024)} MB; choose a smaller one."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _stream_upload(file: UploadFile, path: Path, limit: int | None = None) -> str:
    """Copy an upload to ``path`` in chunks (never whole in memory); its SHA-256.

    Refuses it (413) as soon as it passes the limit; nothing is left behind.
    File writes run in worker threads, not on the event loop.
    """

    limit = MAX_UPLOAD_BYTES if limit is None else limit
    partial = path.with_name(f".{path.name}.part")
    digest = hashlib.sha256()
    size = 0
    handle = await run_in_threadpool(partial.open, "wb")
    try:
        try:
            while chunk := await file.read(_UPLOAD_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413,
                        f"File is larger than {limit // (1024 * 1024)} MB; choose a smaller one.",
                    )
                digest.update(chunk)
                await run_in_threadpool(handle.write, chunk)
        finally:
            await run_in_threadpool(handle.close)
        await run_in_threadpool(os.replace, partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...)):
    if not assets.is_supported_image_type(file.content_type):
        raise HTTPException(400, "Unsupported image type")
    media_type = (file.content_type or "image/png").split(";", 1)[0].strip().lower()
    path = assets.upload_path(media_type)
    digest = await _stream_upload(file, path)
    # Return at once so the image appears instantly; its text is read in the background.
    await run_in_threadpool(
        block_store.register_asset, settings.block_db_path, path, digest, media_type
    )
    ocr.wake()
    return {"url": f"/media/{path.name}", "text": ""}


@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if media_type.startswith("image/") and not assets.is_supported_image_type(media_type):
        raise HTTPException(415, "Unsupported image type")
    path = assets.upload_path(media_type)
    digest = await _stream_upload(file, path)
    name = assets.safe_filename(file.filename)
    url = f"/media/{path.name}"
    await run_in_threadpool(
        block_store.register_asset, settings.block_db_path, path, digest,
        media_type or "application/octet-stream",
    )
    ocr.wake()
    # BlockNote's uploadFile result is a partial block; its file insertion
    # handler passes this object directly to updateBlock.
    return {"props": {"url": url, "name": name}}


@app.get("/api/notes", response_model=list[NoteSummary])
def list_notes(status: NoteStatus | None = None):
    return documents.list_notes(status)


@app.post("/api/notes", response_model=Note, status_code=201)
def create_note(data: NoteCreate):
    try:
        return documents.create(data)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/notes/{note_id}", response_model=Note)
def get_note(note_id: str):
    note = documents.get_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    return note


@app.put("/api/notes/{note_id}", response_model=Note)
def update_note(note_id: str, data: NoteUpdate):
    try:
        return documents.update(note_id, data)
    except KeyError as exc:
        raise HTTPException(404, "Note not found") from exc


@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: str):
    try:
        documents.trash(note_id)
    except KeyError as exc:
        raise HTTPException(404, "Note not found") from exc
    return Response(status_code=204)


@app.post("/api/notes/{note_id}/promote", response_model=Note)
def promote_note(note_id: str):
    try:
        return documents.update(note_id, NoteUpdate(status="polished"))
    except KeyError as exc:
        raise HTTPException(404, "Note not found") from exc


@app.get("/api/search", response_model=list[SearchResult])
def search_notes(q: str, mode: str = Query("text", pattern="^(text|embedding)$")):
    if mode == "embedding":
        return search.embedding_search(q)
    return search.full_text_search(q)


@app.get("/api/notes/{note_id}/related", response_model=list[RelatedResult])
def related(note_id: str, k: int = Query(5, ge=1, le=50), block_id: str | None = None):
    if not block_store.document_exists(settings.block_db_path, note_id):
        raise HTTPException(404, "Note not found")
    try:
        return search.related_notes(note_id, k, block_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/notes/{note_id}/review", response_model=ReviewResponse)
async def review(note_id: str, kind: ReviewKind = "factcheck"):
    note = await run_in_threadpool(documents.get_note, note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    return await llm.review_note(kind, note.title, note.body)


@app.post(
    "/api/block-documents/{document_id}/blocks/{block_id}/review",
    response_model=BlockReviewResponse,
)
async def review_block(document_id: str, block_id: str, kind: ReviewKind = "factcheck"):
    try:
        result = await run_in_threadpool(
            block_store.document_subtree, settings.block_db_path, document_id, block_id
        )
    except KeyError as exc:
        raise HTTPException(404, "Block not found") from exc
    if result is None:
        raise HTTPException(404, "Block document not found")
    block = result["subtree"]
    return await llm.review_block(kind, block_id, block["text"], block["content"])


@app.post(
    "/api/block-documents/{document_id}/review-context",
    response_model=BlockReviewContextResponse,
)
async def review_block_context(document_id: str, request: BlockReviewContextRequest):
    try:
        resolved = await run_in_threadpool(
            block_store.document_review_context,
            settings.block_db_path, document_id, request.context, request.block_ids,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if resolved is None:
        raise HTTPException(404, "Block document not found")
    return await llm.review_blocks(
        request.kind,
        request.context,
        resolved["targets"],
        resolved["linked_context"],
    )
