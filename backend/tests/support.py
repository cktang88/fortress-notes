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
