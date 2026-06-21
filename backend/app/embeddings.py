"""ColBERT late-interaction search via PyLate, with a keyword fallback.

Doc embeddings are cached per (id, updated_at) so we only re-encode changed notes.
Scoring is brute-force MaxSim, which is fast enough for a personal note collection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import get_settings


@dataclass
class _Cache:
    model: object | None = None
    loaded: bool = False
    failed: bool = False
    # id -> (updated_at_iso, doc_embedding tensor)
    docs: dict[str, tuple[str, object]] = field(default_factory=dict)


_cache = _Cache()


def model_loaded() -> bool:
    return _cache.loaded


def _get_model():
    """Lazily load the ColBERT model. Returns None if disabled or unavailable."""
    settings = get_settings()
    if not settings.embeddings_enabled or _cache.failed:
        return None
    if _cache.model is not None:
        return _cache.model
    try:
        from pylate import models  # heavy import; do it lazily

        _cache.model = models.ColBERT(model_name_or_path=settings.colbert_model)
        _cache.loaded = True
        return _cache.model
    except Exception:
        _cache.failed = True
        return None


def _encode_doc(model, note_id: str, updated_at_iso: str, text: str):
    cached = _cache.docs.get(note_id)
    if cached and cached[0] == updated_at_iso:
        return cached[1]
    emb = model.encode([text], is_query=False, show_progress_bar=False)[0]
    _cache.docs[note_id] = (updated_at_iso, emb)
    return emb


def _maxsim(query_emb, doc_emb) -> float:
    import numpy as np

    q = np.asarray(query_emb)
    d = np.asarray(doc_emb)
    # cosine sim is already handled by ColBERT-normalized vectors; use dot product.
    sims = q @ d.T  # (n_query_tokens, n_doc_tokens)
    return float(sims.max(axis=1).sum())


def available() -> bool:
    return _get_model() is not None


# ---- keyword fallback ---------------------------------------------------------

_word_re = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_word_re.findall(text.lower()))


def _keyword_score(query: str, text: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    overlap = q & _tokens(text)
    return len(overlap) / len(q)


# ---- public scoring API -------------------------------------------------------


def warm_index() -> int:
    """Encode any notes whose cached embedding is missing or stale.

    Returns the number of notes (re)encoded. No-op when the model is unavailable or
    nothing changed, so it's cheap to call on a short interval.
    """
    model = _get_model()
    if model is None:
        return 0
    from . import images, notes_store  # local import to avoid an import cycle

    encoded = 0
    live_ids: set[str] = set()
    for summary in notes_store.list_notes():
        note = notes_store.get_note(summary.id)
        if note is None:
            continue
        live_ids.add(note.id)
        iso = note.updated_at.isoformat()
        cached = _cache.docs.get(note.id)
        if cached and cached[0] == iso:
            continue
        _encode_doc(model, note.id, iso, images.note_search_text(note.title, note.body))
        encoded += 1

    # Drop embeddings for notes that no longer exist.
    for stale_id in set(_cache.docs) - live_ids:
        del _cache.docs[stale_id]
    return encoded


def score_documents(query: str, docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """Score `query` against docs = [(id, updated_at_iso, text)]. Returns {id: score}.

    Uses ColBERT MaxSim when available, otherwise keyword overlap.
    """
    model = _get_model()
    if model is None:
        return {d[0]: _keyword_score(query, d[2]) for d in docs}

    query_emb = model.encode([query], is_query=True, show_progress_bar=False)[0]
    scores: dict[str, float] = {}
    for note_id, updated_at_iso, text in docs:
        if not text.strip():
            scores[note_id] = 0.0
            continue
        doc_emb = _encode_doc(model, note_id, updated_at_iso, text)
        scores[note_id] = _maxsim(query_emb, doc_emb)
    return scores
