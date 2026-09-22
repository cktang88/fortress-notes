"""SQLite foundation for the block-centric workspace.

The current note API still reads Markdown. This module creates the new canonical
shape and imports existing Markdown once, so later API/UI work can switch over
without making the first migration destructive.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from markdown_it import MarkdownIt
from markdown_it.token import Token
from ulid import ULID

from .models import BlockOperation

SCHEMA_VERSION = 4
DB_FILENAME = ".fortress.sqlite3"

_TASK_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+\[([ xX])\]\s+")
_BLOCK_REF_RE = re.compile(r"\(\(\s*([A-Za-z0-9_-]+)(?:\s+[\"']([^\"']*)[\"'])?\s*\)\)")
_DOCUMENT_REF_RE = re.compile(r"\[\[([A-Za-z0-9_-]+)(?:\|([^\]]+))?\]\]")


@dataclass(frozen=True)
class ImportedBlock:
    id: str
    type: str
    source: str
    attrs: dict[str, object] = field(default_factory=dict)
    children: list["ImportedBlock"] = field(default_factory=list)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('rough', 'polished')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    folder_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS folders_parent_position ON folders(parent_id, position);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (document_id, parent_id, position)
);

CREATE INDEX IF NOT EXISTS blocks_document_position
    ON blocks(document_id, parent_id, position);

CREATE TABLE IF NOT EXISTS block_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);

CREATE TABLE IF NOT EXISTS block_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_block_id, label)
);

CREATE TABLE IF NOT EXISTS document_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_document_id, label)
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_id TEXT,
    operation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    block_id UNINDEXED,
    document_id UNINDEXED,
    text
);
"""

_MIGRATIONS = {
    2: """
    CREATE TABLE IF NOT EXISTS document_refs (
        source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
        target_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        label TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source_block_id, target_document_id, label)
    );
    """,
    3: """
    CREATE TABLE IF NOT EXISTS folders (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS folders_parent_position ON folders(parent_id, position);
    CREATE INDEX IF NOT EXISTS documents_folder_position ON documents(folder_id, position);
    """,
    4: "",
}

_ready_paths: set[Path] = set()
UNSET = object()


def database_path(notes_path: Path) -> Path:
    return notes_path / DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def connection_scope(path: Path):
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path: Path) -> None:
    with connection_scope(path) as connection:
        connection.executescript(_SCHEMA)
        row = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )
        elif row["version"] > SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported block database version {row['version']} (expected {SCHEMA_VERSION})"
            )
        else:
            current = int(row["version"])
            while current < SCHEMA_VERSION:
                current += 1
                _apply_migration(connection, current)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (current, _now()),
                )
    _ready_paths.add(path.resolve())


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    if version == 3:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "folder_id" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN folder_id TEXT REFERENCES folders(id) ON DELETE RESTRICT"
            )
        if "position" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
        if "deleted_at" not in columns:
            connection.execute("ALTER TABLE documents ADD COLUMN deleted_at TEXT")
        connection.execute(
            """WITH ordered AS (
                   SELECT id,
                          ROW_NUMBER() OVER (
                              PARTITION BY folder_id ORDER BY updated_at ASC, id ASC
                          ) - 1 AS sibling_position
                     FROM documents
                    WHERE deleted_at IS NULL
               )
               UPDATE documents
                  SET position = (
                      SELECT sibling_position FROM ordered WHERE ordered.id = documents.id
                  )
                WHERE deleted_at IS NULL"""
        )
    if version == 4:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "revision" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
    connection.executescript(_MIGRATIONS[version])


def is_ready(path: Path) -> bool:
    return path.resolve() in _ready_paths


def bootstrap_markdown(notes_path: Path, path: Path) -> int:
    """Import Markdown files that are not already represented in SQLite.

    This is intentionally insert-only. Editing or deleting existing Markdown does
    not mutate the new store until the block API owns writes in a later phase.
    """

    initialize(path)
    imported = 0
    with connection_scope(path) as connection:
        for note_path in sorted(notes_path.glob("*.md")):
            note_id = note_path.stem
            if connection.execute(
                "SELECT 1 FROM documents WHERE id = ?", (note_id,)
            ).fetchone():
                continue
            post = frontmatter.load(note_path)
            now = _now()
            created_at = _iso(post.metadata.get("created_at", now))
            updated_at = _iso(post.metadata.get("updated_at", now))
            title = str(post.metadata.get("title") or _derive_title(post.content))
            status = post.metadata.get("status", "rough")
            if status not in {"rough", "polished"}:
                status = "rough"
            tags = post.metadata.get("tags", []) or []
            _insert_document(
                connection,
                note_id,
                title,
                status,
                tags,
                post.content,
                created_at,
                updated_at,
            )
            imported += 1
    return imported


def create_document(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: object,
    updated_at: object,
) -> None:
    """Create the block representation for a newly created compatibility note."""

    initialize(path)
    with connection_scope(path) as connection:
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone():
            return
        _insert_document(
            connection,
            document_id,
            title,
            status,
            tags,
            body,
            _iso(created_at),
            _iso(updated_at),
        )


def list_documents(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at,
                      COALESCE((SELECT text FROM blocks WHERE blocks.document_id = documents.id
                                ORDER BY parent_id, position, id LIMIT 1), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC, id ASC"""
        ).fetchall()
    return [_navigation_document(row) for row in rows]


def create_block_document(
    path: Path, title: str, status: str, tags: list[str], body: str
) -> dict:
    document_id = str(ULID())
    now = _now()
    create_document(path, document_id, title, status, tags, body, now, now)
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree


def rename_document(path: Path, document_id: str, title: str) -> dict:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            """UPDATE documents SET title = ?, updated_at = ?
                 WHERE id = ? AND deleted_at IS NULL""",
            (title, _now(), document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree


def update_document_metadata(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    updated_at: object,
) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            """UPDATE documents
               SET title = ?, status = ?, tags_json = ?, updated_at = ?
               WHERE id = ? AND deleted_at IS NULL""",
            (title, status, json.dumps(tags), _iso(updated_at), document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")


def replace_document_from_markdown(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: object,
    updated_at: object,
) -> None:
    """Re-import the explicit raw/compatibility edit into the block store."""

    initialize(path)
    with connection_scope(path) as connection:
        existing = connection.execute(
            "SELECT created_at FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if existing is None:
            _insert_document(
                connection,
                document_id,
                title,
                status,
                tags,
                body,
                _iso(created_at),
                _iso(updated_at),
            )
            return
        connection.execute(
            """UPDATE documents
               SET title = ?, status = ?, tags_json = ?, updated_at = ?
               WHERE id = ?""",
            (title, status, json.dumps(tags), _iso(updated_at), document_id),
        )
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM blocks WHERE document_id = ?", (document_id,))
        _insert_blocks(connection, document_id, body, existing["created_at"], _iso(updated_at))


def delete_document(path: Path, document_id: str) -> None:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        cursor = connection.execute(
            "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")


def create_folder(
    path: Path, name: str, parent_id: str | None = None, position: int | None = None
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        _validate_folder_parent(connection, parent_id)
        target_position = _folder_insert_position(connection, parent_id, position)
        folder_id = str(ULID())
        connection.execute(
            """INSERT INTO folders(id, parent_id, name, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (folder_id, parent_id, name, target_position, now, now),
        )
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def rename_folder(path: Path, folder_id: str, name: str) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        cursor = connection.execute(
            "UPDATE folders SET name = ?, updated_at = ? WHERE id = ?",
            (name, now, folder_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("folder not found")
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def move_folder(
    path: Path,
    folder_id: str,
    parent_id: str | None | object = UNSET,
    position: int | None = None,
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        folder = _folder_row(connection, folder_id)
        target_parent = folder["parent_id"] if parent_id is UNSET else parent_id
        _validate_folder_parent(connection, target_parent)
        if target_parent == folder_id or _folder_is_descendant(connection, folder_id, target_parent):
            raise ValueError("cannot move a folder into itself or its descendant")
        _shift_folder_positions(connection, folder["parent_id"], folder["position"] + 1, -1)
        target_position = _folder_insert_position(
            connection, target_parent, position, exclude_id=folder_id
        )
        connection.execute(
            """UPDATE folders
                  SET parent_id = ?, position = ?, updated_at = ?
                WHERE id = ?""",
            (target_parent, target_position, now, folder_id),
        )
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def delete_folder(path: Path, folder_id: str) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        _folder_row(connection, folder_id)
        if connection.execute(
            "SELECT 1 FROM folders WHERE parent_id = ? LIMIT 1", (folder_id,)
        ).fetchone() or connection.execute(
            "SELECT 1 FROM documents WHERE folder_id = ? AND deleted_at IS NULL LIMIT 1",
            (folder_id,),
        ).fetchone():
            raise ValueError("folder is not empty")
        folder = _folder_row(connection, folder_id)
        connection.execute(
            "UPDATE documents SET folder_id = NULL WHERE folder_id = ? AND deleted_at IS NOT NULL",
            (folder_id,),
        )
        connection.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        _shift_folder_positions(connection, folder["parent_id"], folder["position"] + 1, -1)


def move_document(
    path: Path,
    document_id: str,
    folder_id: str | None | object = UNSET,
    position: int | None = None,
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        document = connection.execute(
            """SELECT id, folder_id, position FROM documents
                 WHERE id = ? AND deleted_at IS NULL""",
            (document_id,),
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        target_folder = document["folder_id"] if folder_id is UNSET else folder_id
        _validate_folder_parent(connection, target_folder)
        _shift_document_positions(
            connection, document["folder_id"], document["position"] + 1, -1
        )
        target_position = _document_insert_position(
            connection, target_folder, position, exclude_id=document_id
        )
        connection.execute(
            """UPDATE documents
                  SET folder_id = ?, position = ?, updated_at = ?
                WHERE id = ?""",
            (target_folder, target_position, now, document_id),
        )
        row = connection.execute(
            "SELECT id, folder_id, position FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return {"id": row["id"], "folder_id": row["folder_id"], "position": row["position"]}


def navigation(path: Path, recent_limit: int = 10) -> dict[str, object]:
    """Return the canonical folder tree and recently edited documents."""

    initialize(path)
    with connection_scope(path) as connection:
        folders = connection.execute(
            """SELECT id, parent_id, name, position, created_at, updated_at
                 FROM folders
                ORDER BY parent_id, position, id"""
        ).fetchall()
        documents = connection.execute(
            """SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id
                           ORDER BY parent_id, position, id LIMIT 1
                      ), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY folder_id, position, id"""
        ).fetchall()
        recent = connection.execute(
            """SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id
                           ORDER BY parent_id, position, id LIMIT 1
                      ), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC, id ASC
                LIMIT ?""",
            (max(1, min(recent_limit, 50)),),
        ).fetchall()

    documents_by_folder: dict[str | None, list[dict[str, object]]] = {}
    for row in documents:
        item = _navigation_document(row)
        documents_by_folder.setdefault(row["folder_id"], []).append(item)

    folders_by_parent: dict[str | None, list[sqlite3.Row]] = {}
    for row in folders:
        folders_by_parent.setdefault(row["parent_id"], []).append(row)

    def children(parent_id: str | None) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for folder in folders_by_parent.get(parent_id, []):
            items.append(
                {
                    "kind": "folder",
                    "id": folder["id"],
                    "parent_id": folder["parent_id"],
                    "name": folder["name"],
                    "position": folder["position"],
                    "created_at": folder["created_at"],
                    "updated_at": folder["updated_at"],
                    "children": children(folder["id"]),
                }
            )
        items.extend(documents_by_folder.get(parent_id, []))
        return items

    return {
        "items": children(None),
        "recent": [_navigation_document(row) for row in recent],
    }


def _folder_row(connection: sqlite3.Connection, folder_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise KeyError("folder not found")
    return row


def _folder_data(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_folder_parent(connection: sqlite3.Connection, parent_id: str | None) -> None:
    if parent_id is not None:
        _folder_row(connection, parent_id)


def _folder_is_descendant(
    connection: sqlite3.Connection, folder_id: str, candidate_parent_id: str | None
) -> bool:
    parent_id = candidate_parent_id
    while parent_id is not None:
        if parent_id == folder_id:
            return True
        parent_id = _folder_row(connection, parent_id)["parent_id"]
    return False


def _folder_insert_position(
    connection: sqlite3.Connection,
    parent_id: str | None,
    position: int | None,
    exclude_id: str | None = None,
) -> int:
    count = connection.execute(
        "SELECT COUNT(*) FROM folders WHERE parent_id IS ? AND id IS NOT ?",
        (parent_id, exclude_id),
    ).fetchone()[0]
    target_position = count if position is None else min(position, count)
    _shift_folder_positions(connection, parent_id, target_position, 1)
    return target_position


def _shift_folder_positions(
    connection: sqlite3.Connection, parent_id: str | None, from_position: int, delta: int
) -> None:
    connection.execute(
        """UPDATE folders SET position = position + ?
           WHERE parent_id IS ? AND position >= ?""",
        (delta, parent_id, from_position),
    )


def _document_insert_position(
    connection: sqlite3.Connection,
    folder_id: str | None,
    position: int | None,
    exclude_id: str | None = None,
) -> int:
    count = connection.execute(
        """SELECT COUNT(*) FROM documents
           WHERE folder_id IS ? AND deleted_at IS NULL AND id IS NOT ?""",
        (folder_id, exclude_id),
    ).fetchone()[0]
    target_position = count if position is None else min(position, count)
    _shift_document_positions(connection, folder_id, target_position, 1)
    return target_position


def _shift_document_positions(
    connection: sqlite3.Connection, folder_id: str | None, from_position: int, delta: int
) -> None:
    connection.execute(
        """UPDATE documents SET position = position + ?
           WHERE folder_id IS ? AND deleted_at IS NULL AND position >= ?""",
        (delta, folder_id, from_position),
    )


def backup_database(path: Path, destination: Path) -> None:
    """Create a consistent live snapshot using Python's SQLite backup API."""

    initialize(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connection_scope(path) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        target.commit()


def check_integrity(path: Path) -> bool:
    initialize(path)
    with connection_scope(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    return result is not None and result[0] == "ok"


def fts_is_consistent(path: Path) -> bool:
    """Check that FTS contains exactly the active canonical blocks."""

    initialize(path)
    with connection_scope(path) as connection:
        missing = connection.execute(
            """SELECT 1
                 FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                 LEFT JOIN blocks_fts ON blocks_fts.block_id = blocks.id
                WHERE documents.deleted_at IS NULL AND blocks_fts.block_id IS NULL
                LIMIT 1"""
        ).fetchone()
        stale = connection.execute(
            """SELECT 1
                 FROM blocks_fts
                 LEFT JOIN blocks ON blocks.id = blocks_fts.block_id
                 LEFT JOIN documents ON documents.id = blocks_fts.document_id
                WHERE blocks.id IS NULL OR documents.deleted_at IS NOT NULL
                LIMIT 1"""
        ).fetchone()
    return missing is None and stale is None


def rebuild_fts(path: Path) -> None:
    """Rebuild the disposable FTS rows from active canonical blocks."""

    initialize(path)
    with connection_scope(path) as connection:
        connection.execute("DELETE FROM blocks_fts")
        connection.execute(
            """INSERT INTO blocks_fts(block_id, document_id, text)
                 SELECT blocks.id, blocks.document_id, blocks.text
                   FROM blocks
                   JOIN documents ON documents.id = blocks.document_id
                  WHERE documents.deleted_at IS NULL"""
        )


def ensure_fts_integrity(path: Path) -> bool:
    """Repair FTS if needed and report whether the index is consistent afterward."""

    if not fts_is_consistent(path):
        rebuild_fts(path)
    return fts_is_consistent(path)


def search_blocks(path: Path, query: str, limit: int = 50) -> list[dict[str, object]]:
    """Search canonical block text with SQLite FTS5 and return block context."""

    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not terms:
        return []
    match = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT blocks_fts.block_id, blocks_fts.document_id, documents.title,
                      blocks.type, blocks.text, bm25(blocks_fts) AS rank
                FROM blocks_fts
                 JOIN blocks ON blocks.id = blocks_fts.block_id
                 JOIN documents ON documents.id = blocks_fts.document_id
                WHERE blocks_fts MATCH ? AND documents.deleted_at IS NULL
                ORDER BY rank, blocks.updated_at DESC
                LIMIT ?""",
            (match, max(1, min(limit, 200))),
        ).fetchall()
    return [
        {
            "block_id": row["block_id"],
            "document_id": row["document_id"],
            "document_title": row["title"],
            "block_type": row["type"],
            "text": row["text"],
            "score": -float(row["rank"]),
        }
        for row in rows
    ]


def block_link_targets(path: Path, query: str = "", limit: int = 50) -> list[dict[str, object]]:
    """Return stable block IDs suitable for reference autocomplete."""

    initialize(path)
    normalized_query = query.strip()
    if not normalized_query:
        return []
    escaped_query = (
        normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    needle = f"%{escaped_query}%"
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT blocks.id, blocks.document_id, documents.title,
                      blocks.type, blocks.text
                 FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                WHERE documents.deleted_at IS NULL
                  AND (
                    blocks.text LIKE ? COLLATE NOCASE ESCAPE '\\'
                   OR (
                        (documents.title LIKE ? COLLATE NOCASE ESCAPE '\\'
                         OR documents.id LIKE ? COLLATE NOCASE ESCAPE '\\')
                        AND blocks.id = (
                            SELECT first_block.id
                              FROM blocks AS first_block
                             WHERE first_block.document_id = documents.id
                             ORDER BY first_block.position ASC, first_block.id ASC
                             LIMIT 1
                        )
                   )
                  )
                ORDER BY documents.updated_at DESC, blocks.updated_at DESC,
                         blocks.position ASC, documents.id ASC, blocks.id ASC
                LIMIT ?""",
            (needle, needle, needle, max(1, min(limit, 200))),
        ).fetchall()
    return [
        {
            "block_id": row["id"],
            "document_id": row["document_id"],
            "document_title": row["title"],
            "block_type": row["type"],
            "text": row["text"],
        }
        for row in rows
    ]


def document_backlinks(path: Path, document_id: str, limit: int = 100) -> list[dict[str, object]]:
    """Return block and document references pointing into one document."""

    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT source_block_id, source_document_id, source_document_title,
                      source_text, label, target_block_id
                 FROM (
                    SELECT refs.source_block_id, source.document_id AS source_document_id,
                           source_doc.title AS source_document_title, source.text AS source_text,
                           refs.label, refs.target_block_id, source.updated_at AS updated_at
                      FROM block_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                      JOIN documents AS source_doc ON source_doc.id = source.document_id
                      JOIN blocks AS target ON target.id = refs.target_block_id
                      JOIN documents AS target_doc ON target_doc.id = target.document_id
                     WHERE target.document_id = ?
                       AND source_doc.deleted_at IS NULL
                       AND target_doc.deleted_at IS NULL
                     UNION ALL
                    SELECT refs.source_block_id, source.document_id AS source_document_id,
                           source_doc.title AS source_document_title, source.text AS source_text,
                           refs.label, NULL AS target_block_id, source.updated_at AS updated_at
                      FROM document_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                      JOIN documents AS source_doc ON source_doc.id = source.document_id
                      JOIN documents AS target_doc ON target_doc.id = refs.target_document_id
                     WHERE refs.target_document_id = ?
                       AND source_doc.deleted_at IS NULL
                       AND target_doc.deleted_at IS NULL
                 )
                ORDER BY updated_at DESC
                LIMIT ?""",
            (document_id, document_id, max(1, min(limit, 500))),
        ).fetchall()
    return [
        {
            "source_block_id": row["source_block_id"],
            "source_document_id": row["source_document_id"],
            "source_document_title": row["source_document_title"],
            "source_text": row["source_text"],
            "label": row["label"],
            "target_block_id": row["target_block_id"],
        }
        for row in rows
    ]


def sync_markdown(notes_path: Path, path: Path, document_id: str) -> None:
    """Refresh the old Markdown file as a compatibility mirror of SQLite."""

    tree = document_tree(path, document_id)
    if tree is None:
        raise KeyError("document not found")
    note_path = notes_path / f"{document_id}.md"
    if not note_path.exists():
        return
    post = frontmatter.load(note_path)
    post.content = _render_markdown(tree["children"])
    post.metadata.update(
        {
            "title": tree["title"],
            "status": tree["status"],
            "tags": tree["tags"],
            "created_at": tree["created_at"],
            "updated_at": tree["updated_at"],
        }
    )
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _insert_document(
    connection: sqlite3.Connection,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: str,
    updated_at: str,
) -> None:
    position = connection.execute(
        """SELECT COALESCE(MAX(position) + 1, 0)
             FROM documents
            WHERE folder_id IS NULL AND deleted_at IS NULL"""
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO documents(
                   id, title, status, tags_json, folder_id, position, deleted_at,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)""",
        (document_id, title, status, json.dumps(tags), position, created_at, updated_at),
    )
    _insert_blocks(connection, document_id, body, created_at, updated_at)


def _navigation_document(row: sqlite3.Row) -> dict[str, object]:
    return {
        "kind": "document",
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "tags": json.loads(row["tags_json"]),
        "folder_id": row["folder_id"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "snippet": row["snippet"][:160],
    }


def _insert_blocks(
    connection: sqlite3.Connection,
    document_id: str,
    body: str,
    created_at: str,
    updated_at: str,
) -> None:
    def insert(block: ImportedBlock, parent_id: str | None, position: int) -> None:
        connection.execute(
            """INSERT INTO blocks(
                   id, document_id, parent_id, position, type, attrs_json,
                   content_json, text, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                block.id,
                document_id,
                parent_id,
                position,
                block.type,
                json.dumps(block.attrs),
                json.dumps({"markdown": block.source}),
                block.source,
                created_at,
                updated_at,
            ),
        )
        connection.execute(
            "INSERT INTO blocks_fts(block_id, document_id, text) VALUES (?, ?, ?)",
            (block.id, document_id, block.source),
        )
        for child_position, child in enumerate(block.children):
            insert(child, block.id, child_position)

    for position, block in enumerate(_parse_blocks(body)):
        insert(block, None, position)
    _rebuild_document_references(connection, document_id)


def _rebuild_document_references(connection: sqlite3.Connection, document_id: str) -> None:
    source_ids = connection.execute(
        "SELECT id FROM blocks WHERE document_id = ?", (document_id,)
    ).fetchall()
    for row in source_ids:
        _replace_references(connection, row["id"], document_id)


def _replace_references(
    connection: sqlite3.Connection, source_block_id: str, document_id: str
) -> None:
    row = connection.execute(
        "SELECT content_json, text FROM blocks WHERE id = ? AND document_id = ?",
        (source_block_id, document_id),
    ).fetchone()
    if row is None:
        return
    connection.execute("DELETE FROM block_refs WHERE source_block_id = ?", (source_block_id,))
    connection.execute("DELETE FROM document_refs WHERE source_block_id = ?", (source_block_id,))
    haystack = f"{row['text']}\n{row['content_json']}"
    block_targets = {
        match.group(1): match.group(2) or "" for match in _BLOCK_REF_RE.finditer(haystack)
    }
    document_targets = {
        match.group(1): match.group(2) or "" for match in _DOCUMENT_REF_RE.finditer(haystack)
    }
    for target_id, label in block_targets.items():
        if target_id == source_block_id:
            continue
        if connection.execute(
            """SELECT 1 FROM blocks
                JOIN documents ON documents.id = blocks.document_id
               WHERE blocks.id = ? AND documents.deleted_at IS NULL""",
            (target_id,),
        ).fetchone():
            connection.execute(
                "INSERT OR IGNORE INTO block_refs(source_block_id, target_block_id, label) VALUES (?, ?, ?)",
                (source_block_id, target_id, label),
            )
    for target_id, label in document_targets.items():
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NULL", (target_id,)
        ).fetchone():
            connection.execute(
                "INSERT OR IGNORE INTO document_refs(source_block_id, target_document_id, label) VALUES (?, ?, ?)",
                (source_block_id, target_id, label),
            )


def _render_markdown(nodes: list[dict], depth: int = 0) -> str:
    fragments: list[str] = []
    for node in nodes:
        source = _node_markdown(node)
        if node["type"] == "divider" and not source:
            source = "---"
        if depth and source:
            prefix = "  " * depth
            source = "\n".join(
                f"{prefix}{line}" if line else line for line in source.splitlines()
            )
        if source:
            fragments.append(source)
        children = node.get("children", [])
        if children:
            rendered_children = _render_markdown(children, depth + 1)
            if rendered_children:
                fragments.append(rendered_children)
    return "\n\n".join(fragments)


def _node_markdown(node: dict) -> str:
    content = node.get("content", {})
    if isinstance(content, dict):
        markdown = content.get("markdown")
        if isinstance(markdown, str):
            return markdown
        native = content.get("blocknote")
        if isinstance(native, str):
            return native
        return _inline_text(native)
    return str(node.get("text", ""))


def _inline_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(_inline_text(item.get("content")))
    return "".join(parts)


def document_tree(path: Path, document_id: str) -> dict | None:
    initialize(path)
    with connection_scope(path) as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if document is None:
            return None
        rows = connection.execute(
            """SELECT id, document_id, parent_id, position, type, attrs_json,
                      content_json, text, created_at, updated_at
                 FROM blocks WHERE document_id = ?
                 ORDER BY parent_id, position""",
            (document_id,),
        ).fetchall()
    nodes = {
        row["id"]: {
            "id": row["id"],
            "document_id": row["document_id"],
            "parent_id": row["parent_id"],
            "position": row["position"],
            "type": row["type"],
            "attrs": json.loads(row["attrs_json"]),
            "content": json.loads(row["content_json"]),
            "text": row["text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "children": [],
        }
        for row in rows
    }
    roots: list[dict] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return {
        "id": document["id"],
        "title": document["title"],
        "status": document["status"],
        "tags": json.loads(document["tags_json"]),
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "revision": document["revision"],
        "children": roots,
    }


def document_subtree(path: Path, document_id: str, block_id: str | None = None) -> dict | None:
    tree = document_tree(path, document_id)
    if tree is None:
        return None

    def find(nodes: list[dict], target: str) -> dict | None:
        for node in nodes:
            if node["id"] == target:
                return node
            found = find(node["children"], target)
            if found is not None:
                return found
        return None

    if block_id is None:
        root = {"id": None, "parent": None, "children": tree["children"]}
        return {"document": tree, "parent": None, "depth": 0, "breadcrumbs": [], "subtree": root}
    node = find(tree["children"], block_id)
    if node is None:
        raise KeyError("block not found")
    by_id: dict[str, dict] = {}
    stack = [(child, None) for child in tree["children"]]
    while stack:
        current, parent = stack.pop()
        by_id[current["id"]] = {"node": current, "parent": parent}
        stack.extend((child, current["id"]) for child in current["children"])
    breadcrumbs = []
    current_id: str | None = block_id
    while current_id is not None:
        current = by_id[current_id]["node"]
        breadcrumbs.append({"id": current["id"], "type": current["type"], "text": current["text"]})
        current_id = by_id[current_id]["parent"]
    breadcrumbs.reverse()
    depth = len(breadcrumbs) - 1
    parent_id = by_id[block_id]["parent"]
    parent = by_id[parent_id]["node"] if parent_id else None
    return {
        "document": tree,
        "parent": parent,
        "children": node["children"],
        "depth": depth,
        "breadcrumbs": breadcrumbs,
        "subtree": node,
    }


class DocumentRevisionConflict(RuntimeError):
    """Raised when a transaction was composed from an older document version."""

    def __init__(self, document_id: str, base_revision: int, current_revision: int):
        self.document_id = document_id
        self.base_revision = base_revision
        self.current_revision = current_revision
        super().__init__(
            f"document revision conflict: expected {base_revision}, "
            f"current revision is {current_revision}"
        )

    def detail(self) -> dict[str, int | str]:
        return {
            "code": "document_revision_conflict",
            "document_id": self.document_id,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
        }


def apply_transaction(
    path: Path,
    document_id: str,
    operations: list[BlockOperation],
    base_revision: int,
) -> dict:
    """Apply operations for ``base_revision`` atomically and return the new tree."""

    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        # Acquire the SQLite writer lock before reading the version. Without this,
        # two writers could both read the same revision before either starts a write.
        connection.execute("BEGIN IMMEDIATE")
        document = connection.execute(
            "SELECT revision FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        if base_revision != document["revision"]:
            raise DocumentRevisionConflict(
                document_id, base_revision, document["revision"]
            )
        for operation in operations:
            _apply_operation(connection, document_id, operation, now)
        _rebuild_document_references(connection, document_id)
        connection.execute(
            "UPDATE documents SET updated_at = ?, revision = revision + 1 WHERE id = ?",
            (now, document_id),
        )
    tree = document_tree(path, document_id)
    if tree is None:  # pragma: no cover - protected by the transaction above
        raise KeyError("document not found")
    return tree


def _apply_operation(
    connection: sqlite3.Connection,
    document_id: str,
    operation: BlockOperation,
    now: str,
) -> None:
    if operation.operation == "insert":
        _insert_block(connection, document_id, operation, now)
        return
    if not operation.block_id:
        raise ValueError(f"{operation.operation} requires block_id")
    row = connection.execute(
        "SELECT * FROM blocks WHERE id = ? AND document_id = ?",
        (operation.block_id, document_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"block not found: {operation.block_id}")
    if (
        operation.expected_updated_at is not None
        and operation.expected_updated_at != row["updated_at"]
    ):
        raise RuntimeError(f"block changed since {operation.expected_updated_at}")
    if operation.operation == "update":
        attrs = operation.attrs if operation.attrs is not None else json.loads(row["attrs_json"])
        content = (
            operation.content
            if operation.content is not None
            else json.loads(row["content_json"])
        )
        text = operation.text if operation.text is not None else row["text"]
        connection.execute(
            """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, text = ?,
                      updated_at = ? WHERE id = ?""",
            (
                operation.type if operation.type is not None else row["type"],
                json.dumps(attrs),
                json.dumps(content),
                text,
                now,
                operation.block_id,
            ),
        )
        _replace_attrs(connection, operation.block_id, attrs)
        _refresh_fts(connection, operation.block_id, document_id, text)
    elif operation.operation == "set_attrs":
        attrs = operation.attrs if operation.attrs is not None else {}
        connection.execute(
            "UPDATE blocks SET attrs_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(attrs), now, operation.block_id),
        )
        _replace_attrs(connection, operation.block_id, attrs)
    elif operation.operation == "move":
        _move_block(connection, document_id, row, operation, now)
    elif operation.operation == "delete":
        _delete_block(connection, document_id, row)
    elif operation.operation == "duplicate":
        _duplicate_block(connection, document_id, row, operation, now)
    elif operation.operation == "split":
        _split_block(connection, document_id, row, operation, now)
    elif operation.operation == "merge":
        _merge_block(connection, document_id, row, now)
    else:  # pragma: no cover - Pydantic validates operation values
        raise ValueError(f"unsupported operation: {operation.operation}")
    connection.execute(
        """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (document_id, operation.block_id, operation.model_dump_json(), now),
    )


def _insert_block(
    connection: sqlite3.Connection,
    document_id: str,
    operation: BlockOperation,
    now: str,
) -> None:
    block_id = operation.block_id or str(ULID())
    parent_id = operation.parent_id
    _validate_parent(connection, document_id, parent_id)
    position = _next_position(connection, document_id, parent_id)
    if operation.position is not None:
        position = operation.position
        _shift_positions(connection, document_id, parent_id, position, 1)
    attrs = operation.attrs or {}
    content = operation.content or {}
    text = operation.text or ""
    connection.execute(
        """INSERT INTO blocks(
               id, document_id, parent_id, position, type, attrs_json, content_json,
               text, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            block_id,
            document_id,
            parent_id,
            position,
            operation.type or "paragraph",
            json.dumps(attrs),
            json.dumps(content),
            text,
            now,
            now,
        ),
    )
    _replace_attrs(connection, block_id, attrs)
    _refresh_fts(connection, block_id, document_id, text)
    connection.execute(
        """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (document_id, block_id, operation.model_dump_json(), now),
    )


def _move_block(
    connection: sqlite3.Connection,
    document_id: str,
    row: sqlite3.Row,
    operation: BlockOperation,
    now: str,
) -> None:
    parent_id = operation.parent_id
    _validate_parent(connection, document_id, parent_id)
    if parent_id == row["id"] or _is_descendant(connection, row["id"], parent_id):
        raise ValueError("cannot move a block into itself or its descendant")
    old_parent = row["parent_id"]
    old_position = row["position"]
    _shift_positions(connection, document_id, old_parent, old_position + 1, -1)
    position = operation.position
    if position is None:
        position = _next_position(connection, document_id, parent_id)
    else:
        _shift_positions(connection, document_id, parent_id, position, 1)
    connection.execute(
        "UPDATE blocks SET parent_id = ?, position = ?, updated_at = ? WHERE id = ?",
        (parent_id, position, now, row["id"]),
    )


def _delete_block(
    connection: sqlite3.Connection, document_id: str, row: sqlite3.Row
) -> None:
    connection.execute(
        """DELETE FROM blocks_fts WHERE block_id IN (
               WITH RECURSIVE descendants(id) AS (
                   SELECT id FROM blocks WHERE id = ?
                   UNION ALL
                   SELECT blocks.id FROM blocks JOIN descendants
                     ON blocks.parent_id = descendants.id
               ) SELECT id FROM descendants
           )""",
        (row["id"],),
    )
    connection.execute(
        "DELETE FROM blocks WHERE id = ? AND document_id = ?", (row["id"], document_id)
    )
    _shift_positions(connection, document_id, row["parent_id"], row["position"] + 1, -1)


def _subtree_rows(connection: sqlite3.Connection, block_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """WITH RECURSIVE subtree(id, depth) AS (
               SELECT id, 0 FROM blocks WHERE id = ?
               UNION ALL SELECT blocks.id, subtree.depth + 1
                 FROM blocks JOIN subtree ON blocks.parent_id = subtree.id
           ) SELECT blocks.* FROM blocks JOIN subtree ON subtree.id = blocks.id
             ORDER BY subtree.depth, blocks.position, blocks.id""", (block_id,)
    ).fetchall()


def _duplicate_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row,
                     operation: BlockOperation, now: str) -> None:
    parent_id = row["parent_id"]
    position = row["position"] + 1 if operation.position is None else operation.position
    _shift_positions(connection, document_id, parent_id, position, 1)
    id_map: dict[str, str] = {}
    for source in _subtree_rows(connection, row["id"]):
        new_id = str(ULID())
        id_map[source["id"]] = new_id
        new_parent = id_map.get(source["parent_id"], parent_id)
        new_position = position if source["id"] == row["id"] else source["position"]
        connection.execute(
            """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
               content_json, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, document_id, new_parent, new_position, source["type"], source["attrs_json"],
             source["content_json"], source["text"], now, now),
        )
        _replace_attrs(connection, new_id, json.loads(source["attrs_json"]))
        _refresh_fts(connection, new_id, document_id, source["text"])


def _split_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row,
                 operation: BlockOperation, now: str) -> None:
    offset = operation.split_at
    if offset is None or offset > len(row["text"]):
        raise ValueError("split requires split_at within block text")
    if len(_subtree_rows(connection, row["id"])) > 1:
        raise ValueError("cannot split a block with children")
    first, second = row["text"][:offset], row["text"][offset:]
    connection.execute("UPDATE blocks SET text = ?, content_json = ?, updated_at = ? WHERE id = ?",
                       (first, json.dumps({"markdown": first}), now, row["id"]))
    _refresh_fts(connection, row["id"], document_id, first)
    _shift_positions(connection, document_id, row["parent_id"], row["position"] + 1, 1)
    new_id = str(ULID())
    connection.execute(
        """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
           content_json, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, document_id, row["parent_id"], row["position"] + 1, row["type"],
         row["attrs_json"], json.dumps({"markdown": second}), second, now, now),
    )
    _replace_attrs(connection, new_id, json.loads(row["attrs_json"]))
    _refresh_fts(connection, new_id, document_id, second)


def _merge_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row, now: str) -> None:
    next_row = connection.execute(
        "SELECT * FROM blocks WHERE document_id = ? AND parent_id IS ? AND position = ?",
        (document_id, row["parent_id"], row["position"] + 1),
    ).fetchone()
    if next_row is None:
        raise ValueError("cannot merge the last sibling")
    if len(_subtree_rows(connection, row["id"])) > 1 or len(_subtree_rows(connection, next_row["id"])) > 1:
        raise ValueError("cannot merge blocks with children")
    merged = row["text"] + next_row["text"]
    connection.execute("UPDATE blocks SET text = ?, content_json = ?, updated_at = ? WHERE id = ?",
                       (merged, json.dumps({"markdown": merged}), now, row["id"]))
    _refresh_fts(connection, row["id"], document_id, merged)
    _delete_block(connection, document_id, next_row)


def _validate_parent(
    connection: sqlite3.Connection, document_id: str, parent_id: str | None
) -> None:
    if parent_id is None:
        return
    if not connection.execute(
        "SELECT 1 FROM blocks WHERE id = ? AND document_id = ?", (parent_id, document_id)
    ).fetchone():
        raise KeyError(f"parent block not found: {parent_id}")


def _is_descendant(
    connection: sqlite3.Connection, block_id: str, candidate_parent: str | None
) -> bool:
    if candidate_parent is None:
        return False
    row = connection.execute(
        "SELECT parent_id FROM blocks WHERE id = ?", (candidate_parent,)
    ).fetchone()
    while row is not None and row["parent_id"] is not None:
        if row["parent_id"] == block_id:
            return True
        row = connection.execute(
            "SELECT parent_id FROM blocks WHERE id = ?", (row["parent_id"],)
        ).fetchone()
    return False


def _next_position(
    connection: sqlite3.Connection, document_id: str, parent_id: str | None
) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(position) + 1, 0) AS next_position FROM blocks "
        "WHERE document_id = ? AND parent_id IS ?",
        (document_id, parent_id),
    ).fetchone()
    return int(row["next_position"])


def _shift_positions(
    connection: sqlite3.Connection,
    document_id: str,
    parent_id: str | None,
    from_position: int,
    delta: int,
) -> None:
    connection.execute(
        """UPDATE blocks SET position = position + ?
           WHERE document_id = ? AND parent_id IS ? AND position >= ?""",
        (delta, document_id, parent_id, from_position),
    )


def _replace_attrs(
    connection: sqlite3.Connection, block_id: str, attrs: dict[str, object]
) -> None:
    connection.execute("DELETE FROM block_attrs WHERE block_id = ?", (block_id,))
    connection.executemany(
        "INSERT INTO block_attrs(block_id, name, value) VALUES (?, ?, ?)",
        [(block_id, name, json.dumps(value)) for name, value in attrs.items()],
    )


def _refresh_fts(
    connection: sqlite3.Connection, block_id: str, document_id: str, text: str
) -> None:
    connection.execute("DELETE FROM blocks_fts WHERE block_id = ?", (block_id,))
    connection.execute(
        "INSERT INTO blocks_fts(block_id, document_id, text) VALUES (?, ?, ?)",
        (block_id, document_id, text),
    )


def _parse_blocks(body: str) -> list[ImportedBlock]:
    lines = body.splitlines()
    tokens = MarkdownIt("commonmark").parse(body)
    blocks, _ = _parse_sequence(tokens, lines, 0)
    return blocks


def _parse_sequence(
    tokens: list[Token], lines: list[str], index: int, stop: str | None = None
) -> tuple[list[ImportedBlock], int]:
    blocks: list[ImportedBlock] = []
    while index < len(tokens):
        token = tokens[index]
        if stop and token.type == stop and token.nesting == -1:
            return blocks, index + 1
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            nested, index = _parse_list(tokens, lines, index)
            blocks.extend(nested)
            continue
        if token.type == "heading_open":
            block, index = _parse_inline_block(tokens, lines, index, "heading")
        elif token.type == "paragraph_open":
            block, index = _parse_inline_block(tokens, lines, index, "paragraph")
        elif token.type == "blockquote_open":
            block, index = _parse_container_block(tokens, lines, index, "blockquote_close", "quote")
        elif token.type in {"fence", "code_block"}:
            block = ImportedBlock(
                str(ULID()),
                "code",
                _token_source(lines, token),
                {"language": token.info.strip()} if token.type == "fence" and token.info else {},
            )
            index += 1
        elif token.type == "hr":
            block = ImportedBlock(str(ULID()), "thematic_break", _token_source(lines, token))
            index += 1
        elif token.type in {"html_block", "table_open"}:
            block = ImportedBlock(str(ULID()), "raw", _token_source(lines, token))
            index += 1
        else:
            index += 1
            continue
        blocks.append(block)
    return blocks, index


def _parse_list(
    tokens: list[Token], lines: list[str], index: int
) -> tuple[list[ImportedBlock], int]:
    ordered = tokens[index].type == "ordered_list_open"
    close_type = "ordered_list_close" if ordered else "bullet_list_close"
    index += 1
    blocks: list[ImportedBlock] = []
    while index < len(tokens) and tokens[index].type != close_type:
        if tokens[index].type != "list_item_open":
            index += 1
            continue
        block, index = _parse_list_item(tokens, lines, index, ordered)
        blocks.append(block)
    return blocks, min(index + 1, len(tokens))


def _parse_list_item(
    tokens: list[Token], lines: list[str], index: int, ordered: bool
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    index += 1
    source = ""
    attrs: dict[str, object] = {}
    if ordered:
        attrs["ordered"] = True
    children: list[ImportedBlock] = []
    while index < len(tokens) and tokens[index].type != "list_item_close":
        token = tokens[index]
        if token.type == "paragraph_open":
            paragraph = tokens[index]
            source = _token_source(lines, paragraph)
            task = _TASK_RE.match(source)
            if task:
                attrs["checked"] = task.group(1).lower() == "x"
            index += 1
            while index < len(tokens) and tokens[index].type != "paragraph_close":
                index += 1
            index += 1
            continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            nested, index = _parse_list(tokens, lines, index)
            children.extend(nested)
            continue
        if token.type == "blockquote_open":
            child, index = _parse_container_block(tokens, lines, index, "blockquote_close", "quote")
            children.append(child)
            continue
        if token.type in {"fence", "code_block"}:
            child = ImportedBlock(str(ULID()), "code", _token_source(lines, token))
            children.append(child)
        index += 1
    if not source:
        source = _token_source(lines, open_token)
    return ImportedBlock(str(ULID()), "list", source, attrs, children), min(index + 1, len(tokens))


def _parse_inline_block(
    tokens: list[Token], lines: list[str], index: int, block_type: str
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    source = _token_source(lines, open_token)
    index += 1
    while index < len(tokens) and tokens[index].type != f"{block_type}_close":
        index += 1
    return ImportedBlock(str(ULID()), block_type, source), min(index + 1, len(tokens))


def _parse_container_block(
    tokens: list[Token], lines: list[str], index: int, close_type: str, block_type: str
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    index += 1
    start = open_token.map[0] if open_token.map else 0
    depth = 1
    while index < len(tokens) and depth:
        token = tokens[index]
        if token.type == open_token.type:
            depth += 1
        elif token.type == close_type:
            depth -= 1
        index += 1
    end = tokens[index - 1].map[1] if index and tokens[index - 1].map else len(lines)
    source = "\n".join(lines[start:end]).strip()
    return ImportedBlock(str(ULID()), block_type, source), index


def _token_source(lines: list[str], token: Token) -> str:
    if not token.map:
        return ""
    start, end = token.map
    return "\n".join(lines[start:end]).strip()


def _derive_title(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return "Untitled"


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
