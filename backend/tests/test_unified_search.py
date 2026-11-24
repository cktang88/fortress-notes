import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from support import make_settings, use_settings


class UnifiedSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import embeddings, main

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        self.settings = make_settings(notes, reindex_interval_s=5.0)
        for item in (
            use_settings(self.settings),
            # Keyword overlap stands in for the ColBERT model.
            patch.object(embeddings, "_get_model", lambda wait=False: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, title: str, body: str) -> str:
        response = self.client.post("/api/block-documents", json={"title": title, "body": body})
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

    def test_stopwords_make_no_title_matches(self) -> None:
        # "a" starts "and", "to" is a word of "Books to read": neither says anything
        # about what the query is after (found by scripts/eval_search.py).
        self.create("Sam and Lee's wedding", "Wedding in June in the Cotswolds")
        self.create("Books to read", "The Overstory")
        self.assertEqual(self.find("how to file a patent"), [])

    def test_stopwords_dont_count_towards_covering_the_query(self) -> None:
        # Both share "zqmarl"; only the second also has the other content word.
        # The first is full of the query's stopwords, which used to count as
        # covering it and put it first.
        wordy = self.create("Wordy", "is it the zqmarl for my a the in it")
        exact = self.create("Exact", "zqmarl budget")
        results = self.find("what is the zqmarl budget for my trip")
        self.assertEqual(results[0]["document_id"], exact)
        self.assertIn(wordy, [r["document_id"] for r in results])

    def test_a_title_sharing_under_half_the_query_is_not_a_match(self) -> None:
        # "quantum computing lecture notes" used to list every note titled "... notes".
        self.create("Standup notes", "Maya is blocked on credentials")
        found = self.find("quantum computing lecture notes")
        self.assertFalse([r for r in found if "title" in r["matched"]], found)
        # Half the words is enough ("Lisbon hotel" -> "Lisbon trip").
        trip = self.create("Lisbon trip", "Pack sunscreen")
        self.assertEqual(self.find("Lisbon hotel")[0]["document_id"], trip)

    def test_meaning_cutoffs_search_strict_ask_loose(self) -> None:
        # Made-up MaxSim scores over 10 query tokens, median 7.0: "answer" normalizes
        # to 0.15 (under the search cutoff, over Ask's), "close" to 0.5 and "near" to
        # 0.25 (over the search cutoff but under 0.6 of the best).
        from app import corpus, embeddings, relevance, search

        ids = {
            name: self.create(name.title(), f"zz {name} paragraph")
            for name in ("answer", "close", "near", *(f"other{i}" for i in range(6)))
        }
        notes = corpus.snapshot(self.settings.block_db_path).notes
        block = {name: notes[note].rows[0][0] for name, note in ids.items()}
        normalized = {"answer": 0.15, "close": 0.5, "near": 0.25}
        raw = {block[name]: 7.0 + 3.0 * normalized.get(name, 0.0) for name in block}
        with patch.object(embeddings, "available", return_value=True), patch.object(
            embeddings, "score_documents", return_value=raw
        ), patch.object(relevance, "query_token_count", return_value=10):
            shown = {r["document_id"] for r in search.unified_search("qqq")}
            for_ask = {r["document_id"] for r in search.unified_search("qqq", recall=True)}
        self.assertEqual(shown, {ids["close"]})
        self.assertEqual(for_ask, {ids["close"], ids["near"], ids["answer"]})

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
        touched = self.client.patch(f"/api/block-documents/{packing}", json={"title": "Packing zqmarl"})
        self.assertEqual(touched.status_code, 200, touched.text)
        results = self.find("hotel zqmarl")
        self.assertEqual(results[0]["document_id"], budget)

    def test_one_note_cannot_flood_the_results(self) -> None:
        self.create("Many", "\n\n".join(f"apple fact number {i}" for i in range(10)))
        other = self.create("Other", "An apple a day")
        results = self.find("apple")
        self.assertLessEqual(sum(r["document_id"] != other for r in results), 3)
        self.assertIn(other, [r["document_id"] for r in results])

    def test_trashed_notes_and_empty_queries_return_nothing(self) -> None:
        gone = self.create("Gone", "zebracorn")
        self.client.delete(f"/api/block-documents/{gone}")
        self.assertEqual(self.find("zebracorn"), [])
        self.assertEqual(self.find("   "), [])


if __name__ == "__main__":
    unittest.main()
