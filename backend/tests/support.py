"""Shared test helpers: settings over a temporary notes folder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app import config
from app.config import Settings

# Off unless a test turns them on: no model, no OCR, no daily snapshot, no LLM key.
DEFAULTS = {
    "welcome_note": False,
    "embeddings_enabled": False,
    "vision_enabled": False,
    "auto_backup_enabled": False,
    "reindex_interval_s": 3600.0,
    "openrouter_api_key": "",
}


def make_settings(notes: Path, **overrides) -> Settings:
    """Settings for a notes folder, ignoring backend/.env (its folders are created)."""

    settings = Settings(_env_file=None, notes_dir=Path(notes), **{**DEFAULTS, **overrides})
    settings.create_directories()
    return settings


def use_settings(settings: Settings):
    """Make ``settings`` the active ones (a patcher: ``with`` or ``start``/``stop``)."""

    return patch.object(config, "_active", settings)


def mirror_file(notes: Path, document_id: str) -> Path:
    """A note's vault mirror, where the mirror writer last put it (read from disk).

    A note without one (never written, or trashed) gets a path that doesn't exist.
    """

    import json

    from app.documents import MIRROR_FILES

    try:
        files = json.loads((Path(notes) / MIRROR_FILES).read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        files = {}
    relative = files.get(document_id)
    return Path(notes) / relative if relative else Path(notes) / f".no-mirror-{document_id}.md"


def import_details(database: Path, document_id: str) -> dict:
    """What an importer recorded about a note (``sources.import_details``)."""

    from app import sources
    from app.block_store import connection_scope

    with connection_scope(database) as connection:
        row = connection.execute(
            "SELECT provenance_json FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return sources.import_details(row[0])


def add_markdown_notes(notes: Path, database: Path) -> None:
    """Each ``*.md`` file in ``notes`` as a note whose id is the file name.

    Front matter gives the title and dates (a test's way to write notes).
    """

    import frontmatter

    from app.block_store import create_document, derive_title, utc_now

    for path in sorted(Path(notes).glob("*.md")):
        post = frontmatter.load(path)
        meta = post.metadata
        create_document(
            database, path.stem, str(meta.get("title") or derive_title(post.content)),
            post.content,
            meta.get("created_at", utc_now()), meta.get("updated_at", utc_now()),
        )


def import_markdown_files(database: Path, root: Path) -> list[str]:
    """The Markdown files under ``root`` uploaded together, as the upload API reads them;
    their new notes' ids."""

    from app import block_store
    from app.block_store import create_document
    from app.portability import parse_uploaded_batch, parse_uploaded_markdown

    block_store.initialize(database)
    paths = sorted(root.rglob("*.md"))
    notes = [parse_uploaded_markdown(path.relative_to(root).as_posix(), path.read_bytes()) for path in paths]
    ids = []
    for note, (document_id, specs) in zip(notes, parse_uploaded_batch(database, notes, block_store.new_id)):
        create_document(database, document_id, note.title, note.body,
                        note.created_at or "t", "t", source_kind="markdown_import", specs=specs)
        ids.append(document_id)
    return ids
