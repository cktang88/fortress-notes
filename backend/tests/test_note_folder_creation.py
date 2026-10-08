import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class NoteFolderCreationTests(unittest.TestCase):
    def test_nested_note_creation_persists_and_missing_folder_leaves_no_note(self) -> None:
        from app import documents, main

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            database = notes / ".fortress.sqlite3"
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

            with (
                patch.object(main, "settings", settings),
                patch.object(documents, "get_settings", return_value=settings),
                TestClient(main.app) as client,
            ):
                parent = client.post("/api/folders", json={"name": "Projects"}).json()
                child = client.post(
                    "/api/folders",
                    json={"name": "Research", "parent_id": parent["id"]},
                ).json()

                created = client.post(
                    "/api/notes",
                    json={"title": "Research notes", "body": "", "folder_id": child["id"]},
                )
                self.assertEqual(created.status_code, 201)
                note_id = created.json()["id"]
                navigation = client.get("/api/navigation").json()["items"]
                projects = next(folder for folder in navigation if folder["name"] == "Projects")
                research = next(folder for folder in projects["children"] if folder["name"] == "Research")
                self.assertEqual(research["children"][0]["id"], note_id)

                missing = client.post(
                    "/api/notes",
                    json={"title": "Orphan", "body": "", "folder_id": "missing-folder"},
                )

            self.assertEqual(missing.status_code, 404)
            self.assertEqual({path.stem for path in notes.glob("*.md")}, {note_id})
            with sqlite3.connect(database) as connection:
                stored_ids = {
                    row[0] for row in connection.execute("SELECT id FROM documents")
                }
            self.assertEqual(stored_ids, {note_id})


if __name__ == "__main__":
    unittest.main()
