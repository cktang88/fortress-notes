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
    from app import corpus

    return [(b, v, t) for b, _d, v, t in corpus.embedding_documents(database)]


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
                threading.Thread(target=lambda: results.append(embeddings.load_model()))
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

        embeddings._cache.paragraph_queries.clear()
        embeddings._cache.paragraph_queries["Saved paragraph here"] = torch.zeros(3, 2)
        with patch.object(embeddings, "_get_model", return_value=object()), patch.object(
            embeddings, "_encode", side_effect=fake_encode
        ), patch.object(embeddings, "_saved_query", return_value=None):
            joined = embeddings.note_query_embedding(
                ["Kyoto trip", "Saved paragraph here", "", "New one"]
            )
        self.addCleanup(embeddings._cache.paragraph_queries.clear)
        # One batched model call for the two unseen texts; the saved one is reused.
        self.assertEqual(calls, [["Kyoto trip", "New one"]])
        # Title (2 tokens) + saved (3) + new (2), in note order, blank paragraphs skipped.
        self.assertEqual(tuple(joined.shape), (7, 2))
        self.assertEqual(float(joined[2:5].sum()), 0.0)


class _RacyModel:
    """Mimics PyLate: every call sets a shared max length, then reads it back later.

    Two threads encoding at once would cut documents to the query length.
    """

    def __init__(self) -> None:
        self.max_seq_length = 0

    def _ids(self, texts, is_query):
        import time

        self.max_seq_length = 4 if is_query else 64
        time.sleep(0.002)  # tokenizing: plenty of time for another thread to run
        return [text.split()[: self.max_seq_length] for text in texts]

    def tokenize(self, texts, *, is_query):
        return {"input_ids": self._ids(texts, is_query)}

    def encode(self, texts, *, is_query, **_kwargs):
        import numpy as np

        out = []
        for words in self._ids(texts, is_query):
            vectors = []
            for word in words:
                seed = int.from_bytes(word.encode()[:8].ljust(8, b"\0"), "little") % 2**32
                vector = np.random.default_rng(seed).standard_normal(8).astype(np.float32)
                vectors.append(vector / np.linalg.norm(vector))
            out.append(np.stack(vectors))
        return out


class EncoderTests(unittest.TestCase):
    def test_concurrent_query_and_document_encodes_match_sequential_ones(self) -> None:
        import threading

        import torch

        model = _RacyModel()
        documents = [" ".join(f"d{i}w{j}" for j in range(40)) for i in range(48)]
        queries = [f"q{i} a b c d e f" for i in range(30)]
        settings = type("Settings", (), {"embeddings_enabled": True})()
        with patch.object(embeddings, "get_settings", return_value=settings), patch.object(
            embeddings, "_cache", embeddings._Cache()
        ):
            want_docs = embeddings._encode(model, documents, is_query=False)
            want_queries = [embeddings._encode(model, [q], is_query=True)[0] for q in queries]
            want_lengths = embeddings._encoder.run(
                lambda: [len(ids) for ids in model.tokenize(documents, is_query=False)["input_ids"]], 0
            )
            got_docs, got_queries, got_lengths = [], [], []

            def index() -> None:
                for _ in range(3):
                    got_docs.append(embeddings._encode(model, documents, is_query=False))

            def ask() -> None:
                for query in queries:
                    got_queries.append(embeddings._encode(model, [query], is_query=True)[0])

            with patch.object(embeddings, "_get_model", return_value=model):

                def count() -> None:
                    for _ in range(10):
                        got_lengths.append(embeddings.tokenized_lengths(documents, is_query=False))

                threads = [threading.Thread(target=f) for f in (index, ask, ask, count)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(len(got_docs), 3)
        for run in got_docs:
            for got, want in zip(run, want_docs):
                self.assertEqual(tuple(got.shape), (40, 8))
                self.assertTrue(torch.equal(got, want))
        self.assertEqual(len(got_queries), 2 * len(queries))
        for position, got in enumerate(got_queries):
            # Two askers interleave; each query's encoding equals its sequential one.
            self.assertTrue(any(torch.equal(got, want) for want in want_queries), position)
        self.assertEqual(got_lengths, [want_lengths] * 10)

    def test_searches_run_ahead_of_queued_background_work(self) -> None:
        import threading

        encoder = embeddings._Encoder()
        release = threading.Event()
        order: list[str] = []
        started = threading.Event()

        def blocker() -> None:
            started.set()
            release.wait(5)

        threads = [threading.Thread(target=encoder.run, args=(blocker, embeddings._BACKGROUND))]
        threads[0].start()
        started.wait(5)
        for name, priority in (("index 1", 1), ("index 2", 1), ("search", 0)):
            thread = threading.Thread(
                target=encoder.run, args=(lambda name=name: order.append(name), priority)
            )
            thread.start()
            threads.append(thread)
            while encoder._queue.qsize() < len(threads) - 1:
                pass
        release.set()
        for thread in threads:
            thread.join()
        self.assertEqual(order, ["search", "index 1", "index 2"])

    def test_errors_reach_the_caller(self) -> None:
        def fail():
            raise ValueError("bad batch")

        with self.assertRaises(ValueError):
            embeddings._Encoder().run(fail, 0)


class ModelLoadingTests(unittest.TestCase):
    def test_requests_fall_back_to_keywords_while_the_model_loads(self) -> None:
        import sys
        import threading
        import types

        loading = threading.Event()
        release = threading.Event()

        class SlowColBERT:
            def __init__(self, **_kwargs) -> None:
                loading.set()
                release.wait(5)

            def encode(self, texts, **_kwargs):
                return [[[1.0]] for _ in texts]

        fake_pylate = types.ModuleType("pylate")
        fake_pylate.models = types.SimpleNamespace(ColBERT=SlowColBERT)
        settings = type(
            "Settings", (), {"embeddings_enabled": True, "colbert_model": "fake", "block_db_path": None}
        )()
        docs = [("a", "v1", "kyoto temples"), ("b", "v1", "bread starter")]
        with patch.dict(sys.modules, {"pylate": fake_pylate}), patch.object(
            embeddings, "_cache", embeddings._Cache()
        ), patch.object(embeddings, "get_settings", return_value=settings):
            # Nothing loads the model on a request's behalf.
            self.assertIsNone(embeddings.model_if_ready())
            self.assertFalse(loading.is_set())
            loader = threading.Thread(target=embeddings.load_model)
            loader.start()
            self.assertTrue(loading.wait(5))
            # While the background loader holds the model lock, requests still answer.
            self.assertFalse(embeddings.available())
            self.assertEqual(
                embeddings.score_documents("kyoto", docs), {"a": 1.0, "b": 0.0}
            )
            self.assertIsNone(embeddings.scoring_state([]))
            self.assertIsNone(embeddings.tokenized_lengths(["kyoto"]))
            release.set()
            loader.join(5)
            self.assertTrue(embeddings.available())


class NoteQueryClusteringTests(unittest.TestCase):
    def test_long_notes_are_bounded_and_keep_their_token_weight(self) -> None:
        import torch

        torch.manual_seed(0)
        vectors = torch.nn.functional.normalize(torch.randn(3000, 16), dim=-1)
        centroids, weights = embeddings._cluster(vectors, 128)
        self.assertLessEqual(centroids.shape[0], 128)
        self.assertEqual(float(weights.sum()), 3000.0)
        short, none = embeddings._cluster(vectors[:100], 128)
        self.assertIs(none, None)
        self.assertTrue(torch.equal(short, vectors[:100]))

    def test_weighted_maxsim_is_close_to_the_full_note_and_never_above_it(self) -> None:
        import torch

        torch.manual_seed(1)
        index = embeddings._Index()
        for i in range(200):
            index.put(f"b{i}", "h", "v", torch.nn.functional.normalize(torch.randn(20, 16), dim=-1))
        # A long note: 40 paragraphs drawn around a few topics.
        topics = torch.nn.functional.normalize(torch.randn(6, 16), dim=-1)
        note = torch.nn.functional.normalize(
            topics[torch.randint(0, 6, (2400,))] + 0.3 * torch.randn(2400, 16), dim=-1
        )
        ids = [f"b{i}" for i in range(200)]
        exact = index.maxsim_some(note, ids)
        centroids, weights = embeddings._cluster(note, 64)
        approx = index.maxsim_some(centroids, ids, weights)
        for a, e in zip(approx, exact):
            self.assertLessEqual(a, e + 1e-3)
        ranked = lambda scores: sorted(range(200), key=lambda i: -scores[i])[:10]  # noqa: E731
        self.assertGreaterEqual(len(set(ranked(exact)) & set(ranked(approx))), 8)

    def test_chunked_maxsim_matches_one_pass(self) -> None:
        import torch

        torch.manual_seed(2)
        index = embeddings._Index()
        for i in range(300):
            length = int(torch.randint(3, 60, (1,)))
            index.put(f"b{i}", "h", "v", torch.nn.functional.normalize(torch.randn(length, 16), dim=-1))
        query = torch.nn.functional.normalize(torch.randn(50, 16), dim=-1)
        weights = torch.rand(50)
        ids = [f"b{i}" for i in range(300)]
        whole_ids, whole = index.maxsim_all(query, weights)
        some = index.maxsim_some(query, ids)
        with patch.object(embeddings, "SIM_CHUNK_ELEMENTS", 1000):
            chunked_ids, chunked = index.maxsim_all(query, weights)
            self.assertEqual(chunked_ids, whole_ids)
            self.assertTrue(torch.allclose(chunked, whole, atol=1e-4))
            for a, b in zip(index.maxsim_some(query, ids), some):
                self.assertAlmostEqual(a, b, places=4)

    def test_a_long_note_is_queried_with_bounded_vectors(self) -> None:
        import torch

        def fake_encode(_model, texts, *, is_query):
            return [torch.nn.functional.normalize(torch.randn(32, 8), dim=-1) for _ in texts]

        texts = [f"paragraph {i}" for i in range(300)]
        with patch.object(embeddings, "_get_model", return_value=object()), patch.object(
            embeddings, "_encode", side_effect=fake_encode
        ), patch.object(embeddings, "_saved_query", return_value=None), patch.object(
            embeddings, "_cache", embeddings._Cache()
        ):
            vectors, weights, tokens = embeddings.note_query(texts)
            self.assertLessEqual(vectors.shape[0], embeddings.NOTE_QUERY_VECTORS)
            self.assertEqual(tokens, 300 * 32)
            self.assertEqual(float(weights.sum()), tokens)
            # Cached per note text; typed queries live in their own cache.
            self.assertIs(embeddings.note_query(texts)[0], vectors)
            self.assertEqual(len(embeddings._cache.queries), 0)
