import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class FocusedRelatedTests(unittest.TestCase):
    def test_focus_changes_query_and_rejects_other_documents_blocks(self) -> None:
        from app import documents, embeddings, main, search

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
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
            with (
                patch.object(main, "settings", settings),
                patch.object(documents, "get_settings", return_value=settings),
                patch.object(search, "get_settings", return_value=settings),
                patch.object(embeddings, "get_settings", return_value=settings),
                TestClient(main.app) as client,
            ):
                def create(title: str, body: str) -> dict:
                    response = client.post("/api/notes", json={"title": title, "body": body})
                    self.assertEqual(response.status_code, 201)
                    return response.json()

                source = create("Topics", "astronomy galaxy stars\n\nbaking bread flour")
                stars = create("Space", "astronomy galaxy stars")
                bread = create("Kitchen", "baking bread flour")
                blank = create("Blank", "")
                blocks = client.get(f"/api/block-documents/{source['id']}").json()["children"]
                endpoint = f"/api/notes/{source['id']}/related"

                whole_note = client.get(endpoint).json()
                self.assertEqual({result["note"]["id"] for result in whole_note}, {stars["id"], bread["id"]})
                for block, expected in zip(blocks, (stars, bread), strict=True):
                    response = client.get(endpoint, params={"block_id": block["id"]})
                    self.assertEqual(response.status_code, 200)
                    matches = response.json()
                    self.assertEqual([result["note"]["id"] for result in matches], [expected["id"]])
                    self.assertTrue(matches[0]["matched_block_id"])

                blank_block = client.get(f"/api/block-documents/{blank['id']}").json()["children"][0]
                self.assertEqual(client.get(f"/api/notes/{blank['id']}/related", params={"block_id": blank_block["id"]}).json(), [])
                self.assertEqual(client.get(endpoint, params={"block_id": blank_block["id"]}).status_code, 404)
                self.assertEqual(client.get(endpoint, params={"block_id": "missing"}).status_code, 404)
