"""Which "related" paragraphs are actually related: a cutoff on top of the scores.

``embeddings.score_documents`` ranks well but its scores can't be cut on their
own: ColBERT MaxSim is a sum of per-token best cosines, always well above zero
(function words and the [CLS]/[Q] markers match something in any paragraph) and
growing with the query's length. So the related endpoints keep a paragraph only
when it beats what an unrelated one would score *for this query*:

    relevance = (raw / n - baseline) / (1 - baseline)

where ``n`` is the number of query tokens (``raw / n`` is the mean best cosine per
query token; 1 is a perfect match) and ``baseline`` the typical unrelated level:
the median of ``raw / n`` over the candidates, but never below
``_UNRELATED_RATIO`` (and just that when there are too few candidates for a
median). 0 means "as good as a typical paragraph", 1 "a perfect match". It is
length-independent and needs no extra model pass (``n`` comes from the tokenizer).

Calibration (``scripts/calibrate_related.py``, Agent-ModernColBERT, 40 short notes
over 13 subjects, 1560 ordered pairs: 34 related, 100 same-subject neighbours,
1426 unrelated; 28 of the 40 paragraphs have a related one):

* ``raw / n`` alone is too compressed to cut: related median 0.79 (min 0.70) vs
  unrelated median 0.70 (p95 0.755, p99 0.77, max 0.78); the best cutoff (~0.75)
  kept 85% of related pairs and still 5% of unrelated ones.
* Unrelated ``raw / n``: median 0.702 (p90 0.744) -> ``_UNRELATED_RATIO``.
* ``relevance``: related median +0.27, same-subject neighbours -0.00, unrelated
  -0.04 (p95 +0.06, p99 +0.11, max +0.23). Pairs kept (related / neighbour /
  unrelated): 0.10 -> 88% / 11% / 1.7%; 0.14 -> 76% / 6% / 0.56%;
  0.18 -> 71% / 2% / 0.14%; 0.22 -> 65% / 0% / 0.07%.
* Per paragraph (what the chip shows, best of the 39 others): a true relation is
  found for 27/28 at 0.10, 25/28 at 0.14, 23/28 at 0.18, 21/28 at 0.22, while
  something off-topic is included for 15/40, 10/40, 4/40 and 1/40. Before any
  cutoff all 40/40 showed something. The misses are one-word paragraphs
  ("Bread", "Japan") and two only loosely linked notes about the same cat.

We take 0.18: most true relations survive and off-topic suggestions become rare.
"""

from __future__ import annotations

import statistics
from functools import lru_cache

from . import embeddings, textutil

RELATED_MIN_SCORE = 0.18
# Typed searches: a paragraph is shown by meaning when its relevance is at least
# SEARCH_MIN_SCORE and at least SEARCH_RELATIVE_SCORE of the best paragraph's.
# Measured on the labelled set (scripts/eval_search.py, tests/fixtures/search_eval:
# 99 answerable and 10 no-answer queries; docs/search-eval.md). With the old 0.10
# alone, 6 paragraphs per query were shown at 27% precision and 4 of the 10
# no-answer queries showed something. At 0.22 every no-answer query shows nothing
# (their best scores reached 0.199 and 0.185, for "how to file a patent
# application" and "mortgage refinance rates"; 0.22 leaves a margin for float16 on
# the GPU) and the answer is still shown for 88% of queries (true answers score
# from about 0.11). The relative cut (keep only what is close to the best match)
# then lifts precision from 63% to 78% at the same answer recall. Exact word
# matches are found by full-text search regardless.
SEARCH_MIN_SCORE = 0.22
SEARCH_RELATIVE_SCORE = 0.6
_UNRELATED_RATIO = 0.70
_MIN_CANDIDATES_FOR_MEDIAN = 5

# Keyword fallback (no model): share at least this many distinctive words.
KEYWORD_MIN_SHARED = 2
_COMMON_SHARE = 0.5  # a word in over half the paragraphs is not distinctive...
_COMMON_MIN_CORPUS = 20  # ...once there are enough paragraphs to tell



def related_scores(query: str, docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """{id: relevance} for the docs = [(id, version, text)] related to ``query``.

    Unrelated docs are left out. Uses the model's scores when it is available and
    shared distinctive words otherwise.
    """

    return related(docs, query=query)[0]


def related_note_scores(texts: list[str], docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """Like ``related_scores`` for a whole note given as its title and paragraphs."""

    return related(docs, texts=texts)[0]


def related(
    docs: list[tuple[str, str, str]], *, query: str | None = None, texts: list[str] | None = None
) -> tuple[dict[str, float], tuple[int, float] | None]:
    """``related_scores`` (``query``: a paragraph) or ``related_note_scores`` (``texts``).

    Also returns the calibration used, (query tokens, baseline), when the model
    scored them, so ``rescore`` can score more docs the same way later.
    """

    if query is not None:
        if not textutil.long_enough_to_compare(query) or not docs:
            return {}, None
        if not embeddings.available():
            return relevant_keyword_scores(query, docs), None
        tokens = query_token_count(query)
        if not tokens:
            return {}, None
        raw = embeddings.score_documents(query, docs, paragraph=True)
    else:
        texts = texts or []
        if not any(text.strip() for text in texts) or not docs:
            return {}, None
        if not embeddings.available():
            return relevant_keyword_scores("\n\n".join(texts), docs), None
        raw, tokens = embeddings.score_note(texts, docs)
        if not tokens:
            return {}, None
    if not raw:
        return {}, None
    baseline = model_baseline(raw, tokens)
    return normalized_scores(raw, tokens, baseline), (tokens, baseline)


def rescore(
    docs: list[tuple[str, str, str]],
    calibration: tuple[int, float],
    *,
    query: str | None = None,
    texts: list[str] | None = None,
) -> dict[str, float]:
    """Relevance of a few more docs for a query scored before by ``related``.

    Keeps that pass's baseline (the median over the whole corpus barely moves
    when a few notes change), so cached related lists can take in edits to
    other notes without scoring every paragraph again.
    """

    if not docs:
        return {}
    tokens, baseline = calibration
    if query is not None:
        raw = embeddings.score_documents(query, docs, paragraph=True)
    else:
        raw, _tokens = embeddings.score_note(texts or [], docs)
    return normalized_scores(raw, tokens, baseline)


def query_token_count(query: str) -> int:
    """How many vectors ColBERT makes for ``query`` (no padding, so no forward pass needed)."""

    try:
        lengths = embeddings.tokenized_lengths([query], is_query=True)
    except Exception:
        return 0
    return lengths[0] if lengths else 0


def relevant_model_scores(
    raw: dict[str, float],
    query_tokens: int,
    cutoff: float = RELATED_MIN_SCORE,
    relative: float = 0.0,
) -> dict[str, float]:
    """Normalize one query's raw MaxSim scores and keep those at ``cutoff`` or above,
    and (``relative`` > 0) at least ``relative`` times the best one.

    ``query_tokens`` is the ceiling: a perfect match has cosine 1 for every token.
    """

    if query_tokens <= 0 or not raw:
        return {}
    kept = normalized_scores(raw, query_tokens, model_baseline(raw, query_tokens), cutoff)
    if relative > 0 and kept:
        floor = relative * max(kept.values())
        kept = {doc_id: score for doc_id, score in kept.items() if score >= floor}
    return kept


def model_baseline(raw: dict[str, float], query_tokens: int) -> float:
    """The raw score of a typical unrelated paragraph for this query (see above)."""

    baseline = _UNRELATED_RATIO * float(query_tokens)
    if len(raw) >= _MIN_CANDIDATES_FOR_MEDIAN:
        baseline = max(baseline, statistics.median(raw.values()))
    return baseline


def normalized_scores(
    raw: dict[str, float], query_tokens: int, baseline: float, cutoff: float = RELATED_MIN_SCORE
) -> dict[str, float]:
    """``relevant_model_scores`` with a given baseline (e.g. one kept from a full pass)."""

    ceiling = float(query_tokens)
    span = ceiling - baseline
    if span <= 0:
        return {}
    normalized = {doc_id: (score - baseline) / span for doc_id, score in raw.items()}
    return {doc_id: score for doc_id, score in normalized.items() if score >= cutoff}


def relevant_keyword_scores(query: str, docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """Docs sharing distinctive words with ``query``: {id: share of its distinctive words}."""

    doc_words = {doc_id: _distinctive(text) for doc_id, _version, text in docs}
    wanted = _distinctive(query)
    if len(docs) >= _COMMON_MIN_CORPUS:
        counts: dict[str, int] = {}
        for words in doc_words.values():
            for word in words & wanted:
                counts[word] = counts.get(word, 0) + 1
        wanted = {word for word in wanted if counts.get(word, 0) <= _COMMON_SHARE * len(docs)}
    if not wanted:
        return {}
    needed = min(KEYWORD_MIN_SHARED, len(wanted))
    scores: dict[str, float] = {}
    for doc_id, words in doc_words.items():
        shared = len(wanted & words)
        if shared >= needed:
            scores[doc_id] = shared / len(wanted)
    return scores


@lru_cache(maxsize=1 << 16)
def _distinctive(text: str) -> frozenset[str]:
    # Cached: the keyword fallback tokenized every paragraph on every request.
    return frozenset(textutil.content_words(text, numbers=False))
