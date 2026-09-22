"""SQLite foundation for the block-centric workspace.

The current note API still reads Markdown. This module creates the new canonical
shape and imports existing Markdown once, so later API/UI work can switch over
without making the first migration destructive.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from ulid import ULID

from .models import BlockOperation

SCHEMA_VERSION = 1
DB_FILENAME = ".fortress.sqlite3"

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+")
_LIST_RE = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])\s+")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_THEMATIC_RE = re.compile(r"^ {0,3}(?:\*\s*){3,}$|^ {0,3}(?:-\s*){3,}$|^ {0,3}(?:_\s*){3,}$")


@dataclass(frozen=True)
class ImportedBlock:
    id: str
    type: str
    position: int
    source: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('rough', 'polished')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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

_ready_paths: set[Path] = set()


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


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(_SCHEMA)
        row = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported block database version {row['version']} (expected {SCHEMA_VERSION})"
            )
    _ready_paths.add(path.resolve())


def is_ready(path: Path) -> bool:
    return path.resolve() in _ready_paths


def bootstrap_markdown(notes_path: Path, path: Path) -> int:
    """Import Markdown files that are not already represented in SQLite.

    This is intentionally insert-only. Editing or deleting existing Markdown does
    not mutate the new store until the block API owns writes in a later phase.
    """

    initialize(path)
    imported = 0
    with connect(path) as connection:
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
    with connect(path) as connection:
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


def update_document_metadata(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    updated_at: object,
) -> None:
    initialize(path)
    with connect(path) as connection:
        cursor = connection.execute(
            """UPDATE documents
               SET title = ?, status = ?, tags_json = ?, updated_at = ?
               WHERE id = ?""",
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
    with connect(path) as connection:
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
    with connect(path) as connection:
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def backup_database(path: Path, destination: Path) -> None:
    """Create a consistent live snapshot using Python's SQLite backup API."""

    initialize(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def check_integrity(path: Path) -> bool:
    initialize(path)
    with connect(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    return result is not None and result[0] == "ok"


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
    connection.execute(
        """INSERT INTO documents(id, title, status, tags_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (document_id, title, status, json.dumps(tags), created_at, updated_at),
    )
    _insert_blocks(connection, document_id, body, created_at, updated_at)


def _insert_blocks(
    connection: sqlite3.Connection,
    document_id: str,
    body: str,
    created_at: str,
    updated_at: str,
) -> None:
    for block in _parse_blocks(body):
        connection.execute(
            """INSERT INTO blocks(
                   id, document_id, parent_id, position, type, attrs_json,
                   content_json, text, created_at, updated_at
               ) VALUES (?, ?, NULL, ?, ?, '{}', ?, ?, ?, ?)""",
            (
                block.id,
                document_id,
                block.position,
                block.type,
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
    with connect(path) as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
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
        "children": roots,
    }


def apply_transaction(path: Path, document_id: str, operations: list[BlockOperation]) -> dict:
    """Apply block operations atomically and return the resulting document tree."""

    initialize(path)
    now = _now()
    with connect(path) as connection:
        document = connection.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        for operation in operations:
            _apply_operation(connection, document_id, operation, now)
        connection.execute(
            "UPDATE documents SET updated_at = ? WHERE id = ?", (now, document_id)
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
    blocks: list[ImportedBlock] = []
    current: list[str] = []
    current_type = "paragraph"
    in_fence = False

    def flush() -> None:
        nonlocal current, current_type
        source = "\n".join(current).strip()
        if source:
            blocks.append(ImportedBlock(str(ULID()), current_type, len(blocks), source))
        current = []
        current_type = "paragraph"

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            if not in_fence and current:
                flush()
            in_fence = not in_fence
            current_type = "code" if in_fence else "code"
            current.append(line)
            if not in_fence:
                flush()
            continue
        if in_fence:
            current.append(line)
            continue
        if not line.strip():
            flush()
            continue
        line_type = _line_type(line)
        if current and line_type != current_type:
            flush()
        current_type = line_type
        current.append(line)
    flush()
    return blocks


def _line_type(line: str) -> str:
    if _HEADING_RE.match(line):
        return "heading"
    if _LIST_RE.match(line):
        return "list"
    if _QUOTE_RE.match(line):
        return "quote"
    if _THEMATIC_RE.match(line):
        return "thematic_break"
    return "paragraph"


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
