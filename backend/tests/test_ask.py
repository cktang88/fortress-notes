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

    def ask(self, question: str, reply, ) -> tuple[dict, AsyncMock]:
        from app import llm

        fake = AsyncMock(side_effect=reply) if isinstance(reply, list) else AsyncMock(
            return_value=reply
        )
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

    def test_prefetch_greps_exact_names_before_any_model_call(self) -> None:
        miguel = self.client.post(
            "/api/notes", json={"title": "Contractors", "body": "Miguel quoted 12k for the deck."}
        ).json()["id"]
        result, fake = self.ask(
            "What did Miguel quote?",
            {
                "action": "answer",
                "found": True,
                "sentences": [
                    {"text": "Miguel quoted 12k.", "citations": [{"id": "S1", "quote": "Miguel quoted 12k"}]}
                ],
            },
        )
        self.assertEqual(fake.call_count, 1)  # answered in one model call
        self.assertEqual(result["answer"][0]["citations"][0]["document_id"], miguel)
        self.assertEqual(result["steps"], [])
        self.assertIn("elapsed_ms", result)

    def test_model_can_grep_then_answer_from_the_new_passages(self) -> None:
        self.client.post(
            "/api/notes", json={"title": "Dentist", "body": "Appointment moved to 14 March."}
        )
        result, fake = self.ask(
            "Which hotel did we pick?",
            [
                {"action": "grep", "pattern": r"appointment|\d+ March"},
                {
                    "action": "answer",
                    "found": True,
                    "sentences": [
                        {
                            "text": "The appointment is on 14 March.",
                            "citations": [{"id": "S2", "quote": "moved to 14 March"}],
                        }
                    ],
                },
            ],
        )
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result["steps"], [{"action": "grep", "detail": r"appointment|\d+ March"}])
        self.assertIn("New sources", fake.call_args_list[1].args[1])
        self.assertEqual(result["status"], "answered")

    def test_running_out_of_time_forces_an_answer_and_never_hangs(self) -> None:
        from app import ask

        ticks = iter([0.0, 0.0, 2.5, 2.5, 2.6, 2.6, 2.6])
        with patch.object(ask, "_clock", lambda: next(ticks, 2.6)):
            result, fake = self.ask(
                "Which hotel did we pick?",
                [
                    {"action": "read", "note": "Lisbon trip"},
                    {"action": "grep", "pattern": "hotel"},
                ],
            )
        # The second call was told to answer now; a non-answer means "not found".
        self.assertIn("out of time", fake.call_args_list[1].args[1])
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result["status"], "not_found")

        from app import llm

        async def slow(*_args, **_kwargs):
            raise TimeoutError

        with patch.object(llm, "chat_json", slow):
            timed_out = self.client.post("/api/ask", json={"question": "hotel?"}).json()
        self.assertEqual(timed_out["status"], "timeout")
        self.assertTrue(timed_out["sources"])

        async def unreachable(*_args, **_kwargs):
            raise ConnectionError("proxy said 403")

        with patch.object(llm, "chat_json", unreachable):
            down = self.client.post("/api/ask", json={"question": "hotel?"}).json()
        self.assertEqual(down["status"], "unavailable")
        self.assertTrue(down["sources"])

    def test_read_tool_returns_a_whole_note(self) -> None:
        from app import ask

        rows = ask.read_note("lisbon")
        self.assertEqual([row["document_title"] for row in rows], ["Lisbon trip"])
        self.assertEqual(ask.read_note("no such note"), [])
        self.assertEqual(ask.key_terms("What did Miguel say on 14 March?"), ["miguel", "march", "say", "14"])


if __name__ == "__main__":
    unittest.main()
