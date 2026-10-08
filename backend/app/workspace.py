"""Everyday workspace actions over the canonical SQLite store.

Trash keeps deleted documents recoverable until someone empties it. Tags live on
documents, so renaming or removing a tag rewrites every document that carries it.
Saved searches are small named queries the sidebar can re-run in one click.
"""

from __future__ import annotations

import json
from pathlib import Path

from .block_store import _new_ulid, _now, connection_scope, initialize

SAVED_SEARCH_MODES = {"text", "embedding"}


# --- Trash -------------------------------------------------------------------


def list_trash(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT id, title, status, tags_json, deleted_at,
                      COALESCE((SELECT text FROM blocks WHERE blocks.document_id = documents.id
                                ORDER BY parent_id, position, id LIMIT 1), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id ASC"""
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "tags": _tags(row["tags_json"]),
            "deleted_at": row["deleted_at"],
            "snippet": row["snippet"][:160],
        }
        for row in rows
    ]


def restore_document(path: Path, document_id: str) -> dict[str, object]:
    """Bring a trashed document back to the end of its folder (or the top level)."""

    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        row = connection.execute(
            "SELECT folder_id FROM documents WHERE id = ? AND deleted_at IS NOT NULL",
            (document_id,),
        ).fetchone()
        if row is None:
            raise KeyError("document is not in the trash")
        folder_id = row["folder_id"]
        if folder_id is not None and connection.execute(
            "SELECT 1 FROM folders WHERE id = ?", (folder_id,)
        ).fetchone() is None:
            folder_id = None
        position = connection.execute(
            """SELECT COALESCE(MAX(position) + 1, 0) FROM documents
                WHERE folder_id IS ? AND deleted_at IS NULL""",
            (folder_id,),
        ).fetchone()[0]
        connection.execute(
            """UPDATE documents
                  SET deleted_at = NULL, folder_id = ?, position = ?, updated_at = ?
                WHERE id = ?""",
            (folder_id, position, now, document_id),
        )
    return {"id": document_id, "folder_id": folder_id, "position": position}


def purge_document(path: Path, document_id: str) -> None:
    """Permanently remove one trashed document and everything that hangs off it."""

    initialize(path)
    with connection_scope(path) as connection:
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NOT NULL", (document_id,)
        ).fetchone() is None:
            raise KeyError("document is not in the trash")
        _purge(connection, [document_id])


def empty_trash(path: Path) -> int:
    initialize(path)
    with connection_scope(path) as connection:
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM documents WHERE deleted_at IS NOT NULL"
            ).fetchall()
        ]
        _purge(connection, ids)
    return len(ids)


def _purge(connection, document_ids: list[str]) -> None:
    for document_id in document_ids:
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
        # Children reference parents with ON DELETE CASCADE, so removing the
        # document cascades through blocks, refs, history, and receipts.
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


# --- Tags --------------------------------------------------------------------


def list_tags(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT tags.value AS tag, COUNT(*) AS count
                 FROM documents,
                      json_each(CASE WHEN json_valid(documents.tags_json)
                                     THEN documents.tags_json ELSE '[]' END) AS tags
                WHERE documents.deleted_at IS NULL AND trim(tags.value) <> ''
                GROUP BY tags.value
                ORDER BY lower(tags.value), tags.value"""
        ).fetchall()
    return [{"tag": row["tag"], "count": row["count"]} for row in rows]


def rename_tag(path: Path, old: str, new: str) -> list[str]:
    """Rename (or merge) a tag on every active document; returns changed IDs."""

    new = new.strip()
    if not new:
        raise ValueError("tag name cannot be empty")
    return _rewrite_tag(path, old, new)


def delete_tag(path: Path, tag: str) -> list[str]:
    return _rewrite_tag(path, tag, None)


def _rewrite_tag(path: Path, old: str, new: str | None) -> list[str]:
    initialize(path)
    changed: list[str] = []
    now = _now()
    with connection_scope(path) as connection:
        rows = connection.execute(
            "SELECT id, tags_json FROM documents WHERE deleted_at IS NULL"
        ).fetchall()
        for row in rows:
            tags = _tags(row["tags_json"])
            if old not in tags:
                continue
            updated: list[str] = []
            for tag in tags:
                replacement = new if tag == old else tag
                if replacement is not None and replacement not in updated:
                    updated.append(replacement)
            connection.execute(
                "UPDATE documents SET tags_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(updated), now, row["id"]),
            )
            changed.append(row["id"])
    return changed


def _tags(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(tag) for tag in value] if isinstance(value, list) else []


# --- Saved searches ----------------------------------------------------------


def list_saved_searches(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            "SELECT * FROM saved_searches ORDER BY created_at ASC, id ASC"
        ).fetchall()
    return [_saved_search(row) for row in rows]


def create_saved_search(
    path: Path, name: str, query: str, mode: str, filters: dict[str, object]
) -> dict[str, object]:
    name, query = name.strip(), query.strip()
    if not query:
        raise ValueError("a saved search needs a query")
    if mode not in SAVED_SEARCH_MODES:
        raise ValueError("unknown search mode")
    clean_filters = {
        key: value for key, value in filters.items() if isinstance(value, str) and value
    }
    initialize(path)
    search_id = _new_ulid()
    with connection_scope(path) as connection:
        connection.execute(
            """INSERT INTO saved_searches(id, name, query, mode, filters_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (search_id, name or query, query, mode, json.dumps(clean_filters), _now()),
        )
        row = connection.execute(
            "SELECT * FROM saved_searches WHERE id = ?", (search_id,)
        ).fetchone()
    return _saved_search(row)


def delete_saved_search(path: Path, search_id: str) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        if cursor.rowcount == 0:
            raise KeyError("saved search not found")


def _saved_search(row) -> dict[str, object]:
    try:
        filters = json.loads(row["filters_json"])
    except ValueError:
        filters = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "query": row["query"],
        "mode": row["mode"],
        "filters": filters if isinstance(filters, dict) else {},
        "created_at": row["created_at"],
    }
