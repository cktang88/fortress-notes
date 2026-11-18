"""Everyday workspace actions over the canonical SQLite store.

Trash keeps deleted documents recoverable until someone empties it.
"""

from __future__ import annotations

from pathlib import Path

from .block_store import connection_scope, document_write, initialize, utc_now

# --- Trash -------------------------------------------------------------------


def list_trash(path: Path) -> list[dict[str, object]]:
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT id, title, status, deleted_at,
                      COALESCE((SELECT text FROM blocks WHERE blocks.document_id = documents.id
                                AND text <> '' ORDER BY parent_id IS NOT NULL, order_key
                                LIMIT 1), '') AS snippet
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

    now = utc_now()
    with document_write(path) as write:
        connection = write.connection
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
        # Trashing removed the document from search; put it back. (Links to and
        # from it were kept: link rows don't depend on their target being live.)
        write.rederive(document_id)
    return {"id": document_id, "folder_id": folder_id, "position": position}


def purge_document(path: Path, document_id: str) -> None:
    """Permanently remove one trashed document and everything that hangs off it."""

    with document_write(path) as write:
        if write.connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NOT NULL", (document_id,)
        ).fetchone() is None:
            raise KeyError("document is not in the trash")
        write.purge([document_id])


def empty_trash(path: Path) -> list[str]:
    """Purge every trashed document; returns the purged IDs."""

    with document_write(path) as write:
        ids = [
            row["id"]
            for row in write.connection.execute(
                "SELECT id FROM documents WHERE deleted_at IS NOT NULL"
            ).fetchall()
        ]
        write.purge(ids)
    return ids
