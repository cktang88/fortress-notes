import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import (
    analysis,
    block_query,
    block_store,
    embeddings,
    images,
    llm,
    notes_store,
    search,
    vision,
)
from .portability import (
    export_all_markdown,
    export_document_markdown,
    import_markdown_directory,
    import_markdown_if_empty,
)
from .config import get_settings
from .models import (
    ConsistencyReport,
    HealReport,
    Note,
    NoteCreate,
    NoteStatus,
    NoteSummary,
    NoteUpdate,
    BlockTransaction,
    BlockSearchResult,
    BlockLinkTarget,
    Backlink,
    ReviewKind,
    ReviewResponse,
    SearchResult,
    DocumentMove,
    DocumentOrganization,
    Folder,
    FolderCreate,
    FolderMove,
    FolderRename,
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.block_db_enabled:
        if settings.block_db_import_on_startup:
            await asyncio.to_thread(
                import_markdown_if_empty, settings.block_db_path, settings.notes_path
            )
        else:
            await asyncio.to_thread(block_store.initialize, settings.block_db_path)
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
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Fortress Notes", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        "block_db_enabled": settings.block_db_enabled,
        "block_db_ready": block_store.is_ready(settings.block_db_path),
    }


@app.get("/api/navigation")
def get_navigation(recent_limit: int = Query(10, ge=1, le=50)):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    return block_store.navigation(settings.block_db_path, recent_limit)


@app.post("/api/folders", response_model=Folder, status_code=201)
def create_folder(data: FolderCreate):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    try:
        return block_store.create_folder(
            settings.block_db_path, data.name, data.parent_id, data.position
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/folders/{folder_id}", response_model=Folder)
def rename_folder(folder_id: str, data: FolderRename):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    try:
        return block_store.rename_folder(settings.block_db_path, folder_id, data.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/folders/{folder_id}/move", response_model=Folder)
def move_folder(folder_id: str, data: FolderMove):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
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
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
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
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
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


@app.get("/api/block-documents/{document_id}")
def get_block_document(document_id: str):
    """Read the bootstrapped block tree while the legacy note API remains active."""

    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    tree = block_store.document_tree(settings.block_db_path, document_id)
    if tree is None:
        raise HTTPException(404, "Block document not found")
    return tree


@app.get("/api/block-documents/{document_id}/markdown")
def export_block_document_markdown(document_id: str):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    try:
        markdown = export_document_markdown(settings.block_db_path, document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=markdown, media_type="text/markdown")


@app.get("/api/markdown-export")
def export_all_block_markdown():
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    return export_all_markdown(settings.block_db_path)


@app.post("/api/markdown-import")
def import_workspace_markdown():
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
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
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
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


@app.get("/api/block-link-targets", response_model=list[BlockLinkTarget])
def block_link_targets(q: str = "", limit: int = Query(50, ge=1, le=200)):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    return block_store.block_link_targets(settings.block_db_path, q, limit)


@app.get("/api/block-documents/{document_id}/backlinks", response_model=list[Backlink])
def get_backlinks(document_id: str, limit: int = Query(100, ge=1, le=500)):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    if block_store.document_tree(settings.block_db_path, document_id) is None:
        raise HTTPException(404, "Block document not found")
    return block_store.document_backlinks(settings.block_db_path, document_id, limit)


@app.post("/api/block-documents/{document_id}/transactions")
def apply_block_transaction(document_id: str, transaction: BlockTransaction):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    try:
        tree = block_store.apply_transaction(
            settings.block_db_path, document_id, transaction.operations
        )
        block_store.sync_markdown(settings.notes_path, settings.block_db_path, document_id)
        return tree
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...)):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "Not an image")
    data = await file.read()
    # Save the file and return immediately so the image appears instantly. Caption + OCR
    # (slow, only needed for search) run in the background to populate the sidecar cache.
    url, path = images.save_bytes(data, file.content_type or "image/png")
    _spawn(asyncio.to_thread(images.extract_and_store, path))
    return {"url": url, "text": ""}


@app.get("/api/notes", response_model=list[NoteSummary])
def list_notes(status: NoteStatus | None = None):
    return notes_store.list_notes(status)


@app.post("/api/notes", response_model=Note, status_code=201)
def create_note(data: NoteCreate):
    note = notes_store.create_note(data)
    if settings.block_db_enabled:
        block_store.create_document(
            settings.block_db_path,
            note.id,
            note.title,
            note.status,
            note.tags,
            note.body,
            note.created_at,
            note.updated_at,
        )
    return note


@app.get("/api/notes/{note_id}", response_model=Note)
def get_note(note_id: str):
    note = notes_store.get_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    return note


@app.put("/api/notes/{note_id}", response_model=Note)
def update_note(note_id: str, data: NoteUpdate):
    note = notes_store.update_note(note_id, data)
    if note is None:
        raise HTTPException(404, "Note not found")
    if settings.block_db_enabled:
        if data.body is not None:
            block_store.replace_document_from_markdown(
                settings.block_db_path,
                note.id,
                note.title,
                note.status,
                note.tags,
                note.body,
                note.created_at,
                note.updated_at,
            )
        else:
            block_store.update_document_metadata(
                settings.block_db_path,
                note.id,
                note.title,
                note.status,
                note.tags,
                note.updated_at,
            )
    return note


@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: str):
    if not notes_store.delete_note(note_id):
        raise HTTPException(404, "Note not found")
    if settings.block_db_enabled:
        block_store.delete_document(settings.block_db_path, note_id)
    return Response(status_code=204)


@app.post("/api/notes/{note_id}/promote", response_model=Note)
def promote_note(note_id: str):
    note = notes_store.promote_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    if settings.block_db_enabled:
        block_store.update_document_metadata(
            settings.block_db_path,
            note.id,
            note.title,
            note.status,
            note.tags,
            note.updated_at,
        )
    return note


@app.get("/api/search", response_model=list[SearchResult])
def search_notes(q: str, mode: str = Query("text", pattern="^(text|embedding)$")):
    if mode == "embedding":
        return search.embedding_search(q)
    return search.full_text_search(q)


@app.get("/api/notes/{note_id}/related", response_model=list[SearchResult])
def related(note_id: str, k: int = 5):
    if notes_store.get_note(note_id) is None:
        raise HTTPException(404, "Note not found")
    return search.related_notes(note_id, k)


@app.post("/api/notes/{note_id}/review", response_model=ReviewResponse)
async def review(note_id: str, kind: ReviewKind = "factcheck"):
    note = notes_store.get_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
    return await llm.review_note(kind, note.title, note.body)


@app.post("/api/notes/{note_id}/consistency", response_model=ConsistencyReport)
async def consistency(note_id: str, k: int = 5):
    if notes_store.get_note(note_id) is None:
        raise HTTPException(404, "Note not found")
    return await analysis.check_consistency(note_id, k)


@app.post("/api/notes/{note_id}/heal", response_model=HealReport)
async def heal(note_id: str):
    if notes_store.get_note(note_id) is None:
        raise HTTPException(404, "Note not found")
    return await analysis.heal_note(note_id)
