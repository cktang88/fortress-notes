import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from . import (
    assets,
    backups,
    block_query,
    block_store,
    embeddings,
    images,
    documents,
    llm,
    search,
    vision,
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
    SavedSearchCreate,
)

settings = get_settings()

# Hold references to fire-and-forget tasks so they aren't garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        exc = t.exception()
        if exc:
            logging.getLogger("fortress").exception("background task failed", exc_info=exc)

    task.add_done_callback(_done)


async def _reindex_loop() -> None:
    """Periodically pre-warm embeddings for changed notes so searches stay instant."""
    while True:
        await asyncio.sleep(settings.reindex_interval_s)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(embeddings.warm_index)


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
    # Pre-load the vision models in a background thread so the app serves immediately
    # and the first image upload isn't a cold start.
    if settings.vision_enabled:
        _spawn(asyncio.to_thread(vision.warm))
    try:
        yield
    finally:
        for running in (task, backup_task):
            if running:
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running


app = FastAPI(title="Fortress Notes", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def secure_uploaded_files(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/media/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.endswith(".bin"):
            response.headers["Content-Disposition"] = "attachment"
    return response


# Serve stored images at /media/<file>.
app.mount("/media", StaticFiles(directory=settings.assets_path), name="media")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "embeddings_enabled": settings.embeddings_enabled,
        "model_loaded": embeddings.model_loaded(),
        "vision_enabled": settings.vision_enabled,
        "vision_loaded": vision.loaded(),
        "block_db_ready": block_store.is_ready(settings.block_db_path),
    }


@app.get("/api/navigation")
def get_navigation(recent_limit: int = Query(10, ge=1, le=50)):
    return block_store.navigation(settings.block_db_path, recent_limit)


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
def get_block_document(document_id: str):
    """Read the bootstrapped block tree while the legacy note API remains active."""

    tree = block_store.document_tree(settings.block_db_path, document_id)
    if tree is None:
        raise HTTPException(404, "Block document not found")
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
    if folder_id is not None and not block_store.folder_exists(settings.block_db_path, folder_id):
        raise HTTPException(404, "folder not found")
    imported: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for upload in files:
        name = upload.filename or "untitled.md"
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            errors.append({"name": name, "detail": "file is larger than 5 MB"})
            continue
        try:
            parsed = parse_uploaded_markdown(name, raw)
            note = documents.create(
                NoteCreate(
                    title=parsed.title,
                    body=parsed.body,
                    status=parsed.status,
                    tags=parsed.tags,
                    folder_id=folder_id,
                ),
                created_at=parsed.created_at,
            )
        except ValueError as exc:
            errors.append({"name": name, "detail": str(exc)})
            continue
        imported.append({"name": name, "id": note.id, "title": note.title})
    return {"imported": imported, "errors": errors}


def _attachment(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


# --- Trash ---------------------------------------------------------------------


@app.get("/api/trash")
def list_trash():
    return workspace.list_trash(settings.block_db_path)


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
def list_backups():
    return backups.list_backups(backups.backup_directory(settings.notes_path))


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
    tag: str | None = None,
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
            tag=tag,
            updated_after=updated_after,
            updated_before=updated_before,
        ),
        limit=limit,
    )


@app.get("/api/unified-search")
def unified_search(
    q: str,
    limit: int = Query(30, ge=1, le=100),
    document_id: str | None = None,
    block_type: str | None = None,
    status: NoteStatus | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
):
    """Blocks matching by exact words, meaning, or note title, in one ranked list."""

    return search.unified_search(
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


@app.post("/api/block-search/rebuild")
def rebuild_block_search_index():
    block_store.rebuild_fts(settings.block_db_path)
    return {"ok": block_store.fts_is_consistent(settings.block_db_path)}


@app.get("/api/block-link-targets", response_model=list[BlockLinkTarget])
def block_link_targets(q: str = "", limit: int = Query(50, ge=1, le=200)):
    return block_store.block_link_targets(settings.block_db_path, q, limit)


@app.get("/api/block-documents/{document_id}/backlinks", response_model=list[Backlink])
def get_backlinks(document_id: str, limit: int = Query(100, ge=1, le=500)):
    if block_store.document_tree(settings.block_db_path, document_id) is None:
        raise HTTPException(404, "Block document not found")
    return block_store.document_backlinks(settings.block_db_path, document_id, limit)


@app.post("/api/block-documents/{document_id}/link-checks")
async def check_block_document_links(document_id: str):
    try:
        return await link_checks.check_document_links(
            settings.block_db_path, document_id
        )
    except KeyError as exc:
        raise HTTPException(404, "Block document not found") from exc


@app.post("/api/block-documents/{document_id}/transactions")
def apply_block_transaction(document_id: str, transaction: BlockTransaction):
    try:
        tree = block_store.apply_transaction(
            settings.block_db_path,
            document_id,
            transaction.operations,
            transaction.base_revision,
            transaction.transaction_id,
        )
        documents.write_mirror(document_id)
        return tree
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/block-documents/{document_id}/undo")
def undo_block_transaction(document_id: str, request: BlockHistoryRequest):
    try:
        tree = block_store.undo_transaction(
            settings.block_db_path, document_id, request.base_revision
        )
        documents.write_mirror(document_id)
        return tree
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
        documents.write_mirror(document_id)
        return tree
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except block_store.DocumentRevisionConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...)):
    if not assets.is_supported_image_type(file.content_type):
        raise HTTPException(400, "Unsupported image type")
    data = await file.read()
    # Save the file and return immediately so the image appears instantly. Caption + OCR
    # (slow, only needed for search) run in the background to populate the sidecar cache.
    url, path = images.save_bytes(data, file.content_type or "image/png")
    block_store.register_asset(
        settings.block_db_path, path, assets.content_hash(data),
        (file.content_type or "image/png").split(";", 1)[0].strip().lower(),
    )
    _spawn(asyncio.to_thread(images.extract_and_store, path))
    return {"url": url, "text": ""}


@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if media_type.startswith("image/") and not assets.is_supported_image_type(media_type):
        raise HTTPException(415, "Unsupported image type")
    data = await file.read()
    url, name, path = assets.save_upload(data, file.filename, file.content_type)
    block_store.register_asset(
        settings.block_db_path, path, assets.content_hash(data),
        (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower(),
    )
    if assets.is_supported_image_type(file.content_type):
        _spawn(asyncio.to_thread(images.extract_and_store, path))
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
    if documents.get_note(note_id) is None:
        raise HTTPException(404, "Note not found")
    try:
        return search.related_notes(note_id, k, block_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/notes/{note_id}/review", response_model=ReviewResponse)
async def review(note_id: str, kind: ReviewKind = "factcheck"):
    note = documents.get_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    return await llm.review_note(kind, note.title, note.body)


@app.post(
    "/api/block-documents/{document_id}/blocks/{block_id}/review",
    response_model=BlockReviewResponse,
)
async def review_block(document_id: str, block_id: str, kind: ReviewKind = "factcheck"):
    try:
        result = block_store.document_subtree(
            settings.block_db_path, document_id, block_id
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
        resolved = block_store.document_review_context(
            settings.block_db_path, document_id, request.context, request.block_ids
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
