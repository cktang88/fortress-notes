"""Spot notes that repeat each other, and fold one into the other.

Similarity is word containment: the share of this note's distinctive words that
also appear in another note. It's deliberately literal (not embeddings) so a
match means "you already wrote this", and it can be explained in one number.
"""

from __future__ import annotations

import re
from pathlib import Path

from .block_store import (
    _now,
    _rebuild_document_assets,
    _rebuild_document_references,
    connection_scope,
    initialize,
)

MIN_WORDS = 12
THRESHOLD = 0.6

_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
_STOPWORDS = frozenset(
    """the and for that with this from have are was were but not you your our they them
    their there then than what when where which who will would could should about into
    over also just like some more most only very been being has had its it's can all any
    each other such these those out how why use using get got one two may might must""".split()
)


def _words(text: str) -> set[str]:
    return {word for word in (w.lower() for w in _WORD.findall(text)) if word not in _STOPWORDS}


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
        offset = connection.execute(
            """SELECT COALESCE(MAX(position) + 1, 0) FROM blocks
                WHERE document_id = ? AND parent_id IS NULL""",
            (target_id,),
        ).fetchone()[0]
        moved = connection.execute(
            "SELECT COUNT(*) FROM blocks WHERE document_id = ?", (source_id,)
        ).fetchone()[0]
        connection.execute(
            """UPDATE blocks
                  SET document_id = ?,
                      position = CASE WHEN parent_id IS NULL THEN position + ? ELSE position END
                WHERE document_id = ?""",
            (target_id, offset, source_id),
        )
        connection.execute(
            "DELETE FROM document_history WHERE document_id IN (?, ?)", (source_id, target_id)
        )
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (source_id,))
        _rebuild_document_references(connection, target_id)
        _rebuild_document_assets(connection, target_id)
        connection.execute(
            """UPDATE documents SET updated_at = ?, revision = revision + 1
                WHERE id IN (?, ?)""",
            (now, source_id, target_id),
        )
    return int(moved)
