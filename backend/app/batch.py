"""Moving many notes and folders in one request (deleting a folder moves its
contents up). Kept in its own module so it stays out of the way of ``main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import block_store
from .config import get_settings

router = APIRouter()

MAX_BATCH_MOVES = 500


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

    path = get_settings().block_db_path
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
