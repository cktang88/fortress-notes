"""Full-text search + embedding search + related notes."""

from . import embeddings, notes_store
from .models import NoteSummary, SearchResult


def _all_docs() -> list[tuple[str, str, str]]:
    """Returns [(id, updated_at_iso, body)] for every note."""
    docs = []
    for summary in notes_store.list_notes():
        note = notes_store.get_note(summary.id)
        if note:
            docs.append((note.id, note.updated_at.isoformat(), f"{note.title}\n\n{note.body}"))
    return docs


def full_text_search(q: str) -> list[SearchResult]:
    q_lower = q.lower().strip()
    results: list[SearchResult] = []
    if not q_lower:
        return results
    for summary in notes_store.list_notes():
        note = notes_store.get_note(summary.id)
        if not note:
            continue
        haystack = f"{note.title}\n{note.body}".lower()
        count = haystack.count(q_lower)
        if count:
            results.append(SearchResult(note=summary, score=float(count)))
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def embedding_search(q: str, k: int = 20) -> list[SearchResult]:
    if not q.strip():
        return []
    docs = _all_docs()
    scores = embeddings.score_documents(q, docs)
    summaries = {s.id: s for s in notes_store.list_notes()}
    results = [
        SearchResult(note=summaries[note_id], score=score)
        for note_id, score in scores.items()
        if note_id in summaries and score > 0
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:k]


def related_notes(note_id: str, k: int = 5) -> list[SearchResult]:
    note = notes_store.get_note(note_id)
    if not note:
        return []
    query = f"{note.title}\n\n{note.body}"
    docs = [d for d in _all_docs() if d[0] != note_id]
    scores = embeddings.score_documents(query, docs)
    summaries = {s.id: s for s in notes_store.list_notes()}
    results = [
        SearchResult(note=summaries[nid], score=score)
        for nid, score in scores.items()
        if nid in summaries and score > 0
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:k]
