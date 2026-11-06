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

    def folder(self) -> dict:
        items = self.client.get("/api/navigation").json()["items"]
        [folder] = [item for item in items if item["kind"] == "folder"]
        return folder

    def test_each_capture_is_its_own_note_in_uncategorized_newest_first(self) -> None:
        self.client.post("/api/notes", json={"title": "Existing", "body": "x"})
        first = self.client.post("/api/capture", json={"text": "Call the plumber"})
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            "/api/capture", json={"text": "Idea: shared calendar\nwith the whole family"}
        ).json()
        self.assertEqual(second["title"], "Idea: shared calendar")
        self.assertEqual(second["folder"], "Uncategorized")

        folder = self.folder()
        self.assertEqual(folder["name"], "Uncategorized")
        self.assertEqual(folder["position"], 0)  # sits at the top of the sidebar
        self.assertEqual(
            [child["title"] for child in folder["children"]],
            ["Idea: shared calendar", "Call the plumber"],
        )
        note = self.client.get(f"/api/notes/{second['document_id']}").json()
        self.assertIn("with the whole family", note["body"])
        self.assertTrue((self.notes / f"{second['document_id']}.md").exists())
        hits = self.client.get("/api/block-search", params={"q": "plumber"}).json()
        self.assertEqual([hit["document_id"] for hit in hits], [first.json()["document_id"]])

    def test_a_deleted_folder_is_recreated_and_blank_text_is_refused(self) -> None:
        note = self.client.post("/api/capture", json={"text": "one"}).json()
        self.client.post(
            f"/api/block-documents/{note['document_id']}/move", json={"folder_id": None}
        )
        self.client.delete(f"/api/folders/{self.folder()['id']}")
        self.client.post("/api/capture", json={"text": "two"})
        self.assertEqual([c["title"] for c in self.folder()["children"]], ["two"])
        self.assertEqual(self.client.post("/api/capture", json={"text": " \n "}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
