from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

NoteStatus = Literal["rough", "polished"]
BlockOperationKind = Literal[
    "insert", "update", "move", "delete", "set_attrs", "set_user_attrs",
    "duplicate", "split", "merge"
]


class NoteSummary(BaseModel):
    id: str
    title: str
    status: NoteStatus
    updated_at: datetime
    snippet: str = ""




class NoteCreate(BaseModel):
    title: str = ""
    body: str = ""
    status: NoteStatus = "rough"
    folder_id: str | None = None
    # Where the note came from (see app/provenance.py), e.g.
    # {"source": "ai", "extra": {"question": "..."}} for a saved Ask answer or
    # {"source": "web", "source_url": "https://..."} for a clipped page.
    provenance: dict[str, Any] | None = None


class NoteUpdate(BaseModel):
    # The body changes through block transactions; anything else is refused (422).
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    status: NoteStatus | None = None


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
    # insert/update: where the block's content came from (pasted, web, ai, ...).
    # Omitted on insert means typed here ("manual"); omitted on update keeps it.
    provenance: dict[str, Any] | None = None
    # For split, the offset in the block text in UTF-16 code units (JavaScript string
    # indices, as the browser counts; an emoji is 2). For duplicate/merge,
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
    # Optional extra provenance (e.g. the source_url of a page captured from).
    provenance: dict[str, Any] | None = None


class SourceConflictResolution(BaseModel):
    # "source": take the source's newer version; "local": keep the local edit.
    keep: Literal["source", "local"]
