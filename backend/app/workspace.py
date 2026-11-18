"""Everyday workspace actions over the canonical SQLite store.

Trash keeps deleted documents recoverable until someone empties it. Saved searches are small named queries the sidebar can re-run in one click.
"""

from __future__ import annotations

from pathlib import Path

from .block_store import (
    _now,
    _rebuild_document_assets,
    _rebuild_document_references,
    _replace_references,
    connection_scope,
    initialize,
)

# --- Trash -------------------------------------------------------------------


def list_trash(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT id, title, status, deleted_at,
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
            "deleted_at": row["deleted_at"],
            "snippet": row["snippet"][:160],
        }
        for row in rows
    ]


def restore_document(path: Path, document_id: str) -> dict[str, object]:
    """Bring a trashed document back to the end of its folder (or the top level)."""

    initialize(path)
    now = _now()
    with connection_scope(path, immediate=True) as connection:
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
        # Trashing removed the document from search; put it back.
        _rebuild_document_assets(connection, document_id)
        # Links are only recorded to live targets, so links written while this
        # document sat in the trash (to it or from it) must be picked up now.
        _rebuild_document_references(connection, document_id)
        _rebuild_inbound_references(connection, document_id)
    return {"id": document_id, "folder_id": folder_id, "position": position}


def _rebuild_inbound_references(connection, document_id: str) -> None:
    targets = {document_id} | {
        row["id"]
        for row in connection.execute(
            "SELECT id FROM blocks WHERE document_id = ?", (document_id,)
        )
    }
    # One pass over blocks that contain any link syntax at all.
    candidates = connection.execute(
        """SELECT id, document_id, text, content_json FROM blocks
            WHERE document_id <> ?
              AND (instr(text, '((') > 0 OR instr(text, '[[') > 0
                   OR instr(content_json, '((') > 0 OR instr(content_json, '[[') > 0)""",
        (document_id,),
    ).fetchall()
    for row in candidates:
        haystack = f"{row['text']}\n{row['content_json']}"
        if any(target in haystack for target in targets):
            _replace_references(connection, row["id"], row["document_id"])


def purge_document(path: Path, document_id: str) -> None:
    """Permanently remove one trashed document and everything that hangs off it."""

    initialize(path)
    with connection_scope(path) as connection:
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NOT NULL", (document_id,)
        ).fetchone() is None:
            raise KeyError("document is not in the trash")
        _purge(connection, [document_id])


def empty_trash(path: Path) -> list[str]:
    """Purge every trashed document; returns the purged IDs."""

    initialize(path)
    with connection_scope(path) as connection:
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM documents WHERE deleted_at IS NOT NULL"
            ).fetchall()
        ]
        _purge(connection, ids)
    return ids


def _purge(connection, document_ids: list[str]) -> None:
    for document_id in document_ids:
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
        # Children reference parents with ON DELETE CASCADE, so removing the
        # document cascades through blocks, refs, history, and receipts.
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


# --- Saved searches ----------------------------------------------------------


