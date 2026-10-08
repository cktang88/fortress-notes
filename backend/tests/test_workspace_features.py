import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class WorkspaceFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main, search

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
            patch.object(documents, "get_settings", lambda: self.settings),
            patch.object(search, "get_settings", lambda: self.settings),
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


    # --- Regression tests for review findings ---------------------------------

    def test_restoring_the_oldest_safety_snapshot_keeps_its_data(self) -> None:
        from app import backups

        self.create("Keep me", "important data")
        name = self.client.post("/api/backups").json()["name"]
        first_safety = self.client.post(f"/api/backups/{name}/restore").json()["safety_backup"]
        for _ in range(backups.KEEP["pre-restore"]):
            self.client.post(f"/api/backups/{name}/restore")
        names = {item["name"] for item in self.client.get("/api/backups").json()}
        oldest = min(
            (n for n in names if n.endswith("pre-restore.sqlite3")),
            key=lambda n: n,
        )
        self.assertNotEqual(oldest, first_safety)  # pruning has happened
        response = self.client.post(f"/api/backups/{oldest}/restore")
        self.assertEqual(response.status_code, 200, response.text)
        titles = [doc["title"] for doc in self.client.get("/api/block-documents").json()]
        self.assertEqual(titles, ["Keep me"])
        self.assertTrue(self.notes.joinpath(f"{self.client.get('/api/notes').json()[0]['id']}.md").exists())

    def test_restore_makes_open_editors_conflict_instead_of_overwriting(self) -> None:
        note = self.create("Doc", "before")
        tree = self.client.get(f"/api/block-documents/{note['id']}").json()
        name = self.client.post("/api/backups").json()["name"]
        self.client.post(f"/api/backups/{name}/restore")
        stale = self.client.post(
            f"/api/block-documents/{note['id']}/transactions",
            json={
                "base_revision": tree["revision"],
                "operations": [
                    {"operation": "update", "block_id": tree["children"][0]["id"], "text": "stale"}
                ],
            },
        )
        self.assertEqual(stale.status_code, 409)

    def test_snapshots_are_self_contained_files(self) -> None:
        from app import backups

        self.create("Doc")
        name = self.client.post("/api/backups").json()["name"]
        directory = backups.backup_directory(self.notes)
        self.client.get(f"/api/backups/{name}/download")
        self.client.post(f"/api/backups/{name}/restore")
        leftovers = [p.name for p in directory.iterdir() if p.name.endswith(("-wal", "-shm"))]
        self.assertEqual(leftovers, [])

    def test_restored_note_is_searchable_after_an_index_rebuild(self) -> None:
        from app import block_store

        note = self.create("Alpha", "zebracorn text")
        self.client.delete(f"/api/notes/{note['id']}")
        self.assertTrue(block_store.fts_is_consistent(self.settings.block_db_path))
        block_store.ensure_fts_integrity(self.settings.block_db_path)  # as at startup
        self.client.post(f"/api/trash/{note['id']}/restore")
        hits = self.client.get("/api/block-search", params={"q": "zebracorn"}).json()
        self.assertEqual([hit["document_id"] for hit in hits], [note["id"]])

    def test_trashing_keeps_folder_order_dense(self) -> None:
        folder = self.client.post("/api/folders", json={"name": "F"}).json()["id"]
        ids = {title: self.create(title, folder=folder)["id"] for title in "ABC"}
        self.client.delete(f"/api/notes/{ids['B']}")
        self.create("D", folder=folder)
        children = self.client.get("/api/navigation").json()["items"][0]["children"]
        self.assertEqual(
            [(child["title"], child["position"]) for child in children],
            [("A", 0), ("C", 1), ("D", 2)],
        )
        self.assertEqual(self.client.delete(f"/api/notes/{ids['B']}").status_code, 404)

    def test_markdown_mirror_follows_the_document_lifecycle(self) -> None:
        created = self.client.post("/api/block-documents", json={"title": "Block doc"}).json()
        mirror = self.notes / f"{created['id']}.md"
        self.assertTrue(mirror.exists())
        self.assertEqual(self.client.get(f"/api/notes/{created['id']}").json()["title"], "Block doc")

        self.client.patch(f"/api/block-documents/{created['id']}", json={"status": "polished"})
        self.assertIn("status: polished", mirror.read_text())
        self.assertEqual(
            [note["id"] for note in self.client.get("/api/notes?status=polished").json()],
            [created["id"]],
        )

        self.client.delete(f"/api/block-documents/{created['id']}")
        self.assertFalse(mirror.exists())
        self.assertEqual(self.client.get("/api/notes").json(), [])
        self.assertEqual(
            self.client.put(f"/api/notes/{created['id']}", json={"status": "rough"}).status_code,
            404,
        )
        self.client.delete(f"/api/trash/{created['id']}")
        self.assertEqual(
            self.client.put(f"/api/notes/{created['id']}", json={"body": "back?"}).status_code, 404
        )
        self.assertEqual(self.client.get("/api/block-documents").json(), [])

    def test_mirrors_never_feed_back_into_reads(self) -> None:
        (self.notes / "broken.md").write_text("---\ntitle: [unclosed\n---\nbody", encoding="utf-8")
        self.create("Fine", "needle text")
        self.assertEqual([n["title"] for n in self.client.get("/api/notes").json()], ["Fine"])
        found = self.client.get("/api/search", params={"q": "needle"}).json()
        self.assertEqual([hit["note"]["title"] for hit in found], ["Fine"])

    def test_upload_import_tolerates_odd_front_matter(self) -> None:
        response = self.client.post(
            "/api/markdown-import/files",
            files=[
                ("files", ("odd.md", b"---\nstatus: [a]\ncreated_at: 2020-05-01T00:00:00Z\n---\nhi", "text/markdown")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        note_id = response.json()["imported"][0]["id"]
        note = self.client.get(f"/api/notes/{note_id}").json()
        self.assertEqual(note["status"], "rough")
        self.assertTrue(note["created_at"].startswith("2020-05-01"))


if __name__ == "__main__":
    unittest.main()
