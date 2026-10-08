"""Full-text search + embedding search + related notes, all over SQLite blocks."""

from . import block_store, documents, embeddings
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
