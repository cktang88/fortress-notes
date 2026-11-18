"""Spot notes that repeat each other, and fold one into the other.

Similarity is word containment: the share of this note's distinctive words that
also appear in another note. It's deliberately literal (not embeddings) so a
match means "you already wrote this", and it can be explained in one number.
"""

from __future__ import annotations

from pathlib import Path

from . import provenance as _provenance
from . import order_keys, textutil
from .block_store import (
    _document_provenance,
    _now,
    connection_scope,
    initialize,
    rebuild_document_derived,
)

MIN_WORDS = 12
THRESHOLD = 0.6

def _words(text: str) -> set[str]:
    return set(textutil.content_words(text))


def similar_documents(path: Path, document_id: str, limit: int = 3) -> list[dict[str, object]]:
    """Other notes that contain most of this note's distinctive words."""

    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT documents.id, documents.title, documents.updated_at,
                      COALESCE(group_concat(blocks.text, ' '), '') AS body
                 FROM documents LEFT JOIN blocks ON blocks.document_id = documents.id
                WHERE documents.deleted_at IS NULL
                GROUP BY documents.id"""
        ).fetchall()
    bodies = {row["id"]: row for row in rows}
    if document_id not in bodies:
        raise KeyError("document not found")
    mine = _words(bodies[document_id]["body"])
    if len(mine) < MIN_WORDS:
        return []
    matches = []
    for other_id, row in bodies.items():
        if other_id == document_id:
            continue
        theirs = _words(row["body"])
        if len(theirs) < MIN_WORDS:
            continue
        overlap = len(mine & theirs) / len(mine)
        if overlap >= THRESHOLD:
            matches.append(
                {
                    "document_id": other_id,
                    "title": row["title"],
                    "updated_at": row["updated_at"],
                    "overlap": round(overlap, 2),
                }
            )
    matches.sort(key=lambda match: -float(match["overlap"]))
    return matches[:limit]


def move_into(path: Path, source_id: str, target_id: str) -> int:
    """Move every block of ``source_id`` to the end of ``target_id``; returns blocks moved.

    Block IDs are kept, so links and backlinks to the moved paragraphs still work.
    Both documents' undo history is cleared (it refers to the old layout), and the
    emptied source document is moved to the trash.
    """

    if source_id == target_id:
        raise ValueError("a note can't be moved into itself")
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        active = {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM documents WHERE id IN (?, ?) AND deleted_at IS NULL",
                (source_id, target_id),
            ).fetchall()
        }
        if active != {source_id, target_id}:
            raise KeyError("document not found")
        last_key = connection.execute(
            """SELECT MAX(order_key) FROM blocks WHERE document_id = ? AND parent_id IS NULL""",
            (target_id,),
        ).fetchone()[0]
        moved = connection.execute(
            "SELECT COUNT(*) FROM blocks WHERE document_id = ?", (source_id,)
        ).fetchone()[0]
        # Each moved block remembers the note it came from, and keeps its own
        # source (a block that inherited the old note's provenance gets a copy).
        source_provenance = _document_provenance(connection, source_id)
        source_title = connection.execute(
            "SELECT title FROM documents WHERE id = ?", (source_id,)
        ).fetchone()["title"]
        target_revision = connection.execute(
            "SELECT revision FROM documents WHERE id = ?", (target_id,)
        ).fetchone()["revision"] + 1
        updates = []
        for row in connection.execute(
            "SELECT id, provenance_json FROM blocks WHERE document_id = ?", (source_id,)
        ).fetchall():
            value = _provenance.materialize(row["provenance_json"], source_provenance)
            value["origin"] = {
                "operation": "move_into",
                "document_id": source_id,
                "document_title": source_title,
                "block_id": row["id"],
                "at": now,
            }
            updates.append((_provenance.dumps(value), target_revision, row["id"]))
        connection.executemany(
            "UPDATE blocks SET provenance_json = ?, revision = ? WHERE id = ?", updates
        )
        # The moved top-level blocks go after the target's, in their order.
        roots = [
            row[0] for row in connection.execute(
                """SELECT id FROM blocks WHERE document_id = ? AND parent_id IS NULL
                    ORDER BY order_key""",
                (source_id,),
            )
        ]
        connection.execute(
            "UPDATE blocks SET order_key = '~' || id WHERE document_id = ? AND parent_id IS NULL",
            (source_id,),
        )
        connection.execute(
            "UPDATE blocks SET document_id = ? WHERE document_id = ?", (target_id, source_id)
        )
        connection.executemany(
            "UPDATE blocks SET order_key = ? WHERE id = ?",
            list(zip(order_keys.keys_between(last_key, None, len(roots)), roots)),
        )
        connection.execute(
            "DELETE FROM document_history WHERE document_id IN (?, ?)", (source_id, target_id)
        )
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (source_id,))
        rebuild_document_derived(connection, target_id)
        connection.execute(
            """UPDATE documents SET updated_at = ?, revision = revision + 1
                WHERE id IN (?, ?)""",
            (now, source_id, target_id),
        )
    return int(moved)
