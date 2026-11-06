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
            self.assertEqual(len(embeddings._cache.docs), 2)

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


if __name__ == "__main__":
    unittest.main()
