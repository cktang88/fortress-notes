"""Shared test helpers: settings over a temporary notes folder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app import config
from app.config import Settings

# Off unless a test turns them on: no model, no OCR, no daily snapshot, no LLM key.
DEFAULTS = {
    "block_db_import_on_startup": False,
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

    from app.documents import MirrorWriter

    return MirrorWriter().path(Path(notes), document_id) or Path(notes) / f".no-mirror-{document_id}.md"


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
