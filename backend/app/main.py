import asyncio
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
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import (
    ask as ask_notes,
    assets,
    background,
    backups,
    block_query,
    block_store,
    embeddings,
    documents,
    http_cache,
    llm,
    media,
    ocr,
    onenote_graph,
    onenote_import,
    search,
    vault,
)
from .portability import (
    export_document_markdown,
    UploadedNote,
    parse_uploaded_batch,
    parse_uploaded_markdown,
    seed_welcome_note,
)
from .block_schema import BlockShapeError
from .config import get_settings
from .models import (
    NoteCreate,
    NoteUpdate,
    BlockTransaction,
    BlockLinkTarget,
    Backlink,
    RelatedResult,
    DocumentMove,
    DocumentOrganization,
    Folder,
    FolderCreate,
    FolderMove,
    FolderRename,
    AskRequest,
    CaptureRequest,
    ExternalOcrResult,
)

def _db() -> Path:
    """The notes database of the active settings (app/config.py)."""

    return get_settings().block_db_path


# Hold references to fire-and-forget tasks (I/O only, e.g. warming the LLM
# connection) so they aren't garbage-collected mid-run.
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


# Background lanes the app runs (app/background.py); each module registers its own.
onenote_import.jobs.ocr = ocr.wake


def _start_background(settings) -> None:
    lanes = ["mirrors", "imports"]
    if settings.auto_backup_enabled:
        lanes.append("backups")
    if settings.embeddings_enabled:
        lanes.append("index")
    if settings.vision_enabled:
        # Reads text in pictures and PDFs, starting with whatever an earlier run left.
        ocr.start(settings.block_db_path)
    background.start(*lanes)


def _stop_background() -> None:
    """Stop every lane (and the converter it may run), then write pending mirrors."""

    background.stop()
    documents.flush_mirrors()


# The Mac app shows the text after this prefix when the backend stops at startup.
STARTUP_ERROR_PREFIX = "Fortress can't start: "


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    new_workspace = not settings.block_db_path.exists()
    try:
        await asyncio.to_thread(block_store.initialize, settings.block_db_path)
    except block_store.WorkspaceTooOld as error:
        # One plain line the app shows (BackendController.startupError) before the traceback.
        logging.getLogger("fortress").error("%s%s", STARTUP_ERROR_PREFIX, error)
        raise
    if new_workspace and settings.welcome_note:
        await asyncio.to_thread(seed_welcome_note, settings.block_db_path)
    await asyncio.to_thread(block_store.ensure_fts_integrity, settings.block_db_path)
    _start_background(settings)
    try:
        yield
    finally:
        await asyncio.to_thread(_stop_background)


app = FastAPI(title="Fortress Notes", lifespan=lifespan)

class ResponseHeadersMiddleware:
    """Headers every response of a kind needs (plain ASGI: no body buffering).

    API responses default to ``Cache-Control: no-cache`` (keep, but revalidate;
    GETs carry ETags). Stored files are never sniffed, ``.bin`` downloads as an
    attachment and a stored SVG picture never runs scripts when opened directly.
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
# Keep last (outermost): token and browser checks run before anything else.
from .auth import LocalAuthMiddleware  # noqa: E402

app.add_middleware(LocalAuthMiddleware)


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
app.mount("/media", media.MediaFiles(lambda: get_settings().assets_path), name="media")

# Batch moves for the native app (app/batch.py).
from .batch import router as batch_router  # noqa: E402

app.include_router(batch_router)

@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "embeddings_enabled": get_settings().embeddings_enabled,
        "model_loaded": embeddings.model_loaded(),
        # Where ColBERT encodes queries and documents ("cpu" or "mlx"); null while loading.
        "embeddings_device": embeddings.device_info(),
        "vision_enabled": get_settings().vision_enabled,
        # Whether Ask can write answers (OPENROUTER_API_KEY is set). Never the key itself.
        "ask_configured": bool(get_settings().openrouter_api_key),
    }


@app.get("/api/ocr")
def ocr_status() -> dict:
    """Background reading of pictures and PDFs: model, queued/done/failed counts."""

    return {"enabled": get_settings().vision_enabled, **ocr.status_counts(_db())}


def _external_model(model: str) -> str:
    if not ocr.is_external_model(model):
        raise HTTPException(400, f"Unknown OCR reader {model!r}")
    return model


@app.get("/api/ocr/pending")
def ocr_pending(
    model: str = Query(..., max_length=64), limit: int = Query(8, ge=0, le=100)
) -> dict:
    """Pictures and PDFs an external reader (the Mac app's Apple Vision, ``model`` like
    ``apple-vision/26``) has yet to read, newest first. Asking marks the reader active,
    so this process's own OCR leaves the backlog to it (app/ocr.py)."""

    model = _external_model(model)
    ocr.note_external_reader(model)
    total, rows = ocr.external_pending(_db(), model, limit)
    return {
        "model": model,
        "total": total,
        "items": [
            {
                "content_hash": row["content_hash"],
                "media_url": f"/media/{quote(row['media_name'])}",
                "mime_type": row["mime_type"],
                "attempts": row["attempts"],
            }
            for row in rows
        ],
    }


@app.get("/api/ocr/{content_hash}")
def ocr_reading(content_hash: str) -> dict:
    """All the text read from a picture or PDF (the note's info carries only its start)."""

    found = ocr.reading(_db(), content_hash)
    if found is None:
        raise HTTPException(404, "No text has been read from that picture yet")
    return found


@app.put("/api/ocr/{content_hash}")
def ocr_store(content_hash: str, result: ExternalOcrResult) -> dict:
    """Store an external reader's text for a picture or PDF (by content hash). Its text
    outranks this process's model and reaches search, ColBERT and Ask the same way."""

    model = _external_model(result.model)
    ocr.note_external_reader(model)
    try:
        return ocr.store_external(
            _db(), content_hash, model, status=result.status, text=result.text,
            method=result.method, pages=result.pages, error=result.error,
            duration_ms=result.duration_ms,
        )
    except KeyError:
        raise HTTPException(404, "No picture or PDF with that content") from None


@app.get("/api/navigation")
def get_navigation(recent_limit: int = Query(10, ge=1, le=50)):
    return http_cache.json_payload(block_store.navigation(_db(), recent_limit))


@app.post("/api/folders", response_model=Folder, status_code=201)
def create_folder(data: FolderCreate):
    try:
        return block_store.create_folder(
            _db(), data.name, data.parent_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/folders/{folder_id}", response_model=Folder)
def rename_folder(folder_id: str, data: FolderRename):
    try:
        return block_store.rename_folder(_db(), folder_id, data.name)
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
            _db(), folder_id, parent_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: str):
    try:
        block_store.delete_folder(_db(), folder_id)
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
            _db(), document_id, folder_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/block-documents", status_code=201)
def create_block_document(data: NoteCreate):
    try:
        tree = documents.create(data)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return http_cache.written_response(tree["id"], status_code=201)


@app.patch("/api/block-documents/{document_id}")
def update_block_document(document_id: str, data: NoteUpdate):
    """Change a document's title (the body changes through transactions).

    Answers the metadata payload (``children`` empty, as ``?response=meta`` of a
    transaction) with ``X-Document-ETag``; the note's content did not change.
    """

    try:
        meta, validator = documents.update(document_id, data)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return http_cache.tree_response(document_id, meta, validator)


@app.delete("/api/block-documents/{document_id}", status_code=204)
def delete_block_document(document_id: str):
    try:
        documents.trash(document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/block-documents/{document_id}")
def get_block_document(document_id: str, request: Request):
    """The note's block tree; ``If-None-Match`` with its ETag answers 304.

    A read only: opening is recorded by ``POST .../opened``, and the paragraph
    an empty note needs is made when the note is created, imported or restored.
    """

    return http_cache.document_response(request, document_id)


@app.post("/api/block-documents/{document_id}/opened", status_code=204)
def record_opened(document_id: str):
    """The person opened the note (for "last opened"); the app sends it fire-and-forget.

    Debounced per note and skipped rather than waited for while a writer holds
    the database. Not part of the note's ETag.
    """

    if not block_store.document_exists(_db(), document_id):
        raise HTTPException(404, "Block document not found")
    block_store.mark_opened(_db(), document_id, wait=False)
    return Response(status_code=204)


@app.get("/api/block-documents/{document_id}/info")
def get_block_document_info(document_id: str):
    """Metadata for an info panel: dates, where the note came from, counts."""

    try:
        return http_cache.json_payload(documents.document_info(_db(), document_id))
    except KeyError as exc:
        raise HTTPException(404, "Block document not found") from exc


@app.get("/api/block-documents/{document_id}/markdown")
def export_block_document_markdown(document_id: str, download: bool = False):
    try:
        markdown = export_document_markdown(_db(), document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    headers = {}
    if download:
        tree = block_store.document_tree(_db(), document_id)
        title = vault.file_stem(str(tree["title"])) if tree else document_id
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
        block_store.folder_exists, _db(), folder_id
    ):
        raise HTTPException(404, "folder not found")
    imported: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    readable: list[tuple[str, UploadedNote]] = []
    for upload in files:
        name = upload.filename or "untitled.md"
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            errors.append({"name": name, "detail": "file is larger than 5 MB"})
            continue
        try:
            readable.append((name, parse_uploaded_markdown(name, raw)))
        except ValueError as exc:
            errors.append({"name": name, "detail": str(exc)})

    def create_all() -> None:
        # Read together, so [[wikilinks]] between the files resolve.
        notes = [note for _, note in readable]
        parsed = parse_uploaded_batch(_db(), notes, block_store.new_id)
        for (name, note), (document_id, specs) in zip(readable, parsed):
            try:
                created = documents.create(
                    NoteCreate(title=note.title, body=note.body, folder_id=folder_id),
                    created_at=note.created_at,
                    source_kind="markdown_import",
                    document_id=document_id,
                    specs=specs,
                )
            except ValueError as exc:
                errors.append({"name": name, "detail": str(exc)})
                continue
            imported.append({"name": name, "id": created["id"], "title": created["title"]})

    await run_in_threadpool(create_all)
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
            owned = onenote_import.imports_dir(get_settings().notes_path) / f"{job_id}-upload"
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
        db_path=_db(),
        notes_path=get_settings().notes_path,
        assets_path=get_settings().assets_path,
        folder_id=folder_id,
    )
    return {"job_id": record["job_id"], "status": record["status"]}


def _check_import_folder(folder_id: str | None) -> None:
    if folder_id is not None and not block_store.folder_exists(_db(), folder_id):
        raise HTTPException(404, "folder not found")


@app.get("/api/import/onenote")
def list_onenote_imports(limit: int = Query(20, ge=1, le=100)):
    return onenote_import.jobs.list(get_settings().notes_path, limit)


@app.get("/api/import/onenote/{job_id}")
def get_onenote_import(job_id: str):
    record = onenote_import.jobs.get(job_id, get_settings().notes_path)
    if record is None:
        raise HTTPException(404, "import job not found")
    return record


@app.post("/api/import/onenote/{job_id}/cancel", status_code=202)
def cancel_onenote_import(job_id: str):
    """Stop an import between pages; imported pages are kept (status ``cancelled``)."""

    record, accepted = onenote_import.jobs.cancel(job_id, get_settings().notes_path)
    if record is None:
        raise HTTPException(404, "import job not found")
    if not accepted:
        raise HTTPException(409, f"The import already ended ({record.get('status')}).")
    return record


# --- OneNote download from Microsoft (jobs; app/onenote_graph.py) ------------------


@app.get("/api/import/onenote-graph")
def onenote_graph_status():
    """Whether the download can run (tools/fortress-onenote-graph and uv are there;
    ``problem`` says how to fix it) and the latest download job, if any."""

    return {**onenote_graph.availability(), "job": onenote_graph.downloads.latest()}


async def _start_onenote_graph(request: Request, kind: str) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Send JSON {\"client_id\": ..., \"account\": ...}.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("client_id"), str):
        raise HTTPException(400, "Send JSON {\"client_id\": ..., \"account\": ...}.")
    account = payload.get("account") or "personal"
    notebooks = payload.get("notebooks")
    folder_id = payload.get("folder_id") or None
    if notebooks is not None and not (isinstance(notebooks, list) and all(isinstance(n, str) for n in notebooks)):
        raise HTTPException(400, "notebooks must be a list of names")
    if folder_id is not None and not isinstance(folder_id, str):
        raise HTTPException(400, "folder_id must be a string")
    await run_in_threadpool(_check_import_folder, folder_id)
    settings = get_settings()
    try:
        return await run_in_threadpool(
            lambda: onenote_graph.downloads.submit(
                kind=kind, client_id=payload["client_id"], account=str(account), notebooks=notebooks,
                folder_id=folder_id, db_path=_db(), notes_path=settings.notes_path,
                assets_path=settings.assets_path))
    except onenote_graph.GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/import/onenote-graph", status_code=202)
async def start_onenote_graph_download(request: Request):
    """Sign in, download notebooks from Microsoft, then import them:
    ``{"client_id", "account": "personal"|"work", "notebooks"?: [names], "folder_id"?}``.
    When the job is ``done`` its ``import_job_id`` is an ordinary OneNote import job."""

    return await _start_onenote_graph(request, "download")


@app.post("/api/import/onenote-graph/notebooks", status_code=202)
async def list_onenote_graph_notebooks(request: Request):
    """Sign in and list the notebooks (the job's ``notebooks`` when ``done``)."""

    return await _start_onenote_graph(request, "notebooks")


@app.get("/api/import/onenote-graph/{job_id}")
def get_onenote_graph_job(job_id: str):
    record = onenote_graph.downloads.get(job_id)
    if record is None:
        raise HTTPException(404, "download job not found")
    return record


@app.post("/api/import/onenote-graph/{job_id}/cancel", status_code=202)
def cancel_onenote_graph_job(job_id: str):
    """Stop a download (the tool's cache keeps what it fetched: running it again resumes)."""

    record, accepted = onenote_graph.downloads.cancel(job_id)
    if record is None:
        raise HTTPException(404, "download job not found")
    if not accepted:
        raise HTTPException(409, f"The download already ended ({record.get('status')}).")
    return record


def _attachment(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@app.post("/api/capture", status_code=201)
def capture(data: CaptureRequest):
    """Save a quick thought as its own note in the Uncategorized folder."""

    try:
        return documents.capture(data.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# --- Trash ---------------------------------------------------------------------


@app.get("/api/trash")
def list_trash():
    return http_cache.json_payload(documents.list_trash(_db()))


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


# --- Backups -------------------------------------------------------------------


@app.get("/api/backups")
def list_backups():
    return backups.list_backups(backups.backup_directory(get_settings().notes_path))


@app.post("/api/backups", status_code=201)
def create_backup():
    directory = backups.backup_directory(get_settings().notes_path)
    path = backups.create_backup(_db(), directory, "manual")
    return next(item for item in backups.list_backups(directory) if item["name"] == path.name)


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    directory = backups.backup_directory(get_settings().notes_path)
    try:
        backups.resolve_backup(directory, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    # No background writer runs while the database is swapped; mirrors of the
    # edits made so far are written first, then all of them follow the backup.
    try:
        with background.paused():
            documents.flush_mirrors()
            try:
                safety = backups.restore_backup(_db(), directory, name)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            documents.rebuild_mirrors(directory / f"mirrors-before-{safety.stem}")
    except background.Busy as exc:
        raise HTTPException(409, f"{exc}; try again when it has finished") from exc
    return {"restored": name, "safety_backup": safety.name}


@app.get("/api/unified-search")
async def unified_search(
    q: str,
    limit: int = Query(30, ge=1, le=100),
    document_id: str | None = None,
    block_type: str | None = None,
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


@app.get("/api/block-link-targets", response_model=list[BlockLinkTarget])
def block_link_targets(q: str = "", limit: int = Query(50, ge=1, le=200)):
    return block_store.block_link_targets(_db(), q, limit)


@app.get("/api/block-documents/{document_id}/backlinks", response_model=list[Backlink])
def get_backlinks(document_id: str, limit: int = Query(100, ge=1, le=500)):
    if not block_store.document_exists(_db(), document_id):
        raise HTTPException(404, "Block document not found")
    return http_cache.json_payload(block_store.document_backlinks(_db(), document_id, limit))


@app.post("/api/block-documents/{document_id}/transactions")
def apply_block_transaction(
    document_id: str,
    transaction: BlockTransaction,
    response: str = Query("meta", pattern="^(document|meta)$"),
):
    """Apply a transaction. Answers the metadata payload (``children`` empty) by
    default, or the whole tree with ``?response=document``; ``X-Document-ETag``
    either way."""

    try:
        validator: list = []
        tree = block_store.apply_transaction(
            _db(),
            document_id,
            transaction.operations,
            transaction.base_revision,
            transaction.transaction_id,
            validator_out=validator,
            include_children=response == "document",
        )
        if not validator:
            return http_cache.written_response(document_id)
        return http_cache.tree_response(document_id, tree, validator[0])
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except BlockShapeError as exc:
        # An unknown type, illegal nesting or attrs/content the registry rejects.
        raise HTTPException(422, str(exc)) from exc
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
        block_store.register_asset, _db(), path, digest,
        media_type or "application/octet-stream",
    )
    ocr.wake()
    # The Mac app reads ``props.url``/``props.name`` (EditorUpload.Response).
    return {"props": {"url": url, "name": name}}


@app.get("/api/block-documents/{document_id}/related", response_model=list[RelatedResult])
def related(document_id: str, k: int = Query(5, ge=1, le=50), block_id: str | None = None):
    """Notes like this one (or like one of its blocks, with ``block_id``)."""

    if not block_store.document_exists(_db(), document_id):
        raise HTTPException(404, "Block document not found")
    try:
        return search.related_notes(document_id, k, block_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


