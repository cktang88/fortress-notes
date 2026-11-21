from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

BlockOperationKind = Literal["insert", "update", "move", "delete"]


class NoteSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime
    snippet: str = ""




class NoteCreate(BaseModel):
    title: str = ""
    body: str = ""
    folder_id: str | None = None


class NoteUpdate(BaseModel):
    # The body changes through block transactions; anything else is refused (422).
    model_config = ConfigDict(extra="forbid")

    title: str | None = None


class BlockOperation(BaseModel):
    """One operation of a block transaction (docs/block-model.md §4).

    Canonical shapes only: an unknown field (``position``, ``text``,
    ``expected_updated_at``, ...) or non-list ``content`` is refused (422).
    """

    model_config = ConfigDict(extra="forbid")

    operation: BlockOperationKind
    block_id: str | None = None
    # Placement (insert, move): after or before a sibling, else last child of parent_id.
    parent_id: str | None = None
    after_id: str | None = None
    before_id: str | None = None
    type: str | None = None
    # Insert: the block's attrs. Update: a patch (null deletes a key).
    attrs: dict[str, Any] | None = None
    # Update: a patch of the person's own attributes (null deletes a key).
    user_attrs: dict[str, Any] | None = None
    # Inline nodes (text types).
    content: list[dict[str, Any]] | None = None
    # Structured data (html).
    payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_insert_block_id(self) -> "BlockOperation":
        # Clients may supply ULIDs or UUIDs; the server generates a ULID when omitted.
        if self.operation == "insert" and self.block_id is not None:
            if (
                len(self.block_id) == 26
                and self.block_id[0] in "01234567"
                and all(
                    character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
                    for character in self.block_id
                )
            ):
                return self
            try:
                if str(UUID(self.block_id)) == self.block_id.lower():
                    return self
            except ValueError:
                pass
            raise ValueError("insert block_id must be a ULID or UUID")
        return self


class BlockTransaction(BaseModel):
    transaction_id: str | None = Field(default=None, min_length=1, max_length=128)
    base_revision: int = Field(ge=0)
    operations: list[BlockOperation] = Field(min_length=1)


class Folder(BaseModel):
    id: str
    parent_id: str | None = None
    name: str
    position: int
    created_at: datetime
    updated_at: datetime


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)


class FolderRename(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class FolderMove(BaseModel):
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)


class DocumentMove(BaseModel):
    folder_id: str | None = None
    position: int | None = Field(default=None, ge=0)


class DocumentOrganization(BaseModel):
    id: str
    folder_id: str | None = None
    position: int


class SearchResult(BaseModel):
    note: NoteSummary
    score: float = Field(description="Higher is more relevant")


class RelatedResult(SearchResult):
    matched_block_id: str | None = None
    matched_block_text: str = ""




class BlockLinkTarget(BaseModel):
    block_id: str
    document_id: str
    document_title: str
    block_type: str
    text: str


class Backlink(BaseModel):
    source_block_id: str
    source_document_id: str
    source_document_title: str
    source_text: str
    label: str = ""
    target_block_id: str | None = None
















class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ExternalOcrResult(BaseModel):
    """The text another reader (the Mac app's Apple Vision) found in a picture or PDF."""

    model: str = Field(min_length=3, max_length=64)  # e.g. "apple-vision/26"
    status: Literal["done", "failed"] = "done"
    text: str = Field(default="", max_length=400_000)
    method: str = Field(default="ocr", max_length=32)  # ocr, pdf-text, pdf-text+ocr
    pages: int | None = Field(default=None, ge=0, le=100_000)
    error: str | None = Field(default=None, max_length=2_000)
    duration_ms: int | None = Field(default=None, ge=0)
