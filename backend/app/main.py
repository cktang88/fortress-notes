from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from . import analysis, embeddings, llm, notes_store, search
from .config import get_settings
from .models import (
    ConsistencyReport,
    HealReport,
    Note,
    NoteCreate,
    NoteStatus,
    NoteSummary,
    NoteUpdate,
    ReviewKind,
    ReviewResponse,
    SearchResult,
)

settings = get_settings()
app = FastAPI(title="Fortress Notes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "embeddings_enabled": settings.embeddings_enabled,
        "model_loaded": embeddings.model_loaded(),
    }


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
