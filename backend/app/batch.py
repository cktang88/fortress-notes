"""Batch endpoints for the native app's data layer.

One request in place of N: prefetching notes (next/previous in the list, hover) and
moving many notes and folders at once (deleting a folder moves its contents up).
Kept in its own module so it stays out of the way of ``main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import block_store

router = APIRouter()

MAX_BATCH_DOCUMENTS = 50
MAX_BATCH_MOVES = 500


def _db_path():
    # Read through ``main`` so tests that patch ``main.settings`` reach this module too.
    from . import main

    return main.settings.block_db_path


class DocumentBatchRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH_DOCUMENTS)
    # Revisions the client already holds: those documents come back as ``unchanged``.
    known: dict[str, int] = Field(default_factory=dict)


@router.post("/api/batch/block-documents")
def batch_block_documents(request: DocumentBatchRequest) -> dict:
    """Several block documents in one read, for prefetching.

    Unlike ``GET /api/block-documents/{id}`` this does not mark the notes as opened:
    a prefetch is not the person opening a note.
    """

    path = _db_path()
    documents: list[dict] = []
    unchanged: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    block_store.initialize(path)
    for document_id in request.ids:
        if document_id in seen:
            continue
        seen.add(document_id)
        # The editor needs a block to type into (same rule as the single read).
        block_store.ensure_editable_block(path, document_id)
    with block_store.connection_scope(path) as connection:
        for document_id in dict.fromkeys(request.ids):
            known = request.known.get(document_id)
            if known is not None:
                row = connection.execute(
                    "SELECT revision FROM documents WHERE id = ? AND deleted_at IS NULL",
                    (document_id,),
                ).fetchone()
                if row is None:
                    missing.append(document_id)
                    continue
                if row["revision"] == known:
                    unchanged.append(document_id)
                    continue
            tree = block_store._document_tree(connection, document_id)
            if tree is None:
                missing.append(document_id)
            else:
                documents.append(tree)
    return {"documents": documents, "unchanged": unchanged, "missing": missing}


class DocumentMoveItem(BaseModel):
    id: str
    folder_id: str | None = None


class FolderMoveItem(BaseModel):
    id: str
    parent_id: str | None = None


class MoveBatchRequest(BaseModel):
    documents: list[DocumentMoveItem] = Field(default_factory=list, max_length=MAX_BATCH_MOVES)
    folders: list[FolderMoveItem] = Field(default_factory=list, max_length=MAX_BATCH_MOVES)


@router.post("/api/batch/move")
def batch_move(request: MoveBatchRequest) -> dict:
    """Moves notes and folders in one request (folders first, then notes).

    Each move is applied on its own, as the single-item endpoints do; the first one
    that fails stops the batch with its status, and the moves before it stay applied.
    """

    path = _db_path()
    moved_folders = 0
    moved_documents = 0
    for folder in request.folders:
        try:
            block_store.move_folder(path, folder.id, folder.parent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        moved_folders += 1
    for document in request.documents:
        try:
            block_store.move_document(path, document.id, document.folder_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        moved_documents += 1
    return {"folders": moved_folders, "documents": moved_documents}
