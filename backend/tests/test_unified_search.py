import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class UnifiedSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, embeddings, main, search

        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        self.settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        self.settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", self.settings),
            patch.object(documents, "get_settings", lambda: self.settings),
            patch.object(search, "get_settings", lambda: self.settings),
            # Keyword overlap stands in for the ColBERT model.
            patch.object(embeddings, "_get_model", lambda: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)

    def create(self, title: str, body: str, status: str = "rough") -> str:
        response = self.client.post(
            "/api/notes", json={"title": title, "body": body, "status": status}
        )
        return response.json()["id"]

    def find(self, q: str, **params) -> list[dict]:
        response = self.client.get("/api/unified-search", params={"q": q, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_combines_exact_words_meaning_and_titles(self) -> None:
        trip = self.create("Lisbon trip", "Book the hotel near Alfama.\n\nPack sunscreen.")
        self.create("Groceries", "Buy hotel soap and tomatoes.")
        results = self.find("Lisbon hotel")
        self.assertEqual(results[0]["document_id"], trip)
        self.assertIn("title", results[0]["matched"])
        # A block with only some of the words still shows up (by meaning/overlap).
        self.assertIn("Buy hotel soap and tomatoes.", [r["text"] for r in results])

    def test_highlights_query_words_in_a_snippet(self) -> None:
        self.create("Long", ("filler " * 80) + "the quarterly budget review is on Friday " + "tail " * 80)
        [result] = self.find("budget")
        self.assertTrue(result["text"].startswith("…"))
        self.assertTrue(result["text"].endswith("…"))
        start, end = result["highlights"][0]
        self.assertEqual(result["text"][start:end].lower(), "budget")

    def test_filters_apply_to_every_source(self) -> None:
        self.create("Rough idea", "Solar panels on the shed")
        polished = self.create("Final plan", "Solar panels on the roof", status="polished")
        results = self.find("solar panels", status="polished")
        self.assertEqual({r["document_id"] for r in results}, {polished})

    def test_one_note_cannot_flood_the_results(self) -> None:
        self.create("Many", "\n\n".join(f"apple fact number {i}" for i in range(10)))
        other = self.create("Other", "An apple a day")
        results = self.find("apple")
        self.assertLessEqual(sum(r["document_id"] != other for r in results), 3)
        self.assertIn(other, [r["document_id"] for r in results])

    def test_trashed_notes_and_empty_queries_return_nothing(self) -> None:
        gone = self.create("Gone", "zebracorn")
        self.client.delete(f"/api/notes/{gone}")
        self.assertEqual(self.find("zebracorn"), [])
        self.assertEqual(self.find("   "), [])


if __name__ == "__main__":
    unittest.main()
