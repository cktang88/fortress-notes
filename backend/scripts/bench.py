"""Benchmark semantic related/search and Ask on a large generated workspace.

Usage (from ``backend/``, model loaded offline from the Hugging Face cache)::

    HF_HUB_OFFLINE=1 uv run --frozen python scripts/bench.py --dir /tmp/ws --notes 2000
    HF_HUB_OFFLINE=1 uv run --frozen python scripts/bench.py --dir /tmp/ws --ask 2

The first run generates the workspace. Each run measures, in a fresh process:
index time (cold the first time; a restart afterwards), warm related-paragraph,
related-note and unified-search latency, Ask's local prefetch, and memory. With
``--ask N`` it also runs every benchmark question N times end to end against the
real model (needs ``OPENROUTER_API_KEY`` in the environment) and reports latency,
time to first answer text, and whether the expected fact was in the answer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "n": len(ordered),
        "p50_ms": round(1000 * statistics.median(ordered), 1),
        "p95_ms": round(1000 * p95, 1),
        "max_ms": round(1000 * ordered[-1], 1),
    }


def _timed(fn, *args, **kwargs) -> float:
    started = time.perf_counter()
    fn(*args, **kwargs)
    return time.perf_counter() - started


UNIFIED_QUERIES = [
    "train to the castle early morning",
    "memory leak cache eviction",
    "how long to roast vegetables",
    "deadline for the migration",
    "half marathon training",
    "best cheese at the market",
    "cancel unused subscriptions",
    "knee injury physio advice",
    "book about habits compounding",
    "fix the dripping kitchen tap",
    "slow database query index",
    "where to stay near the station",
    "hiring engineers before launch",
    "sleep earlier more energy",
    "electricity bill in winter",
    "sauce tastes flat",
    "flaky test ordering",
    "tomatoes after frost",
    "museum tickets online",
    "code reviews take too long",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--notes", type=int, default=2000)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--ask", type=int, default=0, help="end-to-end Ask rounds (real model)")
    parser.add_argument("--ask-model", default=None)
    parser.add_argument("--questions", type=int, default=None, help="only the first N questions")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument(
        "--baseline-prefill",
        action="store_true",
        help="old in-memory cache: time warm_index on 100 paragraphs, prefill the rest fast",
    )
    parser.add_argument("--out", type=Path, default=None, help="append JSON results here")
    parser.add_argument(
        "--key-from", type=Path, default=None, help="read OPENROUTER_API_KEY from this .env"
    )
    args = parser.parse_args()
    if args.key_from:
        # Only the key, only into this process's environment; never printed.
        for line in args.key_from.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                os.environ["OPENROUTER_API_KEY"] = value.strip().strip("'\"")

    os.environ["NOTES_DIR"] = str(args.dir)
    os.environ.setdefault("VISION_ENABLED", "false")
    os.environ.setdefault("VLM_CAPTION_ENABLED", "false")
    if args.ask_model:
        os.environ["ASK_MODEL"] = args.ask_model
    results: dict[str, object] = {"label": os.environ.get("BENCH_LABEL", "")}

    import bench_workspace

    database = args.dir / ".fortress.sqlite3"
    if not database.exists():
        started = time.perf_counter()
        results["workspace"] = bench_workspace.build(args.dir, args.notes)
        results["generate_s"] = round(time.perf_counter() - started, 1)

    from app import ask, block_store, embeddings, search
    from app.config import get_settings

    settings = get_settings()
    corpus = block_store.embedding_documents(settings.block_db_path)
    results["paragraphs"] = len(corpus)
    results["rss_before_model_mb"] = round(_rss_mb())

    started = time.perf_counter()
    assert embeddings.available(), "ColBERT model unavailable (set HF_HUB_OFFLINE=1?)"
    results["model_load_s"] = round(time.perf_counter() - started, 1)

    if args.baseline_prefill:
        _baseline_prefill(results, corpus)

    # Cold start the first time; a restart (embeddings reused from disk if the code
    # persists them) on later runs.
    started = time.perf_counter()
    try:
        encoded = embeddings.warm_index(queries=False)
    except TypeError:  # the pre-optimisation code
        encoded = embeddings.warm_index()
    results["index_s"] = round(time.perf_counter() - started, 1)
    results["index_encoded"] = encoded
    if hasattr(embeddings, "_warm_queries"):
        # Precomputed query encodings for every paragraph (background, after indexing).
        started = time.perf_counter()
        embeddings.warm_index()
        results["query_index_s"] = round(time.perf_counter() - started, 1)
    results["rss_indexed_mb"] = round(_rss_mb())
    print(json.dumps(results), flush=True)

    rng = random.Random(3)
    blocks = [row for row in corpus if len(row[3]) > 40]
    picks = rng.sample(blocks, args.samples)

    if not args.skip_search:
        # Warm-up so first-call costs (thread pools, caches) aren't counted.
        search.related_notes(picks[0][1], 5, picks[0][0])
        search.unified_search(UNIFIED_QUERIES[0])

        results["related_paragraph"] = _stats(
            [_timed(search.related_notes, document_id, 5, block_id) for block_id, document_id, _, _ in picks]
        )
        # The same paragraphs again: what a re-render or a refetch costs.
        results["related_paragraph_repeat"] = _stats(
            [_timed(search.related_notes, document_id, 5, block_id) for block_id, document_id, _, _ in picks]
        )
        note_ids = sorted({row[1] for row in picks})[:10]
        results["related_note"] = _stats([_timed(search.related_notes, note_id, 5) for note_id in note_ids])
        results["unified_search"] = _stats([_timed(search.unified_search, q) for q in UNIFIED_QUERIES])
        results["ask_prefetch"] = _stats(
            [
                _timed(ask.prefetch, question, ask._Sources())
                for question, _expected in bench_workspace.QUESTIONS
            ]
        )
        results["rss_after_queries_mb"] = round(_rss_mb())
        print(json.dumps(results), flush=True)

    if args.ask:
        results["ask_model"] = get_settings().ask_model
        results["ask"] = asyncio.run(_ask_rounds(args.ask, args.questions))
        print(json.dumps(results["ask"]), flush=True)

    if args.out:
        with args.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(results) + "\n")
    print(json.dumps(results, indent=1))


def _baseline_prefill(results: dict, corpus: list) -> None:
    """The pre-optimisation code encodes one paragraph per call, which would take
    hours here: time it on a sample, then fill its cache with a batched encoder."""

    import copy

    import numpy as np
    import torch
    from app import block_store, embeddings

    sample = corpus[:100]
    original = block_store.embedding_documents
    block_store.embedding_documents = lambda _path: sample
    started = time.perf_counter()
    embeddings.warm_index()
    elapsed = time.perf_counter() - started
    block_store.embedding_documents = original
    results["baseline_index_100_s"] = round(elapsed, 1)
    results["baseline_index_extrapolated_s"] = round(elapsed / 100 * len(corpus))
    fast = copy.deepcopy(embeddings._get_model()).to(torch.bfloat16)
    rest = [row for row in corpus if row[0] not in embeddings._cache.docs]
    with torch.inference_mode():
        for start in range(0, len(rest), 256):
            chunk = rest[start : start + 256]
            out = fast.encode(
                [row[3] for row in chunk], is_query=False, batch_size=16, show_progress_bar=False
            )
            for row, emb in zip(chunk, out):
                embeddings._cache.docs[row[0]] = (row[2], np.asarray(emb, dtype=np.float32))
    del fast
    print(json.dumps(results), flush=True)


async def _ask_rounds(rounds: int, limit: int | None = None) -> dict[str, object]:
    import bench_workspace
    from app import ask

    stream = getattr(ask, "ask_stream", None)
    totals, firsts, statuses, rows = [], [], {}, []
    correct = answerable = 0
    for _round in range(rounds):
        for question, expected in bench_workspace.QUESTIONS[:limit]:
            started = time.perf_counter()
            first = None
            if stream is not None:
                result = None
                async for event in stream(question):
                    if event.get("type") == "sentence" and first is None:
                        first = time.perf_counter() - started
                    if event.get("type") == "done":
                        result = event["result"]
            else:
                result = await ask.ask(question)
            total = time.perf_counter() - started
            if first is None and result["status"] == "answered":
                first = total
            totals.append(total)
            if first is not None:
                firsts.append(first)
            statuses[result["status"]] = statuses.get(result["status"], 0) + 1
            text = " ".join(sentence["text"] for sentence in result["answer"])
            if expected:
                answerable += 1
                correct += expected.lower() in text.lower()
            rows.append(
                {
                    "q": question,
                    "status": result["status"],
                    "ms": round(total * 1000),
                    "answer": text[:160],
                    "steps": len(result.get("steps", [])),
                }
            )
    for row in rows:
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return {
        "end_to_end": _stats(totals),
        "first_text": _stats(firsts) if firsts else None,
        "statuses": statuses,
        "correct": f"{correct}/{answerable}",
    }


if __name__ == "__main__":
    main()
