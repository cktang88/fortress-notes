"""Automatic and on-demand snapshots of the SQLite workspace, plus restore.

Snapshots live in ``NOTES_DIR/.fortress-backups``. Restoring always takes a
``pre-restore`` snapshot first, so a restore can itself be undone.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import block_store

BACKUP_DIRNAME = ".fortress-backups"
_NAME_RE = re.compile(r"^fortress-(\d{8}T\d{6}\d{6}Z)-(auto|manual|pre-restore)\.sqlite3$")
KEEP = {"auto": 7, "manual": 10, "pre-restore": 5}
DAILY = timedelta(hours=24)


def backup_directory(notes_path: Path) -> Path:
    return notes_path / BACKUP_DIRNAME


def create_backup(db_path: Path, directory: Path, kind: str = "manual") -> Path:
    if kind not in KEEP:
        raise ValueError(f"unknown backup kind {kind!r}")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"fortress-{stamp}-{kind}.sqlite3"
    block_store.backup_database(db_path, destination)
    _prune(directory, kind)
    return destination


def ensure_daily_backup(db_path: Path, directory: Path, now: datetime | None = None) -> Path | None:
    """Take an ``auto`` snapshot when the newest one is older than a day."""

    now = now or datetime.now(timezone.utc)
    latest = next((b for b in list_backups(directory) if b["kind"] == "auto"), None)
    if latest is not None and now - datetime.fromisoformat(latest["created_at"]) < DAILY:
        return None
    return create_backup(db_path, directory, "auto")


def list_backups(directory: Path) -> list[dict[str, object]]:
    if not directory.is_dir():
        return []
    items: list[dict[str, object]] = []
    for path in directory.iterdir():
        match = _NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(
            tzinfo=timezone.utc
        )
        items.append(
            {
                "name": path.name,
                "kind": match.group(2),
                "created_at": created.isoformat(),
                "size": path.stat().st_size,
            }
        )
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


def resolve_backup(directory: Path, name: str) -> Path:
    """Return a snapshot path, refusing anything that is not a listed snapshot."""

    if not _NAME_RE.match(name):
        raise KeyError("backup not found")
    path = directory / name
    if not path.is_file():
        raise KeyError("backup not found")
    return path


def restore_backup(db_path: Path, directory: Path, name: str) -> Path:
    """Replace the live database with a verified snapshot; returns the safety copy."""

    source = resolve_backup(directory, name)
    _verify_snapshot(source)
    safety = create_backup(db_path, directory, "pre-restore")
    with closing(sqlite3.connect(source)) as snapshot, block_store.connection_scope(
        db_path
    ) as live:
        snapshot.backup(live)
    # Older snapshots may predate later migrations; bring them up to date.
    block_store.initialize(db_path)
    block_store.rebuild_fts(db_path)
    return safety


def _verify_snapshot(path: Path) -> None:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"backup is not a readable Fortress database: {exc}") from exc
    if check is None or check[0] != "ok":
        raise ValueError("backup failed its integrity check")
    if version is None or version[0] is None or version[0] > block_store.SCHEMA_VERSION:
        raise ValueError("backup was made by a newer version of Fortress Notes")


def _prune(directory: Path, kind: str) -> None:
    same_kind = [item for item in list_backups(directory) if item["kind"] == kind]
    for item in same_kind[KEEP[kind]:]:
        (directory / str(item["name"])).unlink(missing_ok=True)
