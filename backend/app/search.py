"""Full-text search + embedding search + related notes, all over SQLite blocks."""

import heapq
import math
import threading
from collections import OrderedDict
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from . import block_query, block_store, embeddings, relevance, textutil
from . import corpus as corpus_store
from .config import get_settings
from .models import NoteSummary, RelatedResult

_RELATED_EXCERPT_LENGTH = 240


# Related results per (note, k, block). An entry stays valid while its note is
# unchanged: edits to other notes are folded in by scoring just their paragraphs
# (``relevance.rescore``) instead of the whole corpus again.
_RELATED_CACHE_SIZE = 256
# More changed notes than this since an entry was computed: score everything again.
_RELATED_PATCH_LIMIT = 64


@dataclass
class _Related:
    corpus: object  # the snapshot the matches are current for
    state: object
    note: object  # the note's corpus entry then (identity: its title and text)
    query: str | None
    texts: list[str] | None
    calibration: tuple[int, float] | None
    # every other note clearing the cutoff: id -> (score, block id, block text)
    matches: dict[str, tuple[float, str, str]]


_related_cache: OrderedDict[tuple, _Related] = OrderedDict()
_related_lock = threading.Lock()


def related_notes(note_id: str, k: int = 5, block_id: str | None = None) -> list[RelatedResult]:
    settings = get_settings()
    db_path = settings.block_db_path
    corpus = corpus_store.snapshot(db_path)
    note = corpus.notes.get(note_id)
    if note is None:
        return []
    query: str | None = None
    if block_id is not None:
        # Only one block's text is needed (this used to render the whole note).
        text = block_store.block_text(db_path, note_id, block_id)
        if text is None:
            raise KeyError(f"block not found: {block_id}")
        # Its indexed text (with any image text) when it has one.
        query = next((row[3] for row in note.rows if row[0] == block_id), text)
        if not query.strip():
            return []
    state = embeddings.scoring_state(corpus)
    key = (db_path, note_id, k, block_id)
    with _related_lock:
        cached = _related_cache.get(key)
        if cached is not None:
            _related_cache.move_to_end(key)
    entry = None
    if state is not None and cached is not None and cached.state == state and cached.note is note:
        if cached.corpus is corpus:
            entry = cached
        elif cached.calibration is not None:
            entry = _patched(cached, corpus, note_id)
    if entry is None:
        entry = _related(corpus, note, block_id, query, state)
    if state is not None and entry is not cached:
        with _related_lock:
            _related_cache[key] = entry
            while len(_related_cache) > _RELATED_CACHE_SIZE:
                _related_cache.popitem(last=False)
    return _results(entry.matches, k)


def _related(corpus, note, block_id: str | None, query: str | None, state) -> _Related:
    """Score the note (or one of its paragraphs) against every other note's paragraphs."""

    # Scored as the embedding index sees them (title + paragraph, see corpus.Note.documents).
    candidates = [
        doc
        for document_id, other in sorted(corpus.notes.items())
        if document_id != note.document_id
        for doc in other.documents()
    ]
    texts = None
    if block_id is None:
        # A whole note is queried as its title plus each paragraph's saved query
        # encoding (clustered for long notes), so it needs no long model pass.
        texts = [note.title, *(row[3] for row in note.rows)]
    # Only paragraphs that clear the relevance cutoff, so every consumer (the
    # related panel, the "↗ N related" chip) shows real relations, not just the top k.
    scores, calibration = relevance.related(candidates, query=query, texts=texts)
    matches: dict[str, tuple[float, str, str]] = {}
    if scores:
        # In corpus order, so ties resolve as before.
        for bid, document_id, _, text in corpus:
            score = scores.get(bid)
            if score is not None and document_id != note.document_id:
                _keep_best(matches, document_id, score, bid, text)
    return _Related(corpus, state, note, query, texts, calibration, matches)


def _patched(cached: _Related, corpus, note_id: str) -> _Related | None:
    """``cached`` brought up to ``corpus`` by rescoring only the notes that changed."""

    changed = corpus_store.changed_between(cached.corpus, corpus)
    if changed is None or len(changed) > _RELATED_PATCH_LIMIT or note_id in changed:
        return None
    rows = [
        row for document_id in sorted(changed) if document_id in corpus.notes
        for row in corpus.notes[document_id].rows
    ]
    scores = relevance.rescore(
        [
            doc for document_id in sorted(changed) if document_id in corpus.notes
            for doc in corpus.notes[document_id].documents()
        ],
        cached.calibration,
        query=cached.query,
        texts=cached.texts,
    )
    matches = {nid: match for nid, match in cached.matches.items() if nid not in changed}
    for bid, document_id, _, text in rows:
        score = scores.get(bid)
        if score is not None:
            _keep_best(matches, document_id, score, bid, text)
    return _Related(
        corpus, cached.state, cached.note, cached.query, cached.texts, cached.calibration, matches
    )


def _keep_best(matches: dict, document_id: str, score: float, block_id: str, text: str) -> None:
    current = matches.get(document_id)
    if current is None or score > current[0]:
        matches[document_id] = (score, block_id, text)


def _results(matches: dict[str, tuple[float, str, str]], k: int) -> list[RelatedResult]:
    ranked = sorted(
        ((nid, match) for nid, match in matches.items() if match[0] > 0),
        key=lambda item: (-item[1][0], item[0]),
    )
    # Summaries only for the notes that can make the list (every one is active).
    if not ranked:
        return []
    summaries = {
        str(item["id"]): NoteSummary(**{key: item[key] for key in NoteSummary.model_fields})
        for item in block_store.list_documents(
            get_settings().block_db_path, [nid for nid, _match in ranked[: k * 2]]
        )
    }
    results = [
        RelatedResult(
            note=summaries[nid],
            score=match[0],
            matched_block_id=match[1] or None,
            matched_block_text=match[2][:_RELATED_EXCERPT_LENGTH],
        )
        for nid, match in ranked[: k * 2]
        if nid in summaries
    ]
    return results[:k]


# --- Unified search ------------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="meaning")
_RRF_K = 60
# A title containing every query word outweighs a block found by both other sources.
_FULL_TITLE_WEIGHT = 3.0
# How much covering all query words (title + text) multiplies a result's score.
_COVERAGE_WEIGHT = 1.0
_MEANING_CANDIDATES = 25
_PER_DOCUMENT = 3
_SNIPPET = 220


def unified_search(
    q: str,
    filters: block_query.BlockSearchFilters | None = None,
    limit: int = 30,
    *,
    with_rows: bool = False,
) -> list[dict[str, object]]:
    """One ranked list of blocks from exact words, meaning, and note titles.

    Each source ranks blocks on its own; reciprocal rank fusion combines the
    ranks, so no score scales need to agree. Recent edits get a small boost and
    no note contributes more than a few blocks.

    ``with_rows`` adds each block's full ``block_details`` row under ``"row"``
    (Ask quotes whole paragraphs, not snippets, and needn't load them again).
    """

    if not q.strip():
        return []
    db_path = get_settings().block_db_path
    allowed = block_query.allowed_block_ids(db_path, filters)

    def meaning() -> list[str]:
        docs = corpus_store.documents(corpus_store.snapshot(db_path))
        if allowed is not None:
            docs = [doc for doc in docs if doc[0] in allowed]
        scored = embeddings.available()
        scores = embeddings.score_documents(q, docs)
        if scored:
            # Only paragraphs that clear a relevance cutoff: the nearest paragraphs
            # to a query are not necessarily near it.
            scores = relevance.relevant_model_scores(
                scores, relevance.query_token_count(q), relevance.SEARCH_MIN_SCORE
            )
        best = heapq.nlargest(_MEANING_CANDIDATES, scores.items(), key=lambda item: item[1])
        return [block_id for block_id, score in best if score > 0]

    # Meaning (ColBERT, releases the GIL) runs alongside the SQLite word and title
    # lookups instead of after them.
    meaning_future = _pool.submit(meaning)
    words = block_query.search_blocks(db_path, q, filters, limit=100)
    word_ranking = [str(hit["block_id"]) for hit in words]

    title_hits = [
        match
        for match in block_query.title_matches(db_path, q)
        if allowed is None or match["block_id"] in allowed
    ]
    title_ranking = [str(match["block_id"]) for match in title_hits]
    # A title naming every query word is almost certainly the note being looked for;
    # one sharing a single word of several is weak evidence. Plain rank fusion would
    # count both the same, so weight title matches by how much of the query they cover.
    terms = set(textutil.query_terms(q))
    query_words = max(1, len(terms))
    title_weight = {
        str(match["block_id"]): _FULL_TITLE_WEIGHT
        if int(match["hits"]) >= query_words
        else int(match["hits"]) / query_words
        for match in title_hits
    }
    meaning_ranking = meaning_future.result()

    fused: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for reason, ranking in (
        ("words", word_ranking),
        ("meaning", meaning_ranking),
        ("title", title_ranking),
    ):
        for rank, block_id in enumerate(ranking):
            weight = title_weight[block_id] if reason == "title" else 1.0
            fused[block_id] = fused.get(block_id, 0.0) + weight / (_RRF_K + rank + 1)
            reasons.setdefault(block_id, []).append(reason)

    details = block_query.block_details(db_path, list(fused))
    now = datetime.now(timezone.utc)
    tokens = textutil.words(q)
    # Stopwords don't count: "is", "the", "a" are in nearly every paragraph, so
    # counting them boosted wordy paragraphs over the ones meaning ranked first.
    wanted = terms
    for block_id, row in details.items():
        fused[block_id] *= 1.0 + 0.15 * _recency(str(row["updated_at"]), now)
        # Each source ranks blocks on its own, so nothing above rewards a result whose
        # note covers every query word across its title and text ("hotel <name>": the
        # name in the title, "hotel" in the paragraph). Boost by the share it covers.
        if wanted:
            words = textutil.words(
                f"{row['document_title']} {row['text']} {row['asset_text']}"
            )
            covered = sum(1 for token in wanted if any(w.startswith(token) for w in words))
            fused[block_id] *= 1.0 + _COVERAGE_WEIGHT * covered / len(wanted)
    results: list[dict[str, object]] = []
    per_document: dict[str, int] = {}
    for block_id in sorted(details, key=lambda bid: -fused[bid]):
        row = details[block_id]
        document_id = str(row["document_id"])
        if per_document.get(document_id, 0) >= _PER_DOCUMENT:
            continue
        per_document[document_id] = per_document.get(document_id, 0) + 1
        snippet, highlights = _snippet(str(row["text"]), tokens)
        if not highlights and row["asset_text"]:
            # Matched by the text read from a picture/PDF: show that passage instead.
            in_asset, asset_highlights = _snippet(str(row["asset_text"]), tokens)
            if asset_highlights or not snippet:
                snippet, highlights = in_asset, asset_highlights
        result = {
            "block_id": block_id,
            "document_id": document_id,
            "document_title": row["document_title"],
            "block_type": row["type"],
            "text": snippet,
            "highlights": highlights,
            "matched": reasons[block_id],
            "score": round(fused[block_id], 6),
        }
        if with_rows:
            result["row"] = row
        results.append(result)
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
    """A window of ``text`` around the first matched word, plus highlight ranges.

    Words match whole and ignore case and accents, like the word search. Ranges are
    UTF-16 offsets into the snippet, because the browser slices strings that way
    (an emoji counts as two).
    """

    wanted = set(tokens)
    matched = [(start, end) for start, end, word in textutil.word_spans(text) if word in wanted]
    start, stop = textutil.window(text, matched[0][0] if matched else 0, _SNIPPET)
    window = text[start:stop]
    prefix = "…" if start > 0 else ""
    suffix = "…" if stop < len(text) else ""
    snippet = f"{prefix}{window}{suffix}"
    shift = len(prefix) - start
    ranges: list[list[int]] = []
    for word_start, word_end in matched:
        if word_start < start or word_end > stop:
            continue
        # Keep combining accents (decomposed "é") inside the highlight.
        while word_end < stop and unicodedata.combining(text[word_end]):
            word_end += 1
        ranges.append([_utf16(snippet, word_start + shift), _utf16(snippet, word_end + shift)])
    return snippet, ranges


def _utf16(text: str, index: int) -> int:
    return index + sum(1 for character in text[:index] if ord(character) > 0xFFFF)
