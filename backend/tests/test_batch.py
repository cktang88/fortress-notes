import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        settings = SimpleNamespace(
            block_db_enabled=True,
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            reindex_interval_s=5.0,
        )
        self._patches = [
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", return_value=settings),
        ]
        for item in self._patches:
            item.start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for item in self._patches:
            item.stop()
        self._directory.cleanup()

    def _create(self, title: str, folder_id: str | None = None) -> dict:
        response = self.client.post(
            "/api/block-documents",
            json={"title": title, "body": f"{title} body", "folder_id": folder_id},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_batch_documents_returns_trees_without_marking_opened(self) -> None:
        first = self._create("First")
        second = self._create("Second")
        response = self.client.post(
            "/api/batch/block-documents",
            json={"ids": [first["id"], second["id"], "nope", first["id"]], "known": {}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([d["id"] for d in body["documents"]], [first["id"], second["id"]])
        self.assertEqual(body["missing"], ["nope"])
        self.assertEqual(body["documents"][0]["title"], "First")
        self.assertIsNone(body["documents"][0]["last_opened_at"])
        # The single read is what marks a note opened.
        opened = self.client.get(f"/api/block-documents/{first['id']}").json()
        self.assertIsNotNone(opened["last_opened_at"])

    def test_batch_documents_skips_known_revisions(self) -> None:
        first = self._create("First")
        second = self._create("Second")
        response = self.client.post(
            "/api/batch/block-documents",
            json={
                "ids": [first["id"], second["id"]],
                "known": {first["id"]: first["revision"], second["id"]: second["revision"] - 1},
            },
        )
        body = response.json()
        self.assertEqual(body["unchanged"], [first["id"]])
        self.assertEqual([d["id"] for d in body["documents"]], [second["id"]])

    def test_batch_documents_limits_size(self) -> None:
        response = self.client.post("/api/batch/block-documents", json={"ids": ["x"] * 51})
        self.assertEqual(response.status_code, 422)

    def test_batch_move_moves_folders_and_notes(self) -> None:
        parent = self.client.post("/api/folders", json={"name": "Parent"}).json()
        child = self.client.post(
            "/api/folders", json={"name": "Child", "parent_id": parent["id"]}
        ).json()
        note = self._create("Inside", folder_id=parent["id"])
        response = self.client.post(
            "/api/batch/move",
            json={
                "folders": [{"id": child["id"], "parent_id": None}],
                "documents": [{"id": note["id"], "folder_id": None}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"folders": 1, "documents": 1})
        # The folder is empty now, so it can be deleted.
        self.assertEqual(self.client.delete(f"/api/folders/{parent['id']}").status_code, 204)

    def test_batch_move_reports_missing_items(self) -> None:
        response = self.client.post(
            "/api/batch/move", json={"documents": [{"id": "missing", "folder_id": None}]}
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
