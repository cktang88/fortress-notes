import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class AskYourNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import ask, documents, embeddings, main, search

        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
            patch.object(search, "get_settings", lambda: settings),
            patch.object(ask, "get_settings", lambda: settings),
            patch.object(embeddings, "_get_model", lambda: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)
        self.hotel = self.client.post(
            "/api/notes",
            json={"title": "Lisbon trip", "body": "We picked the hotel in Alfama, 3 nights."},
        ).json()["id"]
        self.client.post("/api/notes", json={"title": "Groceries", "body": "Milk and eggs"})

    def ask(self, question: str, reply: dict) -> tuple[dict, AsyncMock]:
        from app import llm

        fake = AsyncMock(return_value=reply)
        with patch.object(llm, "chat_json", fake):
            response = self.client.post("/api/ask", json={"question": question})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), fake

    def test_answers_with_citations_that_point_at_real_paragraphs(self) -> None:
        result, fake = self.ask(
            "Which hotel did we pick in Lisbon?",
            {
                "found": True,
                "sentences": [
                    {
                        "text": "You picked the hotel in Alfama for 3 nights.",
                        "citations": [{"id": "S1", "quote": "picked the hotel in  Alfama"}],
                    }
                ],
            },
        )
        self.assertEqual(result["status"], "answered")
        [sentence] = result["answer"]
        [citation] = sentence["citations"]
        self.assertEqual(citation["document_id"], self.hotel)
        self.assertEqual(citation["document_title"], "Lisbon trip")
        # Only the matching paragraphs are sent, never unrelated notes.
        sent = fake.call_args.args[1]
        self.assertIn("Alfama", sent)
        self.assertNotIn("Milk and eggs", sent)

    def test_drops_sentences_whose_quotes_are_not_in_the_notes(self) -> None:
        result, _ = self.ask(
            "Which hotel did we pick?",
            {
                "found": True,
                "sentences": [
                    {"text": "It was the Ritz.", "citations": [{"id": "S1", "quote": "the Ritz"}]},
                    {"text": "Uncited claim.", "citations": []},
                    {"text": "Bad source.", "citations": [{"id": "S99", "quote": "Alfama"}]},
                ],
            },
        )
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["answer"], [])
        self.assertTrue(result["sources"])  # the closest passages are still offered

    def test_reports_missing_key_and_empty_matches_without_calling_the_model(self) -> None:
        from app import llm

        with patch.object(llm, "chat_json", AsyncMock(side_effect=llm.LLMNotConfigured)):
            result = self.client.post("/api/ask", json={"question": "hotel?"}).json()
        self.assertEqual(result["status"], "not_configured")
        result, fake = self.ask("zebracorn quantum", {"found": False, "sentences": []})
        self.assertEqual(result["status"], "not_found")
        fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
