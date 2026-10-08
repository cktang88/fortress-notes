"""Note lifecycle over the canonical SQLite store, plus the Markdown mirrors.

SQLite is the only source of truth. Every write goes to SQLite first and then
refreshes ``NOTES_DIR/<id>.md``, a read-only mirror kept so notes stay readable
outside the app. Nothing reads the mirrors back except the explicit importer.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from . import block_store, duplicates, images, workspace
from .config import get_settings
from .models import Note, NoteCreate, NoteStatus, NoteSummary, NoteUpdate


def _paths() -> tuple[Path, Path]:
    settings = get_settings()
    return settings.block_db_path, settings.notes_path


# --- Reads -------------------------------------------------------------------


def list_notes(status: NoteStatus | None = None) -> list[NoteSummary]:
    db_path, _ = _paths()
    return [
        NoteSummary(**{key: item[key] for key in NoteSummary.model_fields})
        for item in block_store.list_documents(db_path)
        if status is None or item["status"] == status
    ]


def summaries() -> dict[str, NoteSummary]:
    return {summary.id: summary for summary in list_notes()}


def get_note(document_id: str) -> Note | None:
    db_path, _ = _paths()
    tree = block_store.document_tree(db_path, document_id)
    if tree is None:
        return None
    body = block_store.render_markdown(tree["children"])
    return Note(
        id=tree["id"],
        title=tree["title"],
        status=tree["status"],
        tags=tree["tags"],
        created_at=tree["created_at"],
        updated_at=tree["updated_at"],
        snippet=" ".join(body.split())[:160],
        body=body,
    )


# --- Writes ------------------------------------------------------------------


def create(data: NoteCreate, created_at: object | None = None) -> Note:
    """Create a note; raises KeyError when the folder does not exist."""

    db_path, _ = _paths()
    tree = block_store.create_block_document(
        db_path,
        data.title or block_store.derive_title(data.body),
        data.status,
        data.tags,
        data.body,
        folder_id=data.folder_id,
        created_at=created_at,
    )
    write_mirror(tree["id"])
    note = get_note(tree["id"])
    assert note is not None
    return note


def update(document_id: str, data: NoteUpdate) -> Note:
    """Change title, status, tags or (whole-document) body; KeyError if missing."""

    db_path, _ = _paths()
    current = block_store.document_tree(db_path, document_id)
    if current is None:
        raise KeyError("document not found")
    title = data.title if data.title is not None else current["title"]
    status = data.status if data.status is not None else current["status"]
    tags = data.tags if data.tags is not None else current["tags"]
    now = datetime.now(timezone.utc).isoformat()
    if data.body is not None:
        block_store.replace_document_from_markdown(
            db_path, document_id, title, status, tags, data.body, current["created_at"], now
        )
    else:
        block_store.update_document_metadata(db_path, document_id, title, status, tags, now)
    write_mirror(document_id)
    note = get_note(document_id)
    assert note is not None
    return note


def trash(document_id: str) -> None:
    db_path, _ = _paths()
    block_store.delete_document(db_path, document_id)
    remove_mirror(document_id)


def restore(document_id: str) -> dict[str, object]:
    db_path, _ = _paths()
    result = workspace.restore_document(db_path, document_id)
    write_mirror(document_id)
    return result


def purge(document_id: str) -> None:
    db_path, _ = _paths()
    workspace.purge_document(db_path, document_id)
    remove_mirror(document_id)


def empty_trash() -> int:
    db_path, _ = _paths()
    purged = workspace.empty_trash(db_path)
    for document_id in purged:
        remove_mirror(document_id)
    return len(purged)


def move_into(source_id: str, target_id: str) -> int:
    """Fold one note into another (keeping block IDs) and trash the emptied note."""

    db_path, _ = _paths()
    moved = duplicates.move_into(db_path, source_id, target_id)
    write_mirror(target_id)
    trash(source_id)
    return moved


# --- Mirrors -----------------------------------------------------------------


def write_mirror(document_id: str) -> None:
    """Atomically rewrite one note's Markdown mirror from SQLite."""

    db_path, notes_path = _paths()
    mirror_document(db_path, notes_path, document_id)


def mirror_document(db_path: Path, notes_path: Path, document_id: str) -> None:
    tree = block_store.document_tree(db_path, document_id)
    target = notes_path / f"{document_id}.md"
    if tree is None:
        target.unlink(missing_ok=True)
        return
    body = block_store.render_markdown(tree["children"])
    # Keep any extra front matter a person added to the file by hand.
    metadata: dict[str, object] = {}
    if target.is_file():
        try:
            metadata = dict(frontmatter.loads(target.read_text(encoding="utf-8")).metadata)
        except Exception:  # A damaged mirror is simply rewritten.
            metadata = {}
    metadata.update(
        id=tree["id"],
        title=tree["title"],
        status=tree["status"],
        tags=tree["tags"],
        created_at=tree["created_at"],
        updated_at=tree["updated_at"],
        images=images.collect_image_texts(body),
    )
    _atomic_write(target, frontmatter.dumps(frontmatter.Post(body, **metadata)))


def remove_mirror(document_id: str) -> None:
    _, notes_path = _paths()
    (notes_path / f"{document_id}.md").unlink(missing_ok=True)


def rebuild_mirrors(stash: Path) -> None:
    """Make every mirror match SQLite after a restore.

    Mirrors for documents SQLite no longer has are moved into ``stash`` rather
    than deleted, so nothing on disk is lost.
    """

    db_path, notes_path = _paths()
    active = {str(item["id"]) for item in block_store.list_documents(db_path)}
    for mirror in notes_path.glob("*.md"):
        if mirror.stem not in active:
            stash.mkdir(parents=True, exist_ok=True)
            mirror.replace(stash / mirror.name)
    for document_id in active:
        write_mirror(document_id)


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
