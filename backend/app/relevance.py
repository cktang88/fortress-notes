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

from . import block_query, embeddings

RELATED_MIN_SCORE = 0.18
_UNRELATED_RATIO = 0.70
_MIN_CANDIDATES_FOR_MEDIAN = 5

# Keyword fallback (no model): share at least this many distinctive words.
KEYWORD_MIN_SHARED = 2
_COMMON_SHARE = 0.5  # a word in over half the paragraphs is not distinctive...
_COMMON_MIN_CORPUS = 20  # ...once there are enough paragraphs to tell
_STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing down during
    each few for from further get got had has have having he her here hers herself him
    himself his how i if in into is it its itself just let me more most my myself next no
    nor not now of off on once only or other our ours ourselves out over own same she
    should so some still such than that the their theirs them themselves then there these
    they this those through to too under until up very was we were what when where which
    while who whom why will with would you your yours yourself yourselves""".split()
)


def related_scores(query: str, docs: list[tuple[str, str, str]]) -> dict[str, float]:
    """{id: relevance} for the docs = [(id, version, text)] related to ``query``.

    Unrelated docs are left out. Uses the model's scores when it is available and
    shared distinctive words otherwise.
    """

    if not query.strip() or not docs:
        return {}
    if not embeddings.available():
        return relevant_keyword_scores(query, docs)
    tokens = _query_token_count(query)
    if not tokens:
        return {}
    return relevant_model_scores(embeddings.score_documents(query, docs), tokens)


def _query_token_count(query: str) -> int:
    """How many vectors ColBERT makes for ``query`` (no padding, so no forward pass needed)."""

    model = embeddings._get_model()
    try:
        return len(model.tokenize([query], is_query=True)["input_ids"][0])
    except Exception:
        return 0


def relevant_model_scores(
    raw: dict[str, float], query_tokens: int, cutoff: float = RELATED_MIN_SCORE
) -> dict[str, float]:
    """Normalize one query's raw MaxSim scores and keep those at ``cutoff`` or above.

    ``query_tokens`` is the ceiling: a perfect match has cosine 1 for every token.
    """

    if query_tokens <= 0 or not raw:
        return {}
    ceiling = float(query_tokens)
    baseline = _UNRELATED_RATIO * ceiling
    if len(raw) >= _MIN_CANDIDATES_FOR_MEDIAN:
        baseline = max(baseline, statistics.median(raw.values()))
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


def _distinctive(text: str) -> set[str]:
    return {
        word
        for word in block_query.query_tokens(text)
        if word not in _STOPWORDS and not word.isdigit() and (len(word) >= 3 or not word.isascii())
    }
