from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

NoteStatus = Literal["rough", "polished"]
ReviewKind = Literal["factcheck", "clarify", "object"]


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


class SearchResult(BaseModel):
    note: NoteSummary
    score: float = Field(description="Higher is more relevant")


class ReviewItem(BaseModel):
    label: str = ""
    detail: str


class ReviewResponse(BaseModel):
    kind: ReviewKind
    summary: str
    items: list[ReviewItem] = []


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
