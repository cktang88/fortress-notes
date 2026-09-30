"""Full-text search + embedding search + related notes."""

from . import block_store, embeddings, images, notes_store
from .config import get_settings
from .models import RelatedResult, SearchResult

_RELATED_EXCERPT_LENGTH = 240


def _all_docs() -> list[tuple[str, str, str]]:
    """Returns [(id, updated_at_iso, searchable_text)] for every note."""
    docs = []
    for summary in notes_store.list_notes():
        note = notes_store.get_note(summary.id)
        if note:
            docs.append(
                (note.id, note.updated_at.isoformat(), images.note_search_text(note.title, note.body))
            )
    return docs


def full_text_search(q: str) -> list[SearchResult]:
    q_lower = q.lower().strip()
    results: list[SearchResult] = []
    if not q_lower:
        return results
    settings = get_settings()
    if settings.block_db_enabled:
        hits = block_store.search_blocks(settings.block_db_path, q)
        summaries = {summary.id: summary for summary in notes_store.list_notes()}
        scores: dict[str, float] = {}
        for hit in hits:
            document_id = str(hit["document_id"])
            scores[document_id] = max(scores.get(document_id, 0.0), float(hit["score"]))
        return [
            SearchResult(note=summaries[document_id], score=score)
            for document_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
            if document_id in summaries
        ]
    for summary in notes_store.list_notes():
        note = notes_store.get_note(summary.id)
        if not note:
            continue
        haystack = images.note_search_text(note.title, note.body).lower()
        count = haystack.count(q_lower)
        if count:
            results.append(SearchResult(note=summary, score=float(count)))
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def embedding_search(q: str, k: int = 20) -> list[SearchResult]:
    if not q.strip():
        return []
    settings = get_settings()
    if settings.block_db_enabled:
        block_docs = block_store.embedding_documents(settings.block_db_path)
        scores = embeddings.score_documents(
            q, [(block_id, updated_at, text) for block_id, _, updated_at, text in block_docs]
        )
        document_ids = {block_id: document_id for block_id, document_id, _, _ in block_docs}
        note_scores: dict[str, float] = {}
        for block_id, score in scores.items():
            document_id = document_ids[block_id]
            note_scores[document_id] = max(note_scores.get(document_id, 0.0), score)
    else:
        note_scores = embeddings.score_documents(q, _all_docs())
    summaries = {s.id: s for s in notes_store.list_notes()}
    results = [
        SearchResult(note=summaries[note_id], score=score)
        for note_id, score in note_scores.items()
        if note_id in summaries and score > 0
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:k]


def related_notes(note_id: str, k: int = 5, block_id: str | None = None) -> list[RelatedResult]:
    note = notes_store.get_note(note_id)
    if not note:
        return []
    query = f"{note.title}\n\n{note.body}"
    settings = get_settings()
    if block_id is not None:
        if not settings.block_db_enabled:
            raise KeyError("Block store is disabled")
        source = block_store.document_subtree(settings.block_db_path, note_id, block_id)
        if source is None:
            raise KeyError("Block document not found")
        query = source["subtree"]["text"]
    if settings.block_db_enabled:
        all_blocks = block_store.embedding_documents(settings.block_db_path)
        if block_id is not None:
            query = next((text for bid, _, _, text in all_blocks if bid == block_id), query)
            if not query.strip():
                return []
        block_docs = [
            d for d in all_blocks if d[1] != note_id
        ]
        scores = embeddings.score_documents(
            query, [(block_id, updated_at, text) for block_id, _, updated_at, text in block_docs]
        )
        document_ids = {block_id: document_id for block_id, document_id, _, _ in block_docs}
        block_texts = {block_id: text for block_id, _, _, text in block_docs}
        note_matches: dict[str, tuple[float, str, str]] = {}
        for block_id, score in scores.items():
            document_id = document_ids[block_id]
            current = note_matches.get(document_id)
            if current is None or score > current[0]:
                note_matches[document_id] = (score, block_id, block_texts[block_id])
    else:
        docs = [d for d in _all_docs() if d[0] != note_id]
        scores = embeddings.score_documents(query, docs)
        note_matches = {nid: (score, "", "") for nid, score in scores.items()}
    summaries = {s.id: s for s in notes_store.list_notes()}
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
