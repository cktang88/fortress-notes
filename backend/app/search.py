"""Full-text search + embedding search + related notes, all over SQLite blocks."""

import math
from datetime import datetime, timezone

from . import block_query, block_store, documents, embeddings
from .config import get_settings
from .models import RelatedResult, SearchResult

_RELATED_EXCERPT_LENGTH = 240


def full_text_search(q: str) -> list[SearchResult]:
    if not q.strip():
        return []
    hits = block_store.search_blocks(get_settings().block_db_path, q)
    scores: dict[str, float] = {}
    for hit in hits:
        document_id = str(hit["document_id"])
        scores[document_id] = max(scores.get(document_id, 0.0), float(hit["score"]))
    return _ranked(scores)


def embedding_search(q: str, k: int = 20) -> list[SearchResult]:
    if not q.strip():
        return []
    block_docs = block_store.embedding_documents(get_settings().block_db_path)
    scores = embeddings.score_documents(
        q, [(block_id, updated_at, text) for block_id, _, updated_at, text in block_docs]
    )
    document_ids = {block_id: document_id for block_id, document_id, _, _ in block_docs}
    note_scores: dict[str, float] = {}
    for block_id, score in scores.items():
        document_id = document_ids[block_id]
        note_scores[document_id] = max(note_scores.get(document_id, 0.0), score)
    return _ranked(note_scores)[:k]


def _ranked(scores: dict[str, float]) -> list[SearchResult]:
    summaries = documents.summaries()
    results = [
        SearchResult(note=summaries[document_id], score=score)
        for document_id, score in scores.items()
        if document_id in summaries and score > 0
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def related_notes(note_id: str, k: int = 5, block_id: str | None = None) -> list[RelatedResult]:
    settings = get_settings()
    note = documents.get_note(note_id)
    if not note:
        return []
    query = f"{note.title}\n\n{note.body}"
    if block_id is not None:
        source = block_store.document_subtree(settings.block_db_path, note_id, block_id)
        if source is None:
            raise KeyError("Block document not found")
        query = source["subtree"]["text"]
    all_blocks = block_store.embedding_documents(settings.block_db_path)
    if block_id is not None:
        query = next((text for bid, _, _, text in all_blocks if bid == block_id), query)
        if not query.strip():
            return []
    block_docs = [d for d in all_blocks if d[1] != note_id]
    scores = embeddings.score_documents(
        query, [(bid, updated_at, text) for bid, _, updated_at, text in block_docs]
    )
    document_ids = {bid: document_id for bid, document_id, _, _ in block_docs}
    block_texts = {bid: text for bid, _, _, text in block_docs}
    note_matches: dict[str, tuple[float, str, str]] = {}
    for bid, score in scores.items():
        document_id = document_ids[bid]
        current = note_matches.get(document_id)
        if current is None or score > current[0]:
            note_matches[document_id] = (score, bid, block_texts[bid])
    summaries = documents.summaries()
    results = [
        RelatedResult(
            note=summaries[nid],
            score=match[0],
            matched_block_id=match[1] or None,
            matched_block_text=match[2][:_RELATED_EXCERPT_LENGTH],
        )
        for nid, match in note_matches.items()
        if nid in summaries and match[0] > 0
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:k]


# --- Unified search ------------------------------------------------------------

_RRF_K = 60
_MEANING_CANDIDATES = 25
_PER_DOCUMENT = 3
_SNIPPET = 220


def unified_search(
    q: str, filters: block_query.BlockSearchFilters | None = None, limit: int = 30
) -> list[dict[str, object]]:
    """One ranked list of blocks from exact words, meaning, and note titles.

    Each source ranks blocks on its own; reciprocal rank fusion combines the
    ranks, so no score scales need to agree. Recent edits get a small boost and
    no note contributes more than a few blocks.
    """

    if not q.strip():
        return []
    db_path = get_settings().block_db_path
    allowed = block_query.allowed_block_ids(db_path, filters)

    words = block_query.search_blocks(db_path, q, filters, limit=100)
    word_ranking = [str(hit["block_id"]) for hit in words]

    blocks = block_store.embedding_documents(db_path)
    if allowed is not None:
        blocks = [block for block in blocks if block[0] in allowed]
    meaning_scores = embeddings.score_documents(
        q, [(block_id, version, text) for block_id, _, version, text in blocks]
    )
    meaning_ranking = [
        block_id
        for block_id, score in sorted(meaning_scores.items(), key=lambda item: -item[1])
        if score > 0
    ][:_MEANING_CANDIDATES]

    title_ranking = [
        str(match["block_id"])
        for match in block_query.title_matches(db_path, q)
        if allowed is None or match["block_id"] in allowed
    ]

    fused: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for reason, ranking in (
        ("words", word_ranking),
        ("meaning", meaning_ranking),
        ("title", title_ranking),
    ):
        for rank, block_id in enumerate(ranking):
            fused[block_id] = fused.get(block_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            reasons.setdefault(block_id, []).append(reason)

    details = block_query.block_details(db_path, list(fused))
    now = datetime.now(timezone.utc)
    for block_id, row in details.items():
        fused[block_id] *= 1.0 + 0.15 * _recency(str(row["updated_at"]), now)

    tokens = block_query.query_tokens(q)
    results: list[dict[str, object]] = []
    per_document: dict[str, int] = {}
    for block_id in sorted(details, key=lambda bid: -fused[bid]):
        row = details[block_id]
        document_id = str(row["document_id"])
        if per_document.get(document_id, 0) >= _PER_DOCUMENT:
            continue
        per_document[document_id] = per_document.get(document_id, 0) + 1
        snippet, highlights = _snippet(str(row["text"]), tokens)
        results.append(
            {
                "block_id": block_id,
                "document_id": document_id,
                "document_title": row["document_title"],
                "block_type": row["type"],
                "text": snippet,
                "highlights": highlights,
                "matched": reasons[block_id],
                "score": round(fused[block_id], 6),
            }
        )
        if len(results) >= limit:
            break
    return results


def _recency(updated_at: str, now: datetime) -> float:
    """1.0 for something edited just now, fading to ~0 over a few months."""

    try:
        edited = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if edited.tzinfo is None:
        edited = edited.replace(tzinfo=timezone.utc)
    days = max((now - edited).total_seconds() / 86400, 0.0)
    return math.exp(-days / 45)


def _snippet(text: str, tokens: list[str]) -> tuple[str, list[list[int]]]:
    """A window of ``text`` around the first matched word, plus highlight ranges."""

    lowered = text.lower()
    positions = [lowered.find(token) for token in tokens if token and token in lowered]
    first = min(positions) if positions else 0
    start = max(0, min(first - _SNIPPET // 3, len(text) - _SNIPPET))
    if start > 0:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < first else start
    window = text[start : start + _SNIPPET]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + _SNIPPET < len(text) else ""
    snippet = f"{prefix}{window}{suffix}"
    lowered_snippet = snippet.lower()
    ranges: list[list[int]] = []
    for token in sorted(set(tokens), key=len, reverse=True):
        index = lowered_snippet.find(token)
        while index != -1:
            end = index + len(token)
            if not any(index < r[1] and end > r[0] for r in ranges):
                ranges.append([index, end])
            index = lowered_snippet.find(token, end)
    ranges.sort()
    return snippet, ranges
