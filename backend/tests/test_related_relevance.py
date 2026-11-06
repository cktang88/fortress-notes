"""Related paragraphs need a real relevance cutoff, not just ``score > 0``.

The ColBERT model can't load in unit tests, so a fake model stands in: every word
is a unit vector, and any two different words still have cosine 0.75 (a shared
component), so — like real ColBERT — raw MaxSim is positive for any pair.
"""

import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import block_query

_DIM = 512


def _word_vector(word: str) -> np.ndarray:
    vector = np.zeros(_DIM + 1)
    vector[zlib.crc32(word.encode()) % _DIM] = 1.0
    vector[_DIM] = 3**0.5  # different words: cosine 3 / 4 = 0.75
    return vector / np.linalg.norm(vector)


class _FakeColBERT:
    def tokenize(self, texts, *, is_query):
        return {"input_ids": [block_query.query_tokens(text) or ["_"] for text in texts]}

    def encode(self, texts, *, is_query, **_kwargs):
        return [
            np.stack([_word_vector(word) for word in block_query.query_tokens(text) or ["_"]])
            for text in texts
        ]


def _settings(notes: Path, embeddings_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        block_db_enabled=True,
        block_db_import_on_startup=False,
        block_db_path=notes / ".fortress.sqlite3",
        notes_path=notes,
        assets_path=notes / "assets",
        embeddings_enabled=embeddings_enabled,
        vision_enabled=False,
        reindex_interval_s=3600.0,
    )


class RelatedRelevanceApiTests(unittest.TestCase):
    def _related(self, model, notes_to_create, source_body):
        from app import documents, embeddings, main, search

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory), model is not None)
            with (
                patch.object(main, "settings", settings),
                patch.object(documents, "get_settings", return_value=settings),
                patch.object(search, "get_settings", return_value=settings),
                patch.object(embeddings, "get_settings", return_value=settings),
                patch.object(embeddings, "_get_model", return_value=model),
                patch.object(embeddings, "_cache", embeddings._Cache()),
                TestClient(main.app) as client,
            ):
                ids = {}
                for title, body in notes_to_create:
                    ids[title] = client.post("/api/notes", json={"title": title, "body": body}).json()["id"]
                source = client.post("/api/notes", json={"title": "Draft", "body": source_body}).json()
                block = client.get(f"/api/block-documents/{source['id']}").json()["children"][0]
                endpoint = f"/api/notes/{source['id']}/related"
                by_block = client.get(endpoint, params={"block_id": block["id"]}).json()
                whole = client.get(endpoint).json()
                names = {value: key for key, value in ids.items()}
                return (
                    [names[r["note"]["id"]] for r in by_block],
                    [names[r["note"]["id"]] for r in whole],
                    by_block,
                )

    def test_model_scores_below_the_cutoff_are_not_related(self) -> None:
        notes = [
            ("Bread", "sourdough starter doubled overnight with rye flour"),
            ("Tiles", "matte grey bathroom tiles quote from the tiler"),
            ("Budget", "monthly groceries budget rent transport savings"),
            ("Sleep", "no screens after ten no coffee after two"),
            ("Cat", "vet vaccines for the cat next visit in a year"),
            ("Router", "move the router off the floor or buy a mesh extender"),
        ]
        by_block, whole, results = self._related(
            _FakeColBERT(), notes, "fed the sourdough starter with rye flour this morning"
        )
        self.assertEqual(by_block, ["Bread"])
        self.assertEqual(whole, ["Bread"])
        # Scores are normalized (about 0..1), not raw MaxSim sums.
        self.assertLessEqual(results[0]["score"], 1.0)

    def test_keyword_fallback_needs_more_than_one_shared_common_word(self) -> None:
        notes = [
            ("Garden", "the tomatoes and basil in the raised bed need water"),
            ("Bedroom", "the bed frame squeaks"),
            ("Stopwords", "this is what they would have been doing"),
        ]
        by_block, _whole, _ = self._related(
            None, notes, "planted tomatoes and basil in the raised bed this weekend"
        )
        self.assertEqual(by_block, ["Garden"])


class RelevanceLayerTests(unittest.TestCase):
    """The cutoff on its own, with made-up raw MaxSim scores."""

    def test_normalizes_against_query_tokens_and_corpus_median(self) -> None:
        from app import relevance

        raw = {f"u{i}": 7.5 for i in range(8)} | {"near": 9.0, "weak": 7.7}
        kept = relevance.relevant_model_scores(raw, query_tokens=10)
        self.assertEqual(set(kept), {"near"})
        self.assertAlmostEqual(kept["near"], (9.0 - 7.5) / (10.0 - 7.5))

    def test_a_low_corpus_median_never_lowers_the_bar_below_the_prior(self) -> None:
        from app import relevance

        # Everything else is wildly off-topic, so the median is far below the
        # typical unrelated level; a merely average match must still not pass.
        raw = {f"u{i}": 3.0 for i in range(8)} | {"average": 7.4}
        self.assertEqual(relevance.relevant_model_scores(raw, query_tokens=10), {})

    def test_small_corpora_use_the_prior_baseline(self) -> None:
        from app import relevance

        kept = relevance.relevant_model_scores({"near": 9.0, "unrelated": 7.4}, query_tokens=10)
        self.assertEqual(set(kept), {"near"})

    def test_no_query_tokens_keeps_nothing(self) -> None:
        from app import relevance

        self.assertEqual(relevance.relevant_model_scores({"a": 1.0}, query_tokens=0), {})

    def test_keyword_scores_need_shared_distinctive_words(self) -> None:
        from app import relevance

        docs = [
            ("both", "v", "Kyoto itinerary and the shinkansen"),
            ("one", "v", "an itinerary for the weekend"),
            ("common", "v", "and the of to with"),
        ]
        kept = relevance.relevant_keyword_scores("Kyoto itinerary with the shinkansen", docs)
        self.assertEqual(set(kept), {"both"})
        self.assertAlmostEqual(kept["both"], 1.0)
        # A query with a single distinctive word only needs that word.
        self.assertEqual(set(relevance.relevant_keyword_scores("the Kyoto", docs)), {"both"})


if __name__ == "__main__":
    unittest.main()
