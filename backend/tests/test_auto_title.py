import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class AutoTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main, search

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
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)

    def write(self, document: dict, text: str) -> dict:
        block = document["children"][0]
        response = self.client.post(
            f"/api/block-documents/{document['id']}/transactions",
            json={
                "base_revision": document["revision"],
                "operations": [{"operation": "update", "block_id": block["id"], "text": text}],
            },
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

        undone = self.client.post(
            f"/api/block-documents/{document['id']}/undo",
            json={"base_revision": document["revision"]},
        ).json()
        self.assertEqual(undone["title"], "Trip to Lisbon")

    def test_naming_a_note_stops_auto_titles_and_clearing_resumes_them(self) -> None:
        document = self.client.post("/api/block-documents", json={}).json()
        document = self.write(document, "First thoughts")
        self.client.patch(f"/api/block-documents/{document['id']}", json={"title": "Plans"})
        document = self.client.get(f"/api/block-documents/{document['id']}").json()
        self.assertFalse(document["title_auto"])
        document = self.write(document, "Something else entirely")
        self.assertEqual(document["title"], "Plans")

        # A status change alone keeps the chosen title.
        self.client.patch(f"/api/block-documents/{document['id']}", json={"status": "polished"})
        self.assertEqual(
            self.client.get(f"/api/block-documents/{document['id']}").json()["title"], "Plans"
        )

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
