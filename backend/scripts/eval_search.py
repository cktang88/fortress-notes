"""Search quality: our ColBERT path against a dense model, BM25 and hybrids.

Two parts, both through the app's own code where the app has one:

* ``fortress``: the labelled personal-notes set in ``tests/fixtures/search_eval``
  (notes + graded queries + related-note topics). Notes go into a real block
  store; ColBERT is scored with ``embeddings.score_documents`` (the app's encoder
  and MaxSim index), keyword with ``block_query.search_blocks`` (the FTS5 path),
  and the whole app with ``search.unified_search`` and ``search.related_notes``,
  including their cutoffs (``SEARCH_MIN_SCORE``, ``RELATED_MIN_SCORE``).
* ``beir``: BEIR datasets (e.g. scifact, nfcorpus) as a sanity check against
  the models' published numbers. Download each dataset's
  ``corpus/corpus-00000-of-00001.parquet`` and ``queries/queries-00000-of-00001.parquet``
  from ``BeIR/<name>`` and ``test.tsv`` from ``BeIR/<name>-qrels`` on the Hugging
  Face hub into ``<dir>/<name>/`` (flat). Encodings are cached in ``--cache``.

Systems: ``colbert`` (ours), ``colbert-instruct`` (BEIR only: queries in the
model card's AgentIR format), ``dense`` (single vector: Alibaba-NLP/gte-modernbert-base,
the dense model GTE-ModernColBERT, our model's parent, was trained from: same
backbone and size, so this isolates single- vs multi-vector), ``bm25`` (FTS5 bm25,
words OR-ed), ``fts`` (our AND query), ``rrf(a+b)`` hybrids, and ``unified``
(the app's search, Fortress only).

Run from ``backend/`` (models from the Hugging Face cache)::

    HF_HUB_OFFLINE=1 uv run --frozen python scripts/eval_search.py fortress --report ../docs/search-eval.md
    HF_HUB_OFFLINE=1 uv run --frozen python scripts/eval_search.py beir --data /tmp/beir \\
        --datasets scifact,nfcorpus --cache /tmp/beir-cache --report /tmp/beir.md

``--no-dense`` skips the dense model (it is not a dependency of the app: it comes
with sentence-transformers, which PyLate already installs, and is downloaded on
first use, ~300 MB).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "search_eval"
DENSE_MODEL = "Alibaba-NLP/gte-modernbert-base"
DENSE_REVISION = "e7f32e3c00f91d699e8c43b53106206bcc72bb22"  # 2025-07-04
# Published BEIR nDCG@10. GTE-ModernColBERT-v1 (our model's parent; Agent-ModernColBERT
# publishes BrowseComp-Plus only) and gte-modernbert-base, from their model cards.
PUBLISHED = {
    "scifact": {"colbert": 76.34, "dense": 77.4},
    "nfcorpus": {"colbert": 37.93, "dense": 36.44},
}
# The query layout the Agent-ModernColBERT card asks for (the "\\n" is literal).
INSTRUCT_PREFIX = (
    "Instruct: Given a user's reasoning followed by a web search query, "
    "retrieve relevant passages that answer the query while incorporating the user's reasoning"
    "\\nQuery:"
)
RRF_K = 60

Ranking = list[tuple[str, float]]  # (doc id, score), best first


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- Metrics ----------------------------------------------------------------------


def ndcg(ranked: list[str], relevant: dict[str, int], k: int = 10) -> float:
    """nDCG@k with linear gains (pytrec_eval / BEIR)."""

    dcg = sum(relevant.get(doc, 0) / math.log2(rank + 2) for rank, doc in enumerate(ranked[:k]))
    ideal = sorted(relevant.values(), reverse=True)[:k]
    best = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(ideal))
    return dcg / best if best > 0 else 0.0


def recall(ranked: list[str], relevant: dict[str, int], k: int) -> float:
    wanted = {doc for doc, grade in relevant.items() if grade > 0}
    return len(wanted & set(ranked[:k])) / len(wanted) if wanted else 0.0


def mrr(ranked: list[str], relevant: dict[str, int], k: int = 10) -> float:
    for rank, doc in enumerate(ranked[:k]):
        if relevant.get(doc, 0) > 0:
            return 1.0 / (rank + 1)
    return 0.0


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking):
            fused[doc] = fused.get(doc, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused, key=lambda doc: -fused[doc])


def summarize(runs: dict[str, list[str]], qrels: dict[str, dict[str, int]]) -> dict[str, float]:
    """Mean metrics (x100) over the queries that have relevant docs."""

    judged = [qid for qid in qrels if any(grade > 0 for grade in qrels[qid].values())]
    out = {}
    for name, fn in (
        ("nDCG@10", lambda r, q: ndcg(r, q, 10)),
        ("R@10", lambda r, q: recall(r, q, 10)),
        ("R@100", lambda r, q: recall(r, q, 100)),
        ("MRR@10", lambda r, q: mrr(r, q, 10)),
    ):
        out[name] = 100 * statistics.fmean(fn(runs.get(qid, []), qrels[qid]) for qid in judged)
    return out


# --- Systems -----------------------------------------------------------------------


def activate_settings(notes_dir: Path, **overrides):
    """App settings over ``notes_dir`` (never reads backend/.env)."""

    from app import config
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        notes_dir=notes_dir,
        welcome_note=False,
        vision_enabled=False,
        auto_backup_enabled=False,
        openrouter_api_key="",
        reindex_interval_s=3600.0,
        **overrides,
    )
    config.activate(settings)
    return settings


def colbert_model():
    from app import embeddings

    model = embeddings.load_model()
    if model is None:
        sys.exit("ColBERT model unavailable (is it in the Hugging Face cache?)")
    return model


def colbert_index(docs: list[tuple[str, str]], cache: Path | None = None) -> float:
    """Encode (id, text) docs with the app's encoder into the app's MaxSim index.

    The persistent store is off (no disk), so the app's own scoring
    (``embeddings.score_documents``) runs on exactly these encodings. Returns the
    encoding time (0 when read from ``cache``, float16 there to save disk).
    """

    import torch

    from app import embeddings

    model = colbert_model()
    embeddings._store_path = lambda: None  # in memory only
    embeddings._cache.store_loaded = False
    embeddings._load_store()
    encoded = None
    if cache is not None and cache.exists():
        saved = torch.load(cache)
        if saved["ids"] == [doc_id for doc_id, _ in docs]:
            encoded = [tensor.float() for tensor in saved["vectors"]]
    elapsed = 0.0
    if encoded is None:
        order = sorted(range(len(docs)), key=lambda i: len(docs[i][1]))  # as warm_index does
        encoded = [None] * len(docs)
        started = time.perf_counter()
        step = 256
        for start in range(0, len(order), step):
            chunk = order[start:start + step]
            vectors = embeddings._encode(
                model, [docs[i][1] for i in chunk], is_query=False, background=False
            )
            for i, vector in zip(chunk, vectors):
                encoded[i] = vector
            _say(f"  colbert encoded {min(start + step, len(order))}/{len(order)} "
                 f"({time.perf_counter() - started:.0f}s)")
        elapsed = time.perf_counter() - started
        if cache is not None:
            torch.save({"ids": [doc_id for doc_id, _ in docs],
                        "vectors": [vector.half() for vector in encoded]}, cache)
    with embeddings._index_lock:
        for (doc_id, text), vector in zip(docs, encoded):
            embeddings._cache.index.put(doc_id, embeddings._text_hash(text), "", vector)
        embeddings._cache.index.trim()
    return elapsed


def colbert_rank(query: str, docs: list[tuple[str, str, str]], k: int = 100) -> Ranking:
    from app import embeddings

    scores = embeddings.score_documents(query, docs)
    return sorted(scores.items(), key=lambda item: -item[1])[:k]


class Dense:
    """Single-vector baseline: cosine of normalized CLS embeddings (sentence-transformers)."""

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(DENSE_MODEL, revision=DENSE_REVISION, device="cpu")
        self.ids: list[str] = []
        self.matrix = None

    def index(self, docs: list[tuple[str, str]], cache: Path | None = None) -> float:
        import torch

        self.ids = [doc_id for doc_id, _ in docs]
        if cache is not None and cache.exists():
            saved = torch.load(cache)
            if saved["ids"] == self.ids:
                self.matrix = saved["matrix"].float()
                return 0.0
        started = time.perf_counter()
        self.matrix = self.model.encode(
            [text for _, text in docs], batch_size=16, convert_to_tensor=True,
            normalize_embeddings=True, show_progress_bar=len(docs) > 500,
        ).float().cpu()
        elapsed = time.perf_counter() - started
        if cache is not None:
            torch.save({"ids": self.ids, "matrix": self.matrix.half()}, cache)
        return elapsed

    def encode_query(self, query: str):
        return self.model.encode([query], convert_to_tensor=True, normalize_embeddings=True)[0].float().cpu()

    def rank(self, query: str, k: int = 100) -> Ranking:
        import torch

        scores = self.matrix @ self.encode_query(query)
        top = torch.topk(scores, min(k, len(self.ids)))
        return [(self.ids[i], float(s)) for s, i in zip(top.values.tolist(), top.indices.tolist())]


class Keyword:
    """FTS5 over (id, text), with the block store's tokenizer: bm25 over the query's
    words OR-ed (classic BM25), or AND-ed as ``block_query.build_fts_match`` does."""

    def __init__(self, docs: list[tuple[str, str]]) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE fts USING fts5(doc_id UNINDEXED, text)")
        self.db.executemany("INSERT INTO fts VALUES (?, ?)", docs)

    def rank(self, query: str, k: int = 100, *, conjunctive: bool = False) -> Ranking:
        from app import block_query, textutil

        if conjunctive:
            match = block_query.build_fts_match(query)
        else:
            words = sorted(set(textutil.words(query)) - textutil.STOPWORDS) or textutil.words(query)
            match = " OR ".join(f'"{word}"' for word in words) if words else None
        if not match:
            return []
        rows = self.db.execute(
            "SELECT doc_id, bm25(fts) FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
            (match, k),
        ).fetchall()
        return [(doc_id, -score) for doc_id, score in rows]


def format_instruct(query: str) -> str:
    return f"{INSTRUCT_PREFIX}Reasoning: Empty\n\nQuery: {query}"


# --- BEIR --------------------------------------------------------------------------


def load_beir(folder: Path) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, dict[str, int]]]:
    import pandas as pd

    corpus = pd.read_parquet(folder / "corpus-00000-of-00001.parquet")
    queries = pd.read_parquet(folder / "queries-00000-of-00001.parquet")
    qrels: dict[str, dict[str, int]] = {}
    for line in (folder / "test.tsv").read_text().splitlines()[1:]:
        qid, doc_id, grade = line.split("\t")
        qrels.setdefault(qid, {})[doc_id] = int(grade)
    docs = [
        (str(doc_id), f"{title} {text}".strip())
        for doc_id, title, text in zip(corpus["_id"], corpus["title"], corpus["text"])
    ]
    texts = {str(qid): text for qid, text in zip(queries["_id"], queries["text"]) if str(qid) in qrels}
    return docs, texts, qrels


def run_beir(args) -> str:
    from app import embeddings

    lines = ["## BEIR sanity check", ""]
    lines.append(
        "nDCG@10 x100 over the full test split. Published: GTE-ModernColBERT-v1 (the parent "
        "of Agent-ModernColBERT, which publishes no BEIR numbers) and gte-modernbert-base, "
        "from their model cards."
    )
    lines.append("")
    table = ["| dataset | system | nDCG@10 | R@10 | R@100 | MRR@10 | published nDCG@10 | query ms (p50) | index s |",
             "|---|---|---|---|---|---|---|---|---|"]
    cache = Path(args.cache) if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        activate_settings(Path(directory), embeddings_enabled=True, embeddings_threads=args.threads)
        dense = None if args.no_dense else Dense()
        for name in args.datasets.split(","):
            docs, queries, qrels = load_beir(Path(args.data) / name)
            if args.max_queries:
                queries = dict(list(queries.items())[: args.max_queries])
                qrels = {qid: qrels[qid] for qid in queries}
            _say(f"{name}: {len(docs)} docs, {len(queries)} queries")
            runs: dict[str, dict[str, list[str]]] = {}
            timing: dict[str, tuple[float, float]] = {}
            index_s = colbert_index(docs, cache / f"{name}-colbert.pt" if cache else None)
            triples = [(doc_id, "", text) for doc_id, text in docs]
            for system, shape in (("colbert", lambda q: q), ("colbert-instruct", format_instruct)):
                runs[system], times = {}, []
                for qid, text in queries.items():
                    started = time.perf_counter()
                    runs[system][qid] = [d for d, _ in colbert_rank(shape(text), triples)]
                    times.append(time.perf_counter() - started)
                timing[system] = (statistics.median(times) * 1000, index_s)
            embeddings._cache.index = embeddings._Index()  # free the memory
            keyword = Keyword(docs)
            for system, conjunctive in (("bm25", False), ("fts (AND)", True)):
                runs[system], times = {}, []
                for qid, text in queries.items():
                    started = time.perf_counter()
                    runs[system][qid] = [d for d, _ in keyword.rank(text, conjunctive=conjunctive)]
                    times.append(time.perf_counter() - started)
                timing[system] = (statistics.median(times) * 1000, 0.0)
            if dense is not None:
                index_s = dense.index(docs, cache / f"{name}-dense.pt" if cache else None)
                runs["dense"], times = {}, []
                for qid, text in queries.items():
                    started = time.perf_counter()
                    runs["dense"][qid] = [d for d, _ in dense.rank(text)]
                    times.append(time.perf_counter() - started)
                timing["dense"] = (statistics.median(times) * 1000, index_s)
                runs["rrf(dense+bm25)"] = {
                    qid: rrf(runs["dense"][qid], runs["bm25"][qid])[:100] for qid in queries
                }
            runs["rrf(colbert+bm25)"] = {
                qid: rrf(runs["colbert"][qid], runs["bm25"][qid])[:100] for qid in queries
            }
            for system, run in runs.items():
                metrics = summarize(run, qrels)
                published = PUBLISHED.get(name, {}).get(system.split("-")[0] if system.startswith("colbert") and "+" not in system else system)
                ms, index_s = timing.get(system, (None, None))
                table.append(
                    f"| {name} | {system} | {metrics['nDCG@10']:.1f} | {metrics['R@10']:.1f} | "
                    f"{metrics['R@100']:.1f} | {metrics['MRR@10']:.1f} | "
                    f"{'' if published is None else f'{published:.1f}'} | "
                    f"{'' if ms is None else f'{ms:.0f}'} | {'' if not index_s else f'{index_s:.0f}'} |"
                )
                _say(table[-1])
    return "\n".join(lines + table) + "\n"


# --- Fortress set ------------------------------------------------------------------


def load_fixture(folder: Path = FIXTURES) -> tuple[list[dict], list[dict]]:
    notes = json.loads((folder / "notes.json").read_text(encoding="utf-8"))["notes"]
    queries = json.loads((folder / "queries.json").read_text(encoding="utf-8"))["queries"]
    return notes, queries


def related_ok(notes: list[dict]) -> dict[str, set[str]]:
    """note id -> the notes it may be shown as related to (same topic, or 'also')."""

    ok = {note["id"]: set() for note in notes}
    for a in notes:
        for b in notes:
            if a is not b and a["topic"] == b["topic"]:
                ok[a["id"]].add(b["id"])
        for other in a.get("also", []):
            ok[a["id"]].add(other)
            ok[other].add(a["id"])
    return ok


def same_topic(notes: list[dict]) -> dict[str, set[str]]:
    return {
        a["id"]: {b["id"] for b in notes if b is not a and b["topic"] == a["topic"]} for a in notes
    }


class Workspace:
    """The fixture notes in a real block store, indexed by the app's ColBERT indexer.

    ``block_para`` maps each block to its paragraph id ('<note>.<i>'): a paragraph
    written as a list becomes several blocks.
    """

    def __init__(self, notes: list[dict], directory: Path, *, threads: int = 0) -> None:
        from datetime import datetime, timedelta, timezone

        from app import block_store, corpus

        self.settings = activate_settings(
            directory, embeddings_enabled=True, embeddings_threads=threads
        )
        self.db = self.settings.block_db_path
        # Every note edited at the same time, a month ago: no recency differences.
        when = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        for note in notes:
            block_store.create_document(
                self.db, note["id"], note["title"], "\n\n".join(note["paras"]), when, when
            )
        self.corpus = corpus.snapshot(self.db)
        self.block_para: dict[str, str] = {}
        by_note = {note["id"]: note for note in notes}
        for document_id, entry in self.corpus.notes.items():
            paras = [_plain(text) for text in by_note[document_id]["paras"]]
            index = 0
            for block_id, _document_id, _version, text in entry.rows:
                plain = _plain(text)
                while index < len(paras) and plain not in paras[index]:
                    index += 1
                if index == len(paras):
                    raise AssertionError(f"block {text!r} of {document_id} matches no paragraph")
                self.block_para[block_id] = f"{document_id}.{index}"
        mapped = set(self.block_para.values())
        missing = [f"{n['id']}.{i}" for n in notes for i in range(len(n["paras"]))
                   if f"{n['id']}.{i}" not in mapped]
        if missing:
            raise AssertionError(f"paragraphs without a block: {missing}")
        # As the app scores them (note title + paragraph); every system gets this text.
        self.docs = corpus.documents(self.corpus)
        self.first_block = {
            document_id: entry.rows[0][0] for document_id, entry in self.corpus.notes.items()
        }

    def index(self) -> float:
        from app import embeddings

        colbert_model()
        started = time.perf_counter()
        while embeddings.warm_index():
            pass
        return time.perf_counter() - started

    def paras(self, block_ids) -> list[str]:
        """Paragraph ids in the order their first block appears."""

        out: dict[str, None] = {}
        for block_id in block_ids:
            para = self.block_para.get(block_id)
            if para is not None:
                out.setdefault(para, None)
        return list(out)


def _plain(text: str) -> str:
    """Text without Markdown list/code markup and with collapsed spaces (for mapping)."""

    text = re.sub(r"```\w*", " ", text)
    text = re.sub(r"(?m)^\s*(?:[-*]|\d+\.)\s+", " ", text).replace("`", "")
    return " ".join(text.split())


def _timed_runs(queries: list[dict], fn: Callable[[str], object]) -> tuple[dict, float]:
    out, times = {}, []
    for query in queries:
        started = time.perf_counter()
        out[query["id"]] = fn(query["query"])
        times.append(time.perf_counter() - started)
    return out, statistics.median(times) * 1000


def _auc(positives: list[float], negatives: list[float]) -> float:
    """Chance that a relevant paragraph outscores an irrelevant one across all queries:
    how well one fixed score cutoff can separate them (1 = perfectly)."""

    if not positives or not negatives:
        return float("nan")
    ordered = sorted([(s, 1) for s in positives] + [(s, 0) for s in negatives])
    rank_sum, rank = 0.0, 0
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        average = (rank + 1 + rank + (j - i)) / 2
        rank_sum += average * sum(label for _s, label in ordered[i:j])
        rank += j - i
        i = j
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def shown_metrics(shown: dict[str, list[str]], queries: list[dict]) -> dict[str, float]:
    """What a user sees: precision of everything shown, recall of what should have
    been, and how many no-answer queries show nothing."""

    hits = total = found = wanted = 0
    empty_no_answer = no_answer = 0
    false_positive_queries = 0
    for query in queries:
        relevant = {pid for pid, grade in query["relevant"].items() if grade > 0}
        got = shown.get(query["id"], [])
        if not relevant:
            no_answer += 1
            empty_no_answer += not got
            continue
        hits += sum(1 for pid in got if pid in relevant)
        total += len(got)
        found += len(relevant & set(got))
        wanted += len(relevant)
        false_positive_queries += any(pid not in relevant for pid in got)
    answerable = len(queries) - no_answer
    return {
        "precision": 100 * hits / total if total else float("nan"),
        "recall": 100 * found / wanted if wanted else float("nan"),
        "shown/query": total / answerable if answerable else float("nan"),
        "no-answer empty": 100 * empty_no_answer / no_answer if no_answer else float("nan"),
    }


def run_fortress(args) -> tuple[str, dict]:
    """Every system on the Fortress set; the markdown section and the raw numbers."""

    from app import embeddings, relevance, search
    from app import block_query

    notes, queries = load_fixture()
    qrels = {query["id"]: query["relevant"] for query in queries}
    results: dict = {"systems": {}, "shown": {}, "related": {}, "latency": {}, "per_kind": {}}
    with tempfile.TemporaryDirectory() as directory:
        ws = Workspace(notes, Path(directory), threads=args.threads)
        results["index_s"] = {"colbert": ws.index()}
        texts = {block_id: text for block_id, _v, text in ws.docs}
        runs: dict[str, dict[str, list[str]]] = {}
        scores_by: dict[str, dict[str, dict[str, float]]] = {}

        # ColBERT, as embeddings.score_documents returns it (raw MaxSim), then the cutoff.
        def colbert(query: str):
            raw = embeddings.score_documents(query, ws.docs)
            normalized = relevance.normalized_scores(
                raw, relevance.query_token_count(query),
                relevance.model_baseline(raw, relevance.query_token_count(query)),
                cutoff=-math.inf,
            )
            return raw, normalized

        out, ms = _timed_runs(queries, colbert)
        results["latency"]["colbert"] = ms
        runs["colbert"] = {
            qid: ws.paras(sorted(raw, key=lambda b: -raw[b])) for qid, (raw, _n) in out.items()
        }
        scores_by["colbert"] = {qid: _para_scores(ws, norm) for qid, (_raw, norm) in out.items()}
        def shown(norm: dict[str, float]) -> list[str]:
            # What unified search takes from meaning (relevance.relevant_model_scores).
            kept = {b: s for b, s in norm.items() if s >= relevance.SEARCH_MIN_SCORE}
            floor = relevance.SEARCH_RELATIVE_SCORE * max(kept.values(), default=0.0)
            return ws.paras(sorted((b for b, s in kept.items() if s >= floor), key=lambda b: -kept[b]))

        shown_colbert = {qid: shown(norm) for qid, (_raw, norm) in out.items()}
        runs["colbert, after cutoffs"] = shown_colbert

        keyword = Keyword([(block_id, text) for block_id, text in texts.items()])
        out, ms = _timed_runs(queries, lambda q: keyword.rank(q))
        results["latency"]["bm25"] = ms
        runs["bm25"] = {qid: ws.paras(b for b, _s in ranking) for qid, ranking in out.items()}
        out, ms = _timed_runs(
            queries, lambda q: block_query.search_blocks(ws.db, q, limit=100)
        )
        results["latency"]["fts"] = ms
        runs["fts"] = {qid: ws.paras(str(hit["block_id"]) for hit in hits) for qid, hits in out.items()}

        dense = None
        if not args.no_dense:
            dense = Dense()
            results["index_s"]["dense"] = dense.index(list(texts.items()))
            dense.encode_query("warm up")
            out, ms = _timed_runs(queries, lambda q: dense.rank(q, k=len(texts)))
            results["latency"]["dense"] = ms
            runs["dense"] = {qid: ws.paras(b for b, _s in ranking) for qid, ranking in out.items()}
            scores_by["dense"] = {qid: _para_scores(ws, dict(ranking)) for qid, ranking in out.items()}
            runs["rrf(dense+bm25)"] = {
                qid: rrf(runs["dense"][qid][:100], runs["bm25"][qid]) for qid in qrels
            }
        runs["rrf(colbert+bm25)"] = {
            qid: rrf(runs["colbert"][qid][:100], runs["bm25"][qid]) for qid in qrels
        }

        search.unified_search("warm up")
        out, ms = _timed_runs(queries, lambda q: search.unified_search(q, limit=30))
        results["latency"]["unified"] = ms
        runs["unified"] = {
            qid: ws.paras(str(hit["block_id"]) for hit in hits) for qid, hits in out.items()
        }
        results["unified_hits"] = out

        for system, run in runs.items():
            results["systems"][system] = summarize(run, qrels)
            for kind in dict.fromkeys(query["kind"] for query in queries):
                subset = {q["id"]: qrels[q["id"]] for q in queries if q["kind"] == kind}
                if any(subset.values()):
                    results["per_kind"].setdefault(kind, {})[system] = summarize(run, subset)["nDCG@10"]
        results["shown"]["colbert, search cutoffs"] = shown_metrics(shown_colbert, queries)
        results["shown"]["unified (all shown)"] = shown_metrics(runs["unified"], queries)
        results["shown"]["unified top 5"] = shown_metrics(
            {qid: run[:5] for qid, run in runs["unified"].items()}, queries
        )
        results["shown"]["fts (all shown)"] = shown_metrics(runs["fts"], queries)
        for system, by_query in scores_by.items():
            positives, negatives = [], []
            for qid, para_scores in by_query.items():
                for pid, score in para_scores.items():
                    (positives if qrels[qid].get(pid, 0) > 0 else negatives).append(score)
            results.setdefault("auc", {})[system] = _auc(positives, negatives)
        results["runs"] = runs
        results["shown_colbert"] = shown_colbert
        results["related"] = related_quality(ws, notes)
    return fortress_report(results, queries), results


def _para_scores(ws: Workspace, block_scores: dict[str, float]) -> dict[str, float]:
    """Best block score per paragraph."""

    out: dict[str, float] = {}
    for block_id, score in block_scores.items():
        para = ws.block_para.get(block_id)
        if para is not None and score > out.get(para, -math.inf):
            out[para] = score
    return out


def related_quality(ws: Workspace, notes: list[dict]) -> dict:
    """Related notes (whole note and per paragraph) as the app shows them."""

    from app import search

    ok = related_ok(notes)
    topic = same_topic(notes)
    out: dict = {}
    for label, per_block in (("note", False), ("paragraph", True)):
        shown = correct = wanted = found = 0
        lonely = lonely_with_wrong = 0
        wrong_examples = []
        started = time.perf_counter()
        calls = 0
        for note in notes:
            if per_block:
                blocks = [b for b, p in ws.block_para.items() if p.startswith(note["id"] + ".")]
            else:
                blocks = [None]
            for block_id in blocks:
                calls += 1
                hits = [r.note.id for r in search.related_notes(note["id"], k=5, block_id=block_id)]
                shown += len(hits)
                good = [h for h in hits if h in ok[note["id"]]]
                correct += len(good)
                if not per_block:
                    wanted += len(topic[note["id"]])
                    found += len(set(hits) & topic[note["id"]])
                if not ok[note["id"]]:
                    lonely += 1
                    lonely_with_wrong += bool(hits)
                for h in hits:
                    if h not in ok[note["id"]] and len(wrong_examples) < 12:
                        wrong_examples.append(f"{note['id']}{'' if block_id is None else '.' + ws.block_para[block_id].split('.')[-1]} -> {h}")
        out[label] = {
            "lists": calls,
            "shown": shown,
            "precision": 100 * correct / shown if shown else float("nan"),
            "recall (same topic, top 5)": 100 * found / wanted if wanted else float("nan"),
            "lonely notes showing something": f"{lonely_with_wrong}/{lonely}",
            "ms per list": 1000 * (time.perf_counter() - started) / max(calls, 1),
            "wrong": wrong_examples,
        }
    return out


def _f(value: float) -> str:
    return "–" if value != value else f"{value:.1f}"


def fortress_report(results: dict, queries: list[dict]) -> str:
    from collections import Counter

    kinds = Counter(query["kind"] for query in queries)
    lines = ["## Fortress notes set", ""]
    lines.append(
        f"{len(queries)} queries ({', '.join(f'{k} {n}' for k, n in kinds.items())}) over "
        "`backend/tests/fixtures/search_eval`. Rankings are of paragraphs (a block maps to "
        "its paragraph); metrics x100 over answerable queries."
    )
    lines += ["", "### Ranking quality", "",
              "| system | nDCG@10 | R@10 | R@100 | MRR@10 | query ms (p50) |", "|---|---|---|---|---|---|"]
    for system, m in results["systems"].items():
        ms = results["latency"].get(system)
        lines.append(f"| {system} | {_f(m['nDCG@10'])} | {_f(m['R@10'])} | {_f(m['R@100'])} | "
                     f"{_f(m['MRR@10'])} | {'' if ms is None else f'{ms:.0f}'} |")
    systems = list(results["systems"])
    lines += ["", "### nDCG@10 by query kind", "",
              "| kind | " + " | ".join(systems) + " |", "|---" * (len(systems) + 1) + "|"]
    for kind, row in results["per_kind"].items():
        lines.append(f"| {kind} | " + " | ".join(_f(row.get(s, float('nan'))) for s in systems) + " |")
    lines += ["", "### What search shows", "",
              "Precision/recall of everything shown (answerable queries), and the share of "
              "no-answer queries that show nothing.", "",
              "| what | precision | recall | shown per query | no-answer queries empty |",
              "|---|---|---|---|---|"]
    for name, m in results["shown"].items():
        lines.append(f"| {name} | {_f(m['precision'])} | {_f(m['recall'])} | "
                     f"{_f(m['shown/query'])} | {_f(m['no-answer empty'])}% |")
    if results.get("auc"):
        lines += ["", "Separability of relevant vs irrelevant paragraph scores across all queries "
                  "(AUC; how well one fixed cutoff can work): "
                  + ", ".join(f"{s} {v:.3f}" for s, v in results["auc"].items()) + "."]
    lines += ["", "### Related notes (RELATED_MIN_SCORE)", "",
              "| list | lists | shown | precision | recall (same topic, top 5) | lonely notes showing something | ms per list |",
              "|---|---|---|---|---|---|---|"]
    for label, m in results["related"].items():
        lines.append(f"| {label} | {m['lists']} | {m['shown']} | {_f(m['precision'])} | "
                     f"{_f(m['recall (same topic, top 5)'])} | {m['lonely notes showing something']} | "
                     f"{m['ms per list']:.0f} |")
    lines += ["", f"Index time (CPU, {sum(1 for _ in results['runs']['colbert'])} queries): "
              + ", ".join(f"{s} {v:.1f}s" for s, v in results["index_s"].items()) + "."]
    return "\n".join(lines) + "\n"


def misses(results: dict, queries: list[dict], system: str = "colbert", k: int = 3) -> str:
    """Queries whose best relevant paragraph is not in the system's top k (debugging)."""

    out = []
    for query in queries:
        relevant = query["relevant"]
        ranked = results["runs"][system].get(query["id"], [])
        if relevant and not any(relevant.get(pid, 0) > 0 for pid in ranked[:k]):
            out.append(f"{query['id']} [{query['kind']}] {query['query']!r}: top {ranked[:k]} want {sorted(relevant)}")
    return "\n".join(out)


# --- Entry point -------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="part", required=True)
    beir = sub.add_parser("beir", help="BEIR sanity check")
    beir.add_argument("--data", required=True)
    beir.add_argument("--datasets", default="scifact,nfcorpus")
    beir.add_argument("--cache")
    beir.add_argument("--max-queries", type=int, default=0)
    fortress = sub.add_parser("fortress", help="the labelled personal-notes set")
    fortress.add_argument("--misses", action="store_true", help="list ColBERT's misses")
    fortress.add_argument("--json", help="write the raw numbers here")
    for part in (beir, fortress):
        part.add_argument("--no-dense", action="store_true")
        part.add_argument("--threads", type=int, default=os.cpu_count() or 4)
        part.add_argument("--report")
    args = parser.parse_args()
    if args.part == "beir":
        text = run_beir(args)
    else:
        notes, queries = load_fixture()
        text, results = run_fortress(args)
        if args.misses:
            for system in results["runs"]:
                _say(f"--- {system} misses (no relevant paragraph in the top 3)")
                _say(misses(results, queries, system))
            _say("--- unified search: shown for no-answer queries")
            for query in queries:
                if not query["relevant"]:
                    hits = results["unified_hits"][query["id"]]
                    _say(f"{query['query']!r}: {[(h['matched'], h['text'][:50]) for h in hits]}")
            for label, m in results["related"].items():
                _say(f"--- related {label}, wrong: {m['wrong']}")
        if args.json:
            Path(args.json).write_text(json.dumps(
                {k: v for k, v in results.items() if k not in ("unified_hits",)}, indent=1, default=str))
    print(text)
    if args.report:
        Path(args.report).write_text(text)


if __name__ == "__main__":
    main()
