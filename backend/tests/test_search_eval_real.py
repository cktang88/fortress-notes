"""Search quality floors on the labelled notes set, with the real ColBERT model.

The set (tests/fixtures/search_eval) and the harness (scripts/eval_search.py) are the
ones docs/search-eval.md reports from. This runs the app's own paths on the device
``EMBEDDINGS_DEVICE=auto`` picks (the MLX GPU encoder in the macOS CI job): ColBERT
scores, unified search with its cutoffs, related notes and paragraphs. The floors sit
a little below what was measured on the CPU, so a regression (a broken encoder,
prefix, cutoff or merge) fails while float16 noise does not.

Needs the model in the Hugging Face cache: runs only with ``FORTRESS_REAL_MODEL=1``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ENABLED = os.environ.get("FORTRESS_REAL_MODEL") == "1"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_search.py"

# Measured on the CPU (docs/search-eval.md): ColBERT nDCG@10 94.4, unified 83.1,
# unified precision of what it shows 70%, the answer shown for 88% of queries,
# related notes 89% / paragraphs 68% precise.
FLOORS = {
    "colbert nDCG@10": 90.0,
    "colbert R@10": 93.0,
    "unified nDCG@10": 80.0,
    "unified precision": 60.0,
    "unified answer shown": 80.0,
    "related note precision": 80.0,
    "related paragraph precision": 58.0,
}
# Queries whose answer must be the first result of unified search, one per hard case.
MUST_BE_FIRST = {
    "plane tickets to Asia": "japan-itin.3",  # paraphrase, no shared word
    "INV-2231": "invoices.0",  # identifier with a near-duplicate (INV-2213)
    "ORA-00942": "oracle.0",
    "what temperature and how long do I bake the sourdough in the dutch oven": "sourdough-log.2",
    "invoice that was already paid": "invoices.1",  # negation-ish
    "Wohnung Kreuzberg": "berlin.0",  # German
    "chiken curry": {"thai-curry.0", "curry-chickpea.0"},  # typo
    "fever medicine dose for a toddler": "kids-meds.0",
}
# Must be in unified search's top 3: the paragraph says it without the query's words,
# only the note title does (the model reads the title with each paragraph).
MUST_BE_IN_TOP_3 = {
    "sourdough": {"sourdough-log.0", "sourdough-log.1", "sourdough-log.2",
                  "sourdough-fix.0", "sourdough-fix.1", "sourdough-fix.2"},
    "crepe recipe": {"crepes.0", "crepes.1"},  # French note, English query
}


def _say(message: str) -> None:
    print(f"REALMODEL eval {message}", file=sys.stderr, flush=True)


def _harness():
    spec = importlib.util.spec_from_file_location("eval_search", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(ENABLED, "set FORTRESS_REAL_MODEL=1 (needs the ColBERT model downloaded)")
class SearchEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app import config, embeddings, relevance, search

        E = cls.E = _harness()
        notes, queries = cls.notes, cls.queries = E.load_fixture()
        cls.qrels = {query["id"]: query["relevant"] for query in queries}
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        previous = config._active
        cls.addClassCleanup(config.activate, previous)
        cache = patch.object(embeddings, "_cache", embeddings._Cache())
        cache.start()
        cls.addClassCleanup(cache.stop)
        cls.addClassCleanup(search._related_cache.clear)
        search._related_cache.clear()
        started = time.perf_counter()
        ws = cls.ws = E.Workspace(notes, Path(directory.name))
        index_s = ws.index()
        if embeddings.device_info() is None:
            raise AssertionError("the ColBERT model could not be loaded")

        def colbert(query: str) -> list[str]:
            raw = embeddings.score_documents(query, ws.docs)
            return ws.paras(sorted(raw, key=lambda block: -raw[block]))

        cls.colbert = {q["id"]: colbert(q["query"]) for q in queries}
        query_started = time.perf_counter()
        cls.unified = {
            q["id"]: ws.paras(str(hit["block_id"]) for hit in search.unified_search(q["query"]))
            for q in queries
        }
        search_ms = 1000 * (time.perf_counter() - query_started) / len(queries)
        cls.meaning_kept = {
            q["id"]: relevance.relevant_model_scores(
                embeddings.score_documents(q["query"], ws.docs),
                relevance.query_token_count(q["query"]),
                relevance.SEARCH_MIN_SCORE,
                relevance.SEARCH_RELATIVE_SCORE,
            )
            for q in queries
        }
        cls.related = E.related_quality(ws, notes)
        cls.metrics = {
            "colbert": E.summarize(cls.colbert, cls.qrels),
            "unified": E.summarize(cls.unified, cls.qrels),
            "shown": E.shown_metrics(cls.unified, queries),
        }
        answerable = [q for q in queries if q["relevant"]]
        cls.answer_shown = 100 * sum(
            any(q["relevant"].get(pid, 0) == 2 for pid in cls.unified[q["id"]]) for q in answerable
        ) / len(answerable)
        _say(
            f"device={embeddings.device_info()} index={index_s:.1f}s "
            f"total={time.perf_counter() - started:.0f}s unified_ms={search_ms:.0f} "
            f"colbert_ndcg10={cls.metrics['colbert']['nDCG@10']:.1f} "
            f"colbert_r10={cls.metrics['colbert']['R@10']:.1f} "
            f"unified_ndcg10={cls.metrics['unified']['nDCG@10']:.1f} "
            f"unified_precision={cls.metrics['shown']['precision']:.1f} "
            f"answer_shown={cls.answer_shown:.1f} "
            f"related_note_p={cls.related['note']['precision']:.1f} "
            f"related_paragraph_p={cls.related['paragraph']['precision']:.1f}"
        )

    def test_colbert_ranking_quality(self) -> None:
        self.assertGreaterEqual(self.metrics["colbert"]["nDCG@10"], FLOORS["colbert nDCG@10"])
        self.assertGreaterEqual(self.metrics["colbert"]["R@10"], FLOORS["colbert R@10"])

    def test_unified_search_quality(self) -> None:
        self.assertGreaterEqual(self.metrics["unified"]["nDCG@10"], FLOORS["unified nDCG@10"])
        self.assertGreaterEqual(self.metrics["shown"]["precision"], FLOORS["unified precision"])
        self.assertGreaterEqual(self.answer_shown, FLOORS["unified answer shown"])

    def test_no_answer_queries_show_nothing(self) -> None:
        for query in self.queries:
            if not query["relevant"]:
                self.assertEqual(self.meaning_kept[query["id"]], {}, query["query"])
                self.assertEqual(self.unified[query["id"]], [], query["query"])

    def test_hard_cases(self) -> None:
        by_text = {query["query"]: query["id"] for query in self.queries}
        for text, para in MUST_BE_FIRST.items():
            got = self.unified[by_text[text]]
            wanted = para if isinstance(para, set) else {para}
            self.assertTrue(got and got[0] in wanted, f"{text!r}: {got[:3]}, want {para}")
        for text, paras in MUST_BE_IN_TOP_3.items():
            got = self.unified[by_text[text]]
            self.assertTrue(paras & set(got[:3]), f"{text!r}: {got[:3]}")

    def test_related_notes_and_paragraphs(self) -> None:
        note, paragraph = self.related["note"], self.related["paragraph"]
        self.assertGreaterEqual(note["precision"], FLOORS["related note precision"], note["wrong"])
        self.assertGreaterEqual(
            paragraph["precision"], FLOORS["related paragraph precision"], paragraph["wrong"]
        )
        # Notes with nothing related to them show (almost) nothing.
        shown, lonely = map(int, note["lonely notes showing something"].split("/"))
        self.assertLessEqual(shown, 1)
        self.assertGreater(lonely, 0)


if __name__ == "__main__":
    unittest.main()
