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

    @staticmethod
    def js_slice(text: str, start: int, end: int) -> str:
        """``text.slice(start, end)`` as the browser does it (UTF-16 code units)."""

        units = text.encode("utf-16-le")
        return units[start * 2 : end * 2].decode("utf-16-le")

    def test_highlight_offsets_survive_emoji_and_case_changing_letters(self) -> None:
        # "İ" lowercases to two code points and "😀" is two UTF-16 units in the browser.
        self.create("Trip", "Met İstanbul friends 😀 at the harbour")
        [result] = self.find("friends harbour")
        marked = [self.js_slice(result["text"], a, b) for a, b in result["highlights"]]
        self.assertEqual(marked, ["friends", "harbour"])

    def test_highlights_ignore_accents_like_the_word_search_does(self) -> None:
        self.create("Plans", "Meet at the café for crème brûlée")
        [result] = self.find("cafe creme")
        self.assertIn("words", result["matched"])
        marked = [self.js_slice(result["text"], a, b) for a, b in result["highlights"]]
        self.assertEqual(marked, ["café", "crème"])

    def test_highlights_whole_words_not_letters_inside_words(self) -> None:
        self.create("Languages", "I code in c++ and rust")
        [result] = self.find("c++")
        marked = [self.js_slice(result["text"], a, b) for a, b in result["highlights"]]
        self.assertEqual(marked, ["c"])

    def test_title_matches_ignore_case_for_accented_letters(self) -> None:
        school = self.create("ÉCOLE", "Notes about lessons")
        results = self.find("école")
        self.assertEqual([r["document_id"] for r in results], [school])
        self.assertIn("title", results[0]["matched"])

    def test_title_matches_need_a_word_not_a_letter_inside_one(self) -> None:
        self.create("Café plans", "Nothing relevant here")
        # "a" is inside both title words but no title word starts with it.
        self.assertEqual(self.find("a"), [])

    def test_a_title_naming_every_query_word_beats_one_that_shares_a_word(self) -> None:
        # Reported from the e2e suite: "Structured trip plan" matched "trip" in both its
        # title and its text, and outranked the note whose title has every word.
        self.create("Structured trip plan", "Trip checklist: passport, chargers, trip insurance.")
        lake = self.create("Lake trip zqvarn", "Rent a cabin for the weekend.")
        results = self.find("lake trip zqvarn")
        self.assertEqual(results[0]["document_id"], lake)

    def test_a_result_holding_every_query_word_beats_one_holding_some(self) -> None:
        # "hotel zqmarl": the budget note has "zqmarl" in its title and "hotel" in its
        # text; the packing note (edited later) only shares "zqmarl".
        packing = self.create("Packing zqmarl", "Packing zqmarl\n\nPassport and charger.")
        budget = self.create("Budget zqmarl", "Hotel 120 a night.")
        self.client.patch(f"/api/notes/{packing}", json={"status": "rough"})
        results = self.find("hotel zqmarl")
        self.assertEqual(results[0]["document_id"], budget)

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
