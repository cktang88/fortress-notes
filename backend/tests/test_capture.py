import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class QuickCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

        self._directory = tempfile.TemporaryDirectory()
        self.notes = Path(self._directory.name)
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=self.notes / ".fortress.sqlite3",
            notes_path=self.notes,
            assets_path=self.notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)

    def texts(self, document_id: str) -> list[str]:
        tree = self.client.get(f"/api/block-documents/{document_id}").json()
        return [block["text"] for block in tree["children"]]

    def test_first_capture_creates_the_inbox_and_later_ones_append(self) -> None:
        first = self.client.post("/api/capture", json={"text": "Call the plumber"})
        self.assertEqual(first.status_code, 201, first.text)
        inbox = first.json()["document_id"]
        self.assertEqual(self.texts(inbox), ["Call the plumber"])

        second = self.client.post("/api/capture", json={"text": "Idea: shared calendar\n\nBuy bulbs"})
        self.assertEqual(second.json()["document_id"], inbox)
        self.assertEqual(
            self.texts(inbox), ["Call the plumber", "Idea: shared calendar", "Buy bulbs"]
        )
        titles = [item["title"] for item in self.client.get("/api/navigation").json()["items"]]
        self.assertEqual(titles, ["Inbox"])
        self.assertIn("Buy bulbs", (self.notes / f"{inbox}.md").read_text())
        hits = self.client.get("/api/block-search", params={"q": "plumber"}).json()
        self.assertEqual([hit["document_id"] for hit in hits], [inbox])

    def test_a_trashed_inbox_is_replaced_and_blank_text_is_refused(self) -> None:
        inbox = self.client.post("/api/capture", json={"text": "one"}).json()["document_id"]
        self.client.delete(f"/api/notes/{inbox}")
        fresh = self.client.post("/api/capture", json={"text": "two"}).json()["document_id"]
        self.assertNotEqual(fresh, inbox)
        self.assertEqual(self.texts(fresh), ["two"])
        self.assertEqual(self.client.post("/api/capture", json={"text": " \n "}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
