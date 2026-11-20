import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from support import make_settings, use_settings


class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

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

    def test_batch_documents_returns_trees_with_their_etags(self) -> None:
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
        # Each body is the single GET's body, under the ETag the GET answers 304 to.
        single = self.client.get(f"/api/block-documents/{first['id']}")
        self.assertEqual(body["etags"][first["id"]], single.headers["etag"])
        self.assertEqual(body["documents"][0], single.json())

    def test_batch_documents_skips_known_etags(self) -> None:
        first = self.client.post(
            "/api/block-documents", json={"title": "First", "body": "First body"}
        )
        second = self.client.post(
            "/api/block-documents", json={"title": "Second", "body": "Second body"}
        )
        response = self.client.post(
            "/api/batch/block-documents",
            json={
                "ids": [first.json()["id"], second.json()["id"]],
                "known": {
                    first.json()["id"]: first.headers["x-document-etag"],
                    second.json()["id"]: '"stale"',
                },
            },
        )
        body = response.json()
        self.assertEqual(body["unchanged"], [first.json()["id"]])
        self.assertEqual([d["id"] for d in body["documents"]], [second.json()["id"]])

    def test_a_linked_note_rename_makes_the_batch_send_the_note_again(self) -> None:
        """One validator: a batch \"unchanged\" means exactly what a GET 304 means."""

        target = self._create("Old title")
        source = self.client.post(
            "/api/block-documents",
            json={"title": "A", "body": f"see [[{target['id']}|Old title]]"},
        )
        tag = source.headers["x-document-etag"]
        self.client.patch(f"/api/block-documents/{target['id']}", json={"title": "New title"})
        body = self.client.post(
            "/api/batch/block-documents",
            json={"ids": [source.json()["id"]], "known": {source.json()["id"]: tag}},
        ).json()
        self.assertEqual(body["unchanged"], [])
        self.assertIn("New title", str(body["documents"][0]))
        single = self.client.get(
            f"/api/block-documents/{source.json()['id']}", headers={"If-None-Match": tag}
        )
        self.assertEqual(single.status_code, 200)

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
