import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class WorkspaceFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import main, notes_store

        self.main = main
        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        self.notes = notes
        self.settings = SimpleNamespace(
            block_db_enabled=True,
            block_db_import_on_startup=True,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        self.settings.assets_path.mkdir()
        patches = [
            patch.object(main, "settings", self.settings),
            patch.object(notes_store, "get_settings", lambda: self.settings),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)

    def create(self, title: str, body: str = "", tags: list[str] | None = None, folder=None):
        response = self.client.post(
            "/api/notes",
            json={"title": title, "body": body, "tags": tags or [], "folder_id": folder},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_trash_restore_and_delete_forever(self) -> None:
        folder = self.client.post("/api/folders", json={"name": "Work"}).json()
        kept = self.create("Keep me", "Recoverable words", folder=folder["id"])
        gone = self.create("Gone", "Purged words")

        self.assertEqual(self.client.delete(f"/api/notes/{kept['id']}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/notes/{gone['id']}").status_code, 204)
        trash = self.client.get("/api/trash").json()
        self.assertEqual({item["id"] for item in trash}, {kept["id"], gone["id"]})
        self.assertEqual(self.client.get(f"/api/notes/{kept['id']}").status_code, 404)

        restored = self.client.post(f"/api/trash/{kept['id']}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["folder_id"], folder["id"])
        # The compatibility mirror comes back so the editor header loads again.
        self.assertEqual(self.client.get(f"/api/notes/{kept['id']}").json()["title"], "Keep me")
        hits = self.client.get("/api/block-search", params={"q": "Recoverable"}).json()
        self.assertEqual([hit["document_id"] for hit in hits], [kept["id"]])

        self.assertEqual(self.client.delete(f"/api/trash/{gone['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/trash").json(), [])
        self.assertEqual(self.client.post(f"/api/trash/{gone['id']}/restore").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/trash/{kept['id']}").status_code, 404)

    def test_empty_trash(self) -> None:
        for title in ("One", "Two"):
            note = self.create(title)
            self.client.delete(f"/api/notes/{note['id']}")
        self.assertEqual(self.client.delete("/api/trash").json(), {"deleted": 2})
        self.assertEqual(self.client.get("/api/trash").json(), [])

    def test_saved_searches(self) -> None:
        created = self.client.post(
            "/api/saved-searches",
            json={"name": "", "query": "budget", "filters": {"status": "rough", "tag": ""}},
        )
        self.assertEqual(created.status_code, 201)
        saved = created.json()
        self.assertEqual(saved["name"], "budget")
        self.assertEqual(saved["filters"], {"status": "rough"})
        self.assertEqual([item["id"] for item in self.client.get("/api/saved-searches").json()], [saved["id"]])
        self.assertEqual(self.client.delete(f"/api/saved-searches/{saved['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/saved-searches").json(), [])
        self.assertEqual(self.client.delete(f"/api/saved-searches/{saved['id']}").status_code, 404)

    def test_backup_restore_round_trip(self) -> None:
        before = self.create("Before backup", "original text")
        backup = self.client.post("/api/backups")
        self.assertEqual(backup.status_code, 201)
        name = backup.json()["name"]
        after = self.create("After backup", "later text")

        download = self.client.get(f"/api/backups/{name}/download")
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.content.startswith(b"SQLite format 3"))

        restored = self.client.post(f"/api/backups/{name}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        ids = {doc["id"] for doc in self.client.get("/api/block-documents").json()}
        self.assertEqual(ids, {before["id"]})
        self.assertEqual(self.client.get(f"/api/notes/{after['id']}").status_code, 404)
        # Nothing is lost: the newer state is kept as a pre-restore snapshot.
        kinds = {item["kind"] for item in self.client.get("/api/backups").json()}
        self.assertEqual(kinds, {"manual", "pre-restore"})
        safety = restored.json()["safety_backup"]
        self.client.post(f"/api/backups/{safety}/restore")
        ids = {doc["id"] for doc in self.client.get("/api/block-documents").json()}
        self.assertEqual(ids, {before["id"], after["id"]})
        self.assertEqual(self.client.get(f"/api/notes/{after['id']}").json()["title"], "After backup")

        self.assertEqual(self.client.post("/api/backups/../etc/restore").status_code, 404)
        self.assertEqual(self.client.get("/api/backups/nope.sqlite3/download").status_code, 404)

    def test_corrupt_backup_is_refused(self) -> None:
        from app import backups

        directory = backups.backup_directory(self.notes)
        directory.mkdir()
        name = "fortress-20260101T000000000000Z-manual.sqlite3"
        (directory / name).write_bytes(b"not a database")
        self.create("Still here")
        response = self.client.post(f"/api/backups/{name}/restore")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.client.get("/api/block-documents").json()), 1)

    def test_daily_backup_and_pruning(self) -> None:
        from app import backups, block_store

        block_store.initialize(self.settings.block_db_path)
        directory = backups.backup_directory(self.notes)
        first = backups.ensure_daily_backup(self.settings.block_db_path, directory)
        self.assertIsNotNone(first)
        self.assertIsNone(backups.ensure_daily_backup(self.settings.block_db_path, directory))
        later = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertIsNotNone(
            backups.ensure_daily_backup(self.settings.block_db_path, directory, now=later)
        )
        for _ in range(10):
            backups.create_backup(self.settings.block_db_path, directory, "auto")
        self.assertEqual(len(backups.list_backups(directory)), backups.KEEP["auto"])

    def test_upload_import_and_downloads(self) -> None:
        folder = self.client.post("/api/folders", json={"name": "Imported / Stuff"}).json()
        response = self.client.post(
            "/api/markdown-import/files",
            data={"folder_id": folder["id"]},
            files=[
                (
                    "files",
                    (
                        "trip.md",
                        b"---\ntags: [travel]\n---\n# Packing\n\n![x](/media/01J00000000000000000000000.png)\n",
                        "text/markdown",
                    ),
                ),
                ("files", ("plain.md", "Just text".encode(), "text/markdown")),
                ("files", ("bad.md", b"\xff\xfe\x00bad", "text/markdown")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([item["title"] for item in result["imported"]], ["trip", "plain"])
        self.assertEqual([item["name"] for item in result["errors"]], ["bad.md"])
        trip_id = result["imported"][0]["id"]
        self.assertEqual(self.client.get(f"/api/notes/{trip_id}").json()["tags"], ["travel"])
        navigation = self.client.get("/api/navigation").json()
        self.assertEqual(len(navigation["items"][0]["children"]), 2)

        single = self.client.get(f"/api/block-documents/{trip_id}/markdown?download=true")
        self.assertIn('filename="trip.md"', single.headers["content-disposition"])

    def test_upload_import_rejects_unknown_folder(self) -> None:
        response = self.client.post(
            "/api/markdown-import/files",
            data={"folder_id": "missing"},
            files=[("files", ("a.md", b"hello", "text/markdown"))],
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
