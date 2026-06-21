"""CRUD over Markdown files. The filesystem is the source of truth."""

from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from ulid import ULID

from . import images
from .config import get_settings
from .models import Note, NoteCreate, NoteSummary, NoteUpdate, NoteStatus

SNIPPET_LEN = 160


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _path(note_id: str) -> Path:
    return get_settings().notes_path / f"{note_id}.md"


def _derive_title(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Untitled"


def _snippet(body: str) -> str:
    text = " ".join(body.split())
    return text[:SNIPPET_LEN]


def _to_note(post: frontmatter.Post, note_id: str) -> Note:
    meta = post.metadata
    body = post.content
    return Note(
        id=note_id,
        title=meta.get("title") or _derive_title(body),
        status=meta.get("status", "rough"),
        tags=meta.get("tags", []) or [],
        created_at=meta.get("created_at", _now()),
        updated_at=meta.get("updated_at", _now()),
        snippet=_snippet(body),
        body=body,
        images=meta.get("images", {}) or {},
    )


def _write(note: Note) -> None:
    # Refresh cached image text from the sidecars for whatever images the body references.
    note.images = images.collect_image_texts(note.body)
    post = frontmatter.Post(
        note.body,
        id=note.id,
        title=note.title,
        status=note.status,
        tags=note.tags,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat(),
        images=note.images,
    )
    _path(note.id).write_bytes(frontmatter.dumps(post).encode("utf-8"))


def list_notes(status: NoteStatus | None = None) -> list[NoteSummary]:
    notes = [get_note(p.stem) for p in get_settings().notes_path.glob("*.md")]
    notes = [n for n in notes if n is not None]
    if status:
        notes = [n for n in notes if n.status == status]
    notes.sort(key=lambda n: n.updated_at, reverse=True)
    return [NoteSummary(**n.model_dump(exclude={"body", "created_at"})) for n in notes]


def get_note(note_id: str) -> Note | None:
    path = _path(note_id)
    if not path.exists():
        return None
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    return _to_note(post, note_id)


def create_note(data: NoteCreate) -> Note:
    now = _now()
    note = Note(
        id=str(ULID()),
        title=data.title or _derive_title(data.body),
        status=data.status,
        tags=data.tags,
        created_at=now,
        updated_at=now,
        snippet=_snippet(data.body),
        body=data.body,
    )
    _write(note)
    return note


def update_note(note_id: str, data: NoteUpdate) -> Note | None:
    note = get_note(note_id)
    if note is None:
        return None
    if data.body is not None:
        note.body = data.body
    if data.title is not None:
        note.title = data.title
    elif not note.title:
        # Only auto-derive a title when the note has none — never clobber a user title.
        note.title = _derive_title(note.body)
    if data.status is not None:
        note.status = data.status
    if data.tags is not None:
        note.tags = data.tags
    note.updated_at = _now()
    note.snippet = _snippet(note.body)
    _write(note)
    return note


def delete_note(note_id: str) -> bool:
    path = _path(note_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def promote_note(note_id: str) -> Note | None:
    return update_note(note_id, NoteUpdate(status="polished"))
