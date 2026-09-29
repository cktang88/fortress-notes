from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

NoteStatus = Literal["rough", "polished"]
ReviewKind = Literal["factcheck", "clarify", "object"]
BlockReviewContextKind = Literal["current", "selected", "document", "linked"]
BlockOperationKind = Literal[
    "insert", "update", "move", "delete", "set_attrs", "set_user_attrs",
    "duplicate", "split", "merge"
]


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


class NoteUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    status: NoteStatus | None = None
    tags: list[str] | None = None


class BlockOperation(BaseModel):
    operation: BlockOperationKind
    block_id: str | None = None
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)
    type: str | None = None
    attrs: dict[str, Any] | None = None
    user_attrs: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    text: str | None = None
    split_at: int | None = Field(default=None, ge=0)
    # For split, the character offset in the block text. For duplicate/merge,
    # position keeps its existing sibling-position meaning.
    expected_updated_at: str | None = None

    @model_validator(mode="after")
    def validate_insert_block_id(self) -> "BlockOperation":
        # BlockNote currently supplies UUIDs. The server may omit the ID and
        # generate a ULID; accept both formats so validation doesn't rewrite
        # client IDs or constrain IDs already stored for other operations.
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


# ---- cross-note inconsistency detection ---------------------------------------


class ConsistencyIssue(BaseModel):
    related_note_id: str = ""
    related_note_title: str = ""
    claim: str = Field(description="What this note says")
    conflict: str = Field(description="What the related note says that contradicts it")
    severity: Literal["low", "medium", "high"] = "medium"


class ConsistencyReport(BaseModel):
    summary: str
    issues: list[ConsistencyIssue] = []
    checked_against: list[str] = Field(default=[], description="Titles of related notes checked")


# ---- self-healing notes -------------------------------------------------------


class DeadLink(BaseModel):
    url: str
    status: str = Field(description="HTTP status code or error reason")


class StaleFact(BaseModel):
    claim: str = Field(description="The possibly-outdated claim in the note")
    finding: str = Field(description="What current web search suggests")
    suggestion: str = Field(description="Suggested update")


class HealReport(BaseModel):
    summary: str
    dead_links: list[DeadLink] = []
    stale_facts: list[StaleFact] = []
