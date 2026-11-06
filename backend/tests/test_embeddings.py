import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import embeddings
from app.block_store import bootstrap_markdown


class _FakeModel:
    def __init__(self) -> None:
        self.documents: list[str] = []

    def encode(self, texts, *, is_query, **_kwargs):
        if not is_query:
            self.documents.extend(texts)
        return [[[[1.0]]] for _ in texts]


class _WordModel:
    """Each word is a fixed random unit vector; a text is its words' vectors."""

    def __init__(self) -> None:
        self.documents: list[str] = []

    def encode(self, texts, *, is_query, **_kwargs):
        import numpy as np

        if not is_query:
            self.documents.extend(texts)
        out = []
        for text in texts:
            vectors = []
            for word in text.lower().split():
                seed = int.from_bytes(word.encode()[:8].ljust(8, b"\0"), "little") % 2**32
                vector = np.random.default_rng(seed).standard_normal(16).astype(np.float32)
                vectors.append(vector / np.linalg.norm(vector))
            out.append(np.stack(vectors) if vectors else np.zeros((0, 16), np.float32))
        return out


def _docs(database):
    from app import block_store

    return [(b, v, t) for b, _d, v, t in block_store.embedding_documents(database)]


def _edit_block(database, block_id: str, text: str) -> None:
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE blocks SET text = ?, updated_at = '2025-02-02T00:00:00+00:00' WHERE id = ?",
            (text, block_id),
        )
        connection.execute("UPDATE documents SET revision = revision + 1")


def _trash(database, document_id: str) -> None:
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE documents SET deleted_at = '2025-03-03T00:00:00+00:00' WHERE id = ?",
            (document_id,),
        )


class BlockEmbeddingTests(unittest.TestCase):
    def test_warm_index_encodes_canonical_blocks_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            (notes / "one.md").write_text(
                "---\ntitle: One\n---\nFirst block\n\nSecond block\n", encoding="utf-8"
            )
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(notes, database)
            model = _FakeModel()

            with patch.object(embeddings, "_get_model", return_value=model), patch.object(
                embeddings,
                "get_settings",
                return_value=type("Settings", (), {"block_db_enabled": True, "block_db_path": database})(),
            ):
                self.assertEqual(embeddings.warm_index(), 2)
                self.assertEqual(embeddings.warm_index(), 0)

            self.assertEqual(model.documents, ["First block", "Second block"])
            self.assertEqual(len(embeddings._cache.index), 2)

    def test_restart_reuses_saved_embeddings_and_reencodes_only_edits(self) -> None:
        from app import block_store

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            block_store.create_document(
                database, "doc", "Doc", "rough", [], "Alpha beta\n\nGamma delta", "2025-01-01", "2025-01-01"
            )
            settings = type("Settings", (), {"block_db_path": database})()
            model = _WordModel()
            with patch.object(embeddings, "_get_model", return_value=model), patch.object(
                embeddings, "get_settings", return_value=settings
            ), patch.object(embeddings, "_cache", embeddings._Cache()):
                self.assertEqual(embeddings.warm_index(), 2)
                before = embeddings.score_documents("alpha", _docs(database))
            self.assertTrue((Path(directory) / ".fortress-embeddings.sqlite3").exists())

            # A restart: a fresh in-memory cache loads the saved vectors.
            with patch.object(embeddings, "_get_model", return_value=model), patch.object(
                embeddings, "get_settings", return_value=settings
            ), patch.object(embeddings, "_cache", embeddings._Cache()):
                self.assertEqual(embeddings.warm_index(), 0)
                self.assertEqual(embeddings.score_documents("alpha", _docs(database)), before)
                tree = block_store.document_tree(database, "doc")
                first = tree["children"][0]
                _edit_block(database, first["id"], "Alpha epsilon")
                self.assertEqual(embeddings.warm_index(), 1)
                # Trashing a note drops its paragraphs from the index.
                _trash(database, "doc")
                embeddings.warm_index()
                self.assertEqual(len(embeddings._cache.index), 0)
            self.assertEqual(model.documents, ["Alpha beta", "Gamma delta", "Alpha epsilon"])

    def test_paragraph_queries_use_precomputed_encodings(self) -> None:
        from app import block_store

        class CountingModel(_WordModel):
            queries = 0

            def encode(self, texts, *, is_query, **kwargs):
                if is_query:
                    CountingModel.queries += len(texts)
                return super().encode(texts, is_query=is_query, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            block_store.create_document(
                database, "doc", "Doc", "rough", [], "Alpha beta\n\nGamma delta", "2025-01-01", "2025-01-01"
            )
            settings = type("Settings", (), {"block_db_path": database})()
            model = CountingModel()
            with patch.object(embeddings, "_get_model", return_value=model), patch.object(
                embeddings, "get_settings", return_value=settings
            ), patch.object(embeddings, "_cache", embeddings._Cache()):
                embeddings.warm_index()  # paragraphs
                fresh = embeddings.score_documents("alpha beta", _docs(database))
                embeddings._cache.queries.clear()
                embeddings.warm_index()  # then their query encodings
                before = CountingModel.queries
                saved = embeddings.score_documents("Alpha beta", _docs(database))
                self.assertEqual(CountingModel.queries, before)  # no model pass
            self.assertEqual(saved, fresh)

    def test_vectorised_maxsim_matches_brute_force(self) -> None:
        import numpy as np

        rng = np.random.default_rng(1)
        model = _WordModel()
        words = [f"w{i}" for i in range(60)]
        docs = [
            (f"b{i}", "v1", " ".join(rng.choice(words, size=int(rng.integers(1, 120)))))
            for i in range(300)
        ]
        settings = type("Settings", (), {"block_db_path": None})()

        def brute(query: str) -> dict[str, float]:
            q = np.asarray(model.encode([query], is_query=True)[0])
            out = {}
            for block_id, _version, text in docs:
                d = np.asarray(model.encode([text], is_query=False)[0])
                out[block_id] = float((q @ d.T).max(axis=1).sum())
            return out

        with patch.object(embeddings, "_get_model", return_value=model), patch.object(
            embeddings, "get_settings", return_value=settings
        ), patch.object(embeddings, "_cache", embeddings._Cache()), patch.object(
            embeddings, "INLINE_ENCODE_LIMIT", 1000
        ):
            for query in ("w1 w2 w3", " ".join(words[:50]), " ".join(words * 3)):
                got = embeddings.score_documents(query, docs)
                want = brute(query)
                top = sorted(want, key=want.get, reverse=True)[:20]
                self.assertEqual(sorted(got, key=got.get, reverse=True)[:20], top)
                for block_id in top:
                    self.assertAlmostEqual(got[block_id], want[block_id], places=3)
                if len(query.split()) <= embeddings.LONG_QUERY_TOKENS:
                    for block_id, score in want.items():
                        self.assertAlmostEqual(got[block_id], score, places=3)
            # Edited paragraphs replace their old vectors; normalised scores are per token.
            docs[0] = ("b0", "v2", "w1 w2 w3")
            got = embeddings.score_documents("w1 w2 w3", docs, normalized=True)
            self.assertAlmostEqual(got["b0"], 1.0, places=3)
            self.assertEqual(embeddings.query_token_count("w1 w2 w3"), 3)

    def test_a_search_never_waits_for_a_whole_cold_index(self) -> None:
        model = _WordModel()
        docs = [(f"b{i}", "v1", f"w{i} w{i + 1}") for i in range(100)]
        settings = type("Settings", (), {"block_db_path": None})()
        with patch.object(embeddings, "_get_model", return_value=model), patch.object(
            embeddings, "get_settings", return_value=settings
        ), patch.object(embeddings, "_cache", embeddings._Cache()):
            scores = embeddings.score_documents("w5", docs)
        # Nothing indexed yet: the background indexer's job, scored 0 for now.
        self.assertEqual(model.documents, [])
        self.assertEqual(set(scores.values()), {0.0})
        self.assertEqual(len(scores), 100)

    def test_concurrent_first_use_loads_the_model_once(self) -> None:
        # The background reindexer and a search can both ask for the model at startup.
        import sys
        import threading
        import time
        import types

        loads = []

        class SlowColBERT:
            def __init__(self, **_kwargs) -> None:
                loads.append(1)
                time.sleep(0.2)

        fake_pylate = types.ModuleType("pylate")
        fake_pylate.models = types.SimpleNamespace(ColBERT=SlowColBERT)
        settings = type("Settings", (), {"embeddings_enabled": True, "colbert_model": "fake"})()
        with patch.dict(sys.modules, {"pylate": fake_pylate}), patch.object(
            embeddings, "_cache", embeddings._Cache()
        ), patch.object(embeddings, "get_settings", return_value=settings):
            results = []
            threads = [
                threading.Thread(target=lambda: results.append(embeddings._get_model()))
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(loads), 1)
        self.assertEqual(len({id(model) for model in results}), 1)

    def test_keyword_fallback_understands_words_beyond_ascii(self) -> None:
        with patch.object(embeddings, "_get_model", return_value=None):
            scores = embeddings.score_documents(
                "Μύκονος hotel",
                [("greek", "v1", "Μύκονος in June"), ("email", "v1", "Send the e-mail")],
            )
            self.assertEqual(scores["greek"], 0.5)
            # "brûlée" is one word, not the letters "br", "l" and "e".
            scores = embeddings.score_documents(
                "brûlée", [("dessert", "v1", "crème brulee"), ("email", "v1", "Send the e-mail")]
            )
        self.assertEqual(scores, {"dessert": 1.0, "email": 0.0})

    def test_keyword_fallback_reads_a_long_query_once_not_once_per_paragraph(self) -> None:
        import time

        query = " ".join(f"word{i}" for i in range(2000))
        docs = [(f"b{i}", "v1", f"paragraph {i} mentions word{i}") for i in range(1500)]
        with patch.object(embeddings, "_get_model", return_value=None):
            started = time.monotonic()
            scores = embeddings.score_documents(query, docs)
            elapsed = time.monotonic() - started
        self.assertEqual(scores["b7"], 1 / 2000)
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()


class NoteQueryTests(unittest.TestCase):
    def test_a_whole_note_query_reuses_paragraph_encodings_and_batches_the_rest(self) -> None:
        import torch

        calls: list[list[str]] = []

        def fake_encode(_model, texts, *, is_query):
            assert is_query
            calls.append(list(texts))
            return [torch.ones(len(text.split()), 2) for text in texts]

        embeddings._cache.queries.clear()
        embeddings._cache.queries["Saved paragraph here"] = torch.zeros(3, 2)
        with patch.object(embeddings, "_get_model", return_value=object()), patch.object(
            embeddings, "_encode", side_effect=fake_encode
        ), patch.object(embeddings, "_saved_query", return_value=None):
            joined = embeddings.note_query_embedding(
                ["Kyoto trip", "Saved paragraph here", "", "New one"]
            )
        self.addCleanup(embeddings._cache.queries.clear)
        # One batched model call for the two unseen texts; the saved one is reused.
        self.assertEqual(calls, [["Kyoto trip", "New one"]])
        # Title (2 tokens) + saved (3) + new (2), in note order, blank paragraphs skipped.
        self.assertEqual(tuple(joined.shape), (7, 2))
        self.assertEqual(float(joined[2:5].sum()), 0.0)
