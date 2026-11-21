import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from support import make_settings, use_settings


class AutoTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main, search

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        settings = make_settings(notes, reindex_interval_s=5.0)
        patcher = use_settings(settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def write(self, document: dict, text: str) -> dict:
        """Replace the note's blocks with ``text`` parsed as Markdown, as typed."""

        from app.block_markdown import parse_markdown

        operations = [
            {"operation": "delete", "block_id": block["id"]} for block in document["children"]
        ]
        for parsed in parse_markdown(text):
            operations.append({
                "operation": "insert", "type": parsed.type, "attrs": parsed.attrs,
                "content": parsed.content,
            })
        response = self.client.post(
            f"/api/block-documents/{document['id']}/transactions?response=document",
            json={"base_revision": document["revision"], "operations": operations},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_unnamed_note_takes_its_title_from_the_first_line(self) -> None:
        document = self.client.post("/api/block-documents", json={}).json()
        self.assertEqual(document["title"], "Untitled")
        self.assertTrue(document["title_auto"])

        document = self.write(document, "# Trip to Lisbon\nFlights on Friday")
        self.assertEqual(document["title"], "Trip to Lisbon")
        document = self.write(document, "- [ ] Book the hotel near the river")
        self.assertEqual(document["title"], "Book the hotel near the river")
        self.assertEqual(
            self.client.get("/api/navigation").json()["items"][0]["title"],
            "Book the hotel near the river",
        )

    def test_an_unnamed_note_is_created_with_its_auto_title_in_one_transaction(self) -> None:
        from app import block_store

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = block_store.create_block_document(
                database, "Untitled", "# From the body", title_auto=True
            )
            self.assertEqual((document["title"], document["title_auto"]), ("From the body", True))
            # A failure part-way leaves no half-made note behind.
            with patch.object(block_store, "_refresh_auto_title", side_effect=RuntimeError):
                with self.assertRaises(RuntimeError):
                    block_store.create_block_document(database, "Untitled", "Other", title_auto=True)
            self.assertEqual(len(block_store.list_documents(database)), 1)

    def test_naming_a_note_stops_auto_titles_and_clearing_resumes_them(self) -> None:
        document = self.client.post("/api/block-documents", json={}).json()
        document = self.write(document, "First thoughts")
        self.client.patch(f"/api/block-documents/{document['id']}", json={"title": "Plans"})
        document = self.client.get(f"/api/block-documents/{document['id']}").json()
        self.assertFalse(document["title_auto"])
        document = self.write(document, "Something else entirely")
        self.assertEqual(document["title"], "Plans")

        cleared = self.client.patch(f"/api/block-documents/{document['id']}", json={"title": ""})
        self.assertEqual(cleared.json()["title"], "Something else entirely")
        self.assertTrue(cleared.json()["title_auto"])

    def test_named_and_imported_notes_keep_their_titles(self) -> None:
        named = self.client.post("/api/block-documents", json={"title": "Named"}).json()
        self.assertFalse(named["title_auto"])
        self.assertEqual(self.write(named, "Body line")["title"], "Named")

    def test_long_first_lines_are_trimmed_at_a_word(self) -> None:
        from app.block_store import first_line_title

        title = first_line_title(["", "word " * 40])
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 81)
        self.assertEqual(first_line_title(["", "   "]), "Untitled")
        self.assertEqual(first_line_title(["1. Step one"]), "Step one")


if __name__ == "__main__":
    unittest.main()
