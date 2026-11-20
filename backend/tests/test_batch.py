import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from support import make_settings, use_settings


class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import main

        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        settings = make_settings(notes, reindex_interval_s=5.0)
        self._patches = [
            use_settings(settings),
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
