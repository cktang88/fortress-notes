import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class CorruptBlockRecordTests(unittest.TestCase):
    def test_invalid_block_json_is_not_silently_returned_as_valid_document(self) -> None:
        from app import main
        from app.block_store import create_block_document

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            database = notes / ".fortress.sqlite3"
            document = create_block_document(database, "Damaged", "rough", [], "Readable")
            block_id = document["children"][0]["id"]
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE blocks SET content_json = ? WHERE id = ?",
                    ("{not valid json", block_id),
                )

            settings = SimpleNamespace(
                block_db_enabled=True,
                block_db_import_on_startup=False,
                block_db_path=database,
                notes_path=notes,
                assets_path=notes / "assets",
                embeddings_enabled=False,
                vision_enabled=False,
                reindex_interval_s=5.0,
            )
            with patch.object(main, "settings", settings):
                with TestClient(main.app, raise_server_exceptions=False) as client:
                    response = client.get(f"/api/block-documents/{document['id']}")

            self.assertEqual(response.status_code, 500)
            with sqlite3.connect(database) as connection:
                stored = connection.execute(
                    "SELECT content_json FROM blocks WHERE id = ?", (block_id,)
                ).fetchone()[0]
            self.assertEqual(stored, "{not valid json")


if __name__ == "__main__":
    unittest.main()
