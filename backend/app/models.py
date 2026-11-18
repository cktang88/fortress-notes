from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

NoteStatus = Literal["rough", "polished"]
ReviewKind = Literal["factcheck", "object"]
BlockReviewContextKind = Literal["current", "selected", "document", "linked"]
BlockOperationKind = Literal["insert", "update", "move", "delete"]


class NoteSummary(BaseModel):
    id: str
    title: str
    status: NoteStatus
    tags: list[str] = []
    updated_at: datetime
    snippet: str = ""


class Note(NoteSummary):
    body: str
    created_at: datetime
    # Cached VLM caption + OCR text per referenced image id (mirrors the sidecar cache).
    images: dict[str, str] = {}


class NoteCreate(BaseModel):
    title: str = ""
    body: str = ""
    status: NoteStatus = "rough"
    tags: list[str] = []
    folder_id: str | None = None
    # Where the note came from (see app/provenance.py), e.g.
    # {"source": "ai", "extra": {"question": "..."}} for a saved Ask answer or
    # {"source": "web", "source_url": "https://..."} for a clipped page.
    provenance: dict[str, Any] | None = None


class NoteUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    status: NoteStatus | None = None
    tags: list[str] | None = None


class BlockOperation(BaseModel):
    """One operation of a block transaction (docs/block-model.md §4).

    Unknown fields (``text``, ``expected_updated_at``, ...) are ignored.
    """

    operation: BlockOperationKind
    block_id: str | None = None
    # Placement (insert, move): after or before a sibling, else last child of parent_id.
    parent_id: str | None = None
    after_id: str | None = None
    before_id: str | None = None
    # Deprecated (the Mac app's current requests): index among the parent's children.
    position: int | None = Field(default=None, ge=0)
    type: str | None = None
    # Insert: the block's attrs. Update: a patch (null deletes a key).
    attrs: dict[str, Any] | None = None
    # Update: a patch of the person's own attributes (null deletes a key).
    user_attrs: dict[str, Any] | None = None
    # Inline nodes (text types). The Mac app's current {"blocknote": runs} is read too.
    content: list[dict[str, Any]] | dict[str, Any] | None = None
    # Structured data (math, html).
    payload: dict[str, Any] | None = None
    # Insert: where the content came from (pasted, web, ai, ...; default manual).
    # Update: merged into the block's provenance.
    provenance: dict[str, Any] | None = None

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


class BlockHistoryRequest(BaseModel):
    base_revision: int = Field(ge=0)


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


class BlockSearchResult(BaseModel):
    block_id: str
    document_id: str
    document_title: str
    block_type: str
    text: str
    score: float = Field(description="Higher is more relevant")


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


class ReviewItem(BaseModel):
    label: str = ""
    detail: str
    quote: str = Field(default="", description="Verbatim text from the note this refers to")
    severity: Literal["high", "medium", "low"] = "medium"


class ReviewResponse(BaseModel):
    kind: ReviewKind
    summary: str
    items: list[ReviewItem] = []


class BlockReviewItem(ReviewItem):
    block_id: str


class BlockReviewResponse(BaseModel):
    kind: ReviewKind
    summary: str
    block_id: str
    items: list[BlockReviewItem] = []


class BlockReviewContextRequest(BaseModel):
    kind: ReviewKind
    context: BlockReviewContextKind
    block_ids: list[str] = []

    @model_validator(mode="after")
    def validate_context_ids(self) -> "BlockReviewContextRequest":
        if self.context in ("current", "linked") and len(self.block_ids) != 1:
            raise ValueError(f"{self.context} context requires exactly one block_id")
        if self.context == "selected" and not self.block_ids:
            raise ValueError("selected context requires at least one block_id")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("block_ids must not contain duplicates")
        if self.context == "document" and self.block_ids:
            raise ValueError("document context does not accept block_ids")
        return self


class BlockReviewContextResponse(BaseModel):
    kind: ReviewKind
    context: BlockReviewContextKind
    summary: str
    items: list[BlockReviewItem] = []


class SavedSearchCreate(BaseModel):
    name: str = Field(default="", max_length=100)
    query: str = Field(min_length=1, max_length=500)
    mode: Literal["text", "embedding"] = "text"
    filters: dict[str, str] = {}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    # Optional extra provenance (e.g. the source_url of a page captured from).
    provenance: dict[str, Any] | None = None


class SourceConflictResolution(BaseModel):
    # "source": take the source's newer version; "local": keep the local edit.
    keep: Literal["source", "local"]
