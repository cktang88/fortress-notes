"""Filtered, block-level full-text queries for the canonical SQLite store.

This module deliberately contains no HTTP concerns.  Routes can use
``search_blocks`` directly once they are ready to expose filtered search.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .block_store import connection_scope, initialize


_FTS_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_VALID_STATUSES = frozenset({"rough", "polished"})


@dataclass(frozen=True)
class BlockSearchFilters:
    """Optional constraints for a canonical block search.

    Invalid non-empty values produce no matches.  This makes a partially
    malformed query safe for callers that pass request parameters directly.
    Timestamps accept aware ``datetime`` values or ISO-8601 strings.
    """

    document_id: str | None = None
    block_type: str | None = None
    status: str | None = None
    tag: str | None = None
    updated_after: datetime | str | None = None
    updated_before: datetime | str | None = None


def build_fts_match(query: str) -> str | None:
    """Return a quoted AND query without exposing FTS operators or wildcards."""

    if not isinstance(query, str):
        return None
    tokens = _FTS_TOKEN.findall(query)
    if not tokens:
        return None
    return " AND ".join(f'"{token}"' for token in tokens)


def search_blocks(
    path: Path,
    query: str,
    filters: BlockSearchFilters | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return matching canonical blocks, excluding soft-deleted documents."""

    match = build_fts_match(query)
    if match is None or not isinstance(limit, int) or limit < 1:
        return []

    if filters is not None and not isinstance(filters, BlockSearchFilters):
        return []
    filters = filters or BlockSearchFilters()
    predicates = ["blocks_fts MATCH ?", "documents.deleted_at IS NULL"]
    parameters: list[object] = [match]

    if not _add_filters(predicates, parameters, filters):
        return []

    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"""SELECT blocks_fts.block_id, blocks_fts.document_id,
                       documents.title AS document_title,
                       blocks.type AS block_type, blocks.text,
                       bm25(blocks_fts) AS rank
                  FROM blocks_fts
                  JOIN blocks ON blocks.id = blocks_fts.block_id
                  JOIN documents ON documents.id = blocks_fts.document_id
                 WHERE {' AND '.join(predicates)}
                 ORDER BY rank, blocks.updated_at DESC, blocks.id ASC
                 LIMIT ?""",
            (*parameters, min(limit, 200)),
        ).fetchall()

    return [
        {
            "block_id": row["block_id"],
            "document_id": row["document_id"],
            "document_title": row["document_title"],
            "block_type": row["block_type"],
            "text": row["text"],
            "score": -float(row["rank"]),
        }
        for row in rows
    ]


def _add_filters(
    predicates: list[str], parameters: list[object], filters: BlockSearchFilters
) -> bool:
    if not _add_exact_filter(
        predicates, parameters, "blocks.document_id", filters.document_id
    ):
        return False
    if not _add_exact_filter(predicates, parameters, "blocks.type", filters.block_type):
        return False

    if filters.status is not None:
        if not isinstance(filters.status, str) or filters.status not in _VALID_STATUSES:
            return False
        predicates.append("documents.status = ?")
        parameters.append(filters.status)

    if filters.tag is not None:
        if not isinstance(filters.tag, str) or not filters.tag.strip():
            return False
        predicates.append(
            """EXISTS (
                   SELECT 1
                     FROM json_each(
                         CASE WHEN json_valid(documents.tags_json)
                              THEN documents.tags_json ELSE '[]' END
                     ) AS tags
                    WHERE tags.value = ?
               )"""
        )
        parameters.append(filters.tag)

    updated_after = _normalize_timestamp(filters.updated_after)
    updated_before = _normalize_timestamp(filters.updated_before, upper_bound=True)
    if (filters.updated_after is not None and updated_after is None) or (
        filters.updated_before is not None and updated_before is None
    ):
        return False
    if updated_after is not None:
        predicates.append("julianday(blocks.updated_at) >= julianday(?)")
        parameters.append(updated_after)
    if updated_before is not None:
        predicates.append("julianday(blocks.updated_at) < julianday(?)")
        parameters.append(updated_before)
    if (
        updated_after is not None
        and updated_before is not None
        and updated_after >= updated_before
    ):
        return False
    return True


def _add_exact_filter(
    predicates: list[str], parameters: list[object], column: str, value: str | None
) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    predicates.append(f"{column} = ?")
    parameters.append(value)
    return True


def _normalize_timestamp(
    value: datetime | str | None, *, upper_bound: bool = False
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date_only and upper_bound:
                value += timedelta(days=1)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def allowed_block_ids(path: Path, filters: BlockSearchFilters | None) -> set[str] | None:
    """IDs of active blocks passing ``filters``; ``None`` when nothing is filtered."""

    if filters is None or filters == BlockSearchFilters():
        return None
    predicates = ["documents.deleted_at IS NULL"]
    parameters: list[object] = []
    if not _add_filters(predicates, parameters, filters):
        return set()
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"""SELECT blocks.id FROM blocks
                  JOIN documents ON documents.id = blocks.document_id
                 WHERE {' AND '.join(predicates)}""",
            parameters,
        ).fetchall()
    return {row["id"] for row in rows}


def block_details(path: Path, block_ids: list[str]) -> dict[str, dict[str, object]]:
    """Display details for active blocks, keyed by block ID."""

    if not block_ids:
        return {}
    initialize(path)
    placeholders = ",".join("?" for _ in block_ids)
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"""SELECT blocks.id, blocks.document_id, blocks.type, blocks.text,
                       blocks.updated_at, documents.title AS document_title
                  FROM blocks JOIN documents ON documents.id = blocks.document_id
                 WHERE blocks.id IN ({placeholders}) AND documents.deleted_at IS NULL""",
            block_ids,
        ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def title_matches(path: Path, query: str, limit: int = 20) -> list[dict[str, object]]:
    """Documents with title words starting with query words (most words first).

    Matching ignores case and accents for every script (SQLite's ``lower()`` only
    folds ASCII), and a word must start with the query word, so a short word such as
    "a" doesn't match every title that merely contains the letter.
    """

    tokens = sorted(set(query_tokens(query)))
    if not tokens:
        return []
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT documents.id AS document_id, documents.title, documents.updated_at,
                      (SELECT blocks.id FROM blocks
                        WHERE blocks.document_id = documents.id
                          AND blocks.parent_id IS NULL
                        ORDER BY blocks.position LIMIT 1) AS block_id
                 FROM documents
                WHERE documents.deleted_at IS NULL"""
        ).fetchall()
    matches = []
    for row in rows:
        if not row["block_id"]:
            continue
        words = query_tokens(str(row["title"] or ""))
        hits = sum(1 for token in tokens if any(word.startswith(token) for word in words))
        if hits:
            matches.append(
                {
                    "document_id": row["document_id"],
                    "hits": hits,
                    "updated_at": row["updated_at"],
                    "block_id": row["block_id"],
                }
            )
    matches.sort(key=lambda match: str(match["updated_at"]), reverse=True)
    matches.sort(key=lambda match: -int(match["hits"]))
    return matches[:limit]


def fold(text: str) -> str:
    """Lowercase ``text`` and strip accents, keeping exactly one character per character.

    Offsets into the result are offsets into ``text``, so matches found in the folded
    text can be highlighted in the original. Mirrors the FTS ``unicode61`` tokenizer's
    case and diacritic folding closely enough for highlighting.
    """

    if text.isascii():
        return text.lower()
    return "".join(map(_fold_char, text))


@lru_cache(maxsize=4096)
def _fold_char(character: str) -> str:
    if character.isascii():
        return character.lower()
    base = "".join(
        part
        for part in unicodedata.normalize("NFKD", character)
        if not unicodedata.combining(part)
    ).lower()
    if len(base) == 1:
        return base
    lowered = character.lower()
    return lowered if len(lowered) == 1 else character


def query_tokens(query: str) -> list[str]:
    """Words of ``query`` as the word search sees them: lowercase, without accents."""

    return _FTS_TOKEN.findall(fold(query))


def word_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, folded word) for each word of ``text``, in ``text`` offsets."""

    return [
        (match.start(), match.end(), match.group())
        for match in _FTS_TOKEN.finditer(fold(text))
    ]
