import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import analysis, block_store, embeddings, images, llm, notes_store, search, vision
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
    ReviewKind,
    ReviewResponse,
    SearchResult,
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
                block_store.bootstrap_markdown, settings.notes_path, settings.block_db_path
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


@app.get("/api/block-documents/{document_id}")
def get_block_document(document_id: str):
    """Read the bootstrapped block tree while the legacy note API remains active."""

    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    tree = block_store.document_tree(settings.block_db_path, document_id)
    if tree is None:
        raise HTTPException(404, "Block document not found")
    return tree


@app.post("/api/block-documents/{document_id}/transactions")
def apply_block_transaction(document_id: str, transaction: BlockTransaction):
    if not settings.block_db_enabled:
        raise HTTPException(404, "Block store is disabled")
    try:
        return block_store.apply_transaction(
            settings.block_db_path, document_id, transaction.operations
        )
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
    return notes_store.create_note(data)


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
    return note


@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: str):
    if not notes_store.delete_note(note_id):
        raise HTTPException(404, "Note not found")
    return Response(status_code=204)


@app.post("/api/notes/{note_id}/promote", response_model=Note)
def promote_note(note_id: str):
    note = notes_store.promote_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found")
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
