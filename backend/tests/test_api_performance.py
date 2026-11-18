"""Performance bugs on the hot paths the native app hits (each proven by a test)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import block_store
from app.models import BlockOperation
from app.block_store import (
    backfill_assets,
    connection_scope,
    create_document,
    fts_is_consistent,
    initialize,
    rebuild_fts,
)


def _document(database: Path, document_id: str, blocks: int) -> None:
    create_document(
        database, document_id, document_id, "rough", [],
        "\n\n".join(f"Paragraph {index} of {document_id}" for index in range(blocks)),
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
    )


class StartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.database = self.root / ".fortress.sqlite3"

    def test_fts_consistency_check_is_not_quadratic(self) -> None:
        # The check joined on the FTS table's UNINDEXED block_id: one full FTS
        # scan per block, ~72 s per check on a 20k-paragraph workspace (run
        # twice at every startup before /api/health answered).
        for number in range(4):
            _document(self.database, f"doc-{number}", 1000)
        started = time.perf_counter()
        self.assertTrue(fts_is_consistent(self.database))
        self.assertLess(time.perf_counter() - started, 0.5)

        with connection_scope(self.database) as connection:
            connection.execute(
                "DELETE FROM blocks_fts WHERE rowid = (SELECT MIN(rowid) FROM blocks_fts)"
            )
        self.assertFalse(fts_is_consistent(self.database))
        rebuild_fts(self.database)
        self.assertTrue(fts_is_consistent(self.database))
        with connection_scope(self.database) as connection:
            connection.execute(
                "INSERT INTO blocks_fts(block_id, document_id, text) VALUES ('gone', 'x', 'y')"
            )
        self.assertFalse(fts_is_consistent(self.database))

    def test_backfill_does_not_rehash_and_reindex_everything_on_every_start(self) -> None:
        media = self.root / "assets"
        media.mkdir()
        (media / "01J00000000000000000000000.png").write_bytes(b"png")
        _document(self.database, "doc", 10)
        self.assertEqual(backfill_assets(self.database, media), 1)
        with (
            patch.object(block_store, "rebuild_fts") as rebuild,
            patch.object(block_store, "_rebuild_document_assets") as relink,
            patch.object(Path, "read_bytes", side_effect=AssertionError("re-read")),
        ):
            self.assertEqual(backfill_assets(self.database, media), 1)
        rebuild.assert_not_called()
        relink.assert_not_called()

    def test_backfill_still_picks_up_a_new_sidecar_for_a_registered_asset(self) -> None:
        media = self.root / "assets"
        media.mkdir()
        image = media / "01J00000000000000000000000.png"
        image.write_bytes(b"png")
        backfill_assets(self.database, media)
        image.with_suffix(".txt").write_text("late OCR", encoding="utf-8")
        backfill_assets(self.database, media)
        with connection_scope(self.database) as connection:
            text = connection.execute("SELECT text FROM assets").fetchone()["text"]
        self.assertEqual(text, "late OCR")

    def test_initialize_runs_the_schema_script_once_per_database(self) -> None:
        initialize(self.database)
        with patch.object(block_store, "_initialize") as slow:
            for _ in range(5):
                initialize(self.database)
            block_store.navigation(self.database)
        slow.assert_not_called()

    def test_initialize_runs_again_for_a_replaced_database_file(self) -> None:
        initialize(self.database)
        self.database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)
        block_store.navigation(self.database)  # recreates the schema
        self.assertEqual(block_store.navigation(self.database)["items"], [])



class LargeDocumentTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database = Path(self._directory.name) / ".fortress.sqlite3"
        _document(self.database, "big", 1500)
        self.tree = block_store.document_tree(self.database, "big")

    def apply(self, *operations: BlockOperation) -> dict:
        tree = block_store.apply_transaction(
            self.database, "big", list(operations), self.tree["revision"]
        )
        self.tree = tree
        return tree

    def test_an_edit_refreshes_only_the_blocks_it_wrote(self) -> None:
        # Every edit re-parsed links, asset links and search text of every
        # block of the note (1,500 here): ~0.5 s per keystroke on 2,000 blocks.
        target = self.tree["children"][10]["id"]
        with (
            patch.object(
                block_store, "_replace_references", wraps=block_store._replace_references
            ) as references,
            patch.object(block_store, "_refresh_fts", wraps=block_store._refresh_fts) as fts,
        ):
            self.apply(BlockOperation(operation="update", block_id=target, text="Edited"))
        self.assertLessEqual(references.call_count, 3)
        self.assertLessEqual(fts.call_count, 3)
        self.assertEqual(
            [hit["block_id"] for hit in block_store.search_blocks(self.database, "Edited")],
            [target],
        )
        self.assertEqual(block_store.search_blocks(self.database, "Paragraph 10 of big"), [])
        self.assertTrue(fts_is_consistent(self.database))

    def test_links_and_assets_of_written_blocks_stay_correct(self) -> None:
        target = self.tree["children"][0]["id"]
        mentioning = self.tree["children"][1]["id"]
        new_id = "01J00000000000000000000077"
        # A reference typed before its target exists...
        self.apply(BlockOperation(operation="update", block_id=mentioning,
                                  text=f"see (({new_id}))"))
        self.assertEqual(block_store.document_backlinks(self.database, "big"), [])
        # ...is linked once the target is inserted.
        self.apply(BlockOperation(operation="insert", block_id=new_id, text=f"(({target}))"))
        links = sorted(
            (link["source_block_id"], link["target_block_id"])
            for link in block_store.document_backlinks(self.database, "big")
        )
        self.assertEqual(links, sorted([(mentioning, new_id), (new_id, target)]))
        media = Path(self._directory.name) / "assets"
        media.mkdir()
        image = media / "01J00000000000000000000099.png"
        image.write_bytes(b"png")
        block_store.register_asset(self.database, image, "hash", "image/png")
        block_store.store_asset_text(self.database, "hash", "receipt total")
        self.apply(BlockOperation(
            operation="insert", block_id="01J00000000000000000000088", type="image",
            attrs={"url": f"/media/{image.name}"},
        ))
        self.assertEqual(
            [hit["block_id"] for hit in block_store.search_blocks(self.database, "receipt")],
            ["01J00000000000000000000088"],
        )
        self.apply(BlockOperation(operation="delete", block_id=new_id))
        self.assertEqual(block_store.document_backlinks(self.database, "big"), [])
        self.assertTrue(fts_is_consistent(self.database))

    def test_fts_rows_follow_their_block_rowid_and_old_layouts_are_rebuilt(self) -> None:
        with connection_scope(self.database) as connection:
            # A database indexed before FTS rows shared their block's rowid.
            rows = connection.execute(
                "SELECT block_id, document_id, text FROM blocks_fts"
            ).fetchall()
            connection.execute("DELETE FROM blocks_fts")
            connection.executemany(
                "INSERT INTO blocks_fts(rowid, block_id, document_id, text) VALUES (?, ?, ?, ?)",
                [(100000 + n, *row) for n, row in enumerate(rows)],
            )
        self.assertFalse(fts_is_consistent(self.database))
        initialize(self.database, force=True)
        self.assertTrue(fts_is_consistent(self.database))


class ApiWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from app import assets, documents, images, main, search

        self.main = main
        self.documents = documents
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.notes = Path(self._directory.name)
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=self.notes / ".fortress.sqlite3",
            notes_path=self.notes,
            assets_path=self.notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            vlm_caption_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
            patch.object(assets, "get_settings", lambda: settings),
            patch.object(images, "get_settings", lambda: settings),
            patch.object(search, "get_settings", lambda: settings),
            patch.object(images, "extract_and_store", lambda _path: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        main._clear_response_caches()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.note = self.client.post(
            "/api/block-documents", json={"title": "Note", "body": "one\n\ntwo"}
        ).json()

    def transact(self, text: str, response: str = "document"):
        revision = self.client.get(f"/api/block-documents/{self.note['id']}").json()["revision"]
        return self.client.post(
            f"/api/block-documents/{self.note['id']}/transactions?response={response}",
            json={"base_revision": revision, "operations": [{
                "operation": "update", "block_id": self.note["children"][0]["id"],
                "text": text,
            }]},
        )

    def test_transactions_write_the_mirror_in_the_background(self) -> None:
        mirror = self.notes / f"{self.note['id']}.md"
        with patch.object(self.documents, "write_mirror") as synchronous:
            self.assertEqual(self.transact("Background").status_code, 200)
        synchronous.assert_not_called()
        self.documents.flush_mirrors()
        self.assertIn("Background", mirror.read_text(encoding="utf-8"))

    def test_transaction_response_primes_the_get_and_its_etag(self) -> None:
        response = self.transact("Primed")
        tag = response.headers["x-document-etag"]
        with patch.object(
            block_store, "document_tree_with_validator", side_effect=AssertionError("rebuilt")
        ):
            fetched = self.client.get(f"/api/block-documents/{self.note['id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers["etag"].replace("-gzip", ""), tag)
        self.assertEqual(fetched.json(), response.json())

    def test_meta_response_has_no_children(self) -> None:
        response = self.transact("Meta", response="meta")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["children"], [])
        self.assertTrue(body["can_undo"])
        full = self.client.get(f"/api/block-documents/{self.note['id']}")
        self.assertEqual(full.json()["revision"], body["revision"])
        self.assertEqual(
            full.headers["etag"].replace("-gzip", ""), response.headers["x-document-etag"]
        )
        self.assertEqual(full.json()["children"][0]["text"], "Meta")

    def test_async_routes_do_not_run_blocking_work_on_the_event_loop(self) -> None:
        import asyncio

        from app import llm
        from app.models import BlockReviewResponse

        on_loop: list[bool] = []

        def running_loop() -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False
            return True

        real_register = block_store.register_asset
        real_subtree = block_store.document_subtree

        def register(*args, **kwargs):
            on_loop.append(running_loop())
            return real_register(*args, **kwargs)

        def subtree(*args, **kwargs):
            on_loop.append(running_loop())
            return real_subtree(*args, **kwargs)

        async def review(kind, block_id, text, content):
            return BlockReviewResponse(kind=kind, summary="", block_id=block_id)

        with (
            patch.object(block_store, "register_asset", register),
            patch.object(block_store, "document_subtree", subtree),
            patch.object(llm, "review_block", review),
        ):
            uploaded = self.client.post(
                "/api/files", files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")}
            )
            reviewed = self.client.post(
                f"/api/block-documents/{self.note['id']}/blocks/"
                f"{self.note['children'][0]['id']}/review"
            )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(on_loop, [False, False])

    def test_uploads_are_streamed_to_disk_with_their_hash(self) -> None:
        import hashlib

        data = b"x" * (3 * 1024 * 1024 + 17)
        response = self.client.post(
            "/api/files", files={"file": ("big.mp3", data, "audio/mpeg")}
        )
        url = response.json()["props"]["url"]
        path = self.notes / "assets" / url.rsplit("/", 1)[1]
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
        with connection_scope(self.notes / ".fortress.sqlite3") as connection:
            row = connection.execute("SELECT content_hash, mime_type FROM assets").fetchone()
        self.assertEqual(row["content_hash"], hashlib.sha256(data).hexdigest())
        self.assertEqual(row["mime_type"], "audio/mpeg")


    def test_related_does_not_build_the_note_and_follows_edits(self) -> None:
        from app import embeddings, search

        other = self.client.post(
            "/api/block-documents",
            json={"title": "Other", "body": "kyoto temples gardens autumn\n\nnothing"},
        ).json()
        mine = self.client.post(
            "/api/block-documents",
            json={"title": "Mine", "body": "kyoto temples gardens autumn trip"},
        ).json()
        url = f"/api/notes/{mine['id']}/related"
        calls: list[int] = []

        def scores(docs, *, query=None, texts=None):
            calls.append(1)
            return {bid: 1.0 for bid, _version, text in docs if "kyoto" in text}, None

        with (
            patch.object(embeddings, "_get_model", lambda wait=False: None),
            patch.object(embeddings._cache, "failed", True),
            patch.object(search.relevance, "related", scores),
            patch.object(self.documents, "get_note", side_effect=AssertionError("built")),
            patch.object(block_store, "document_tree", side_effect=AssertionError("built")),
        ):
            first = self.client.get(url).json()
            self.assertEqual([hit["note"]["id"] for hit in first], [other["id"]])
            self.assertEqual(self.client.get(url).json(), first)
            self.assertEqual(len(calls), 1)  # the second answer came from the cache
            self.client.post(
                f"/api/block-documents/{other['id']}/transactions",
                json={"base_revision": other["revision"], "operations": [{
                    "operation": "update", "block_id": other["children"][0]["id"],
                    "text": "unrelated words entirely"}]},
            )
            self.assertEqual(self.client.get(url).json(), [])
            self.assertEqual(self.client.get("/api/notes/missing/related").status_code, 404)
        search._related_cache.clear()

    def test_concurrent_transactions_on_one_note_conflict_cleanly(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        revision = self.note["revision"]
        block = self.note["children"][0]["id"]

        def write(number: int) -> int:
            return self.client.post(
                f"/api/block-documents/{self.note['id']}/transactions",
                json={"base_revision": revision, "operations": [{
                    "operation": "update", "block_id": block, "text": f"writer {number}"}]},
            ).status_code

        with ThreadPoolExecutor(8) as pool:
            statuses = sorted(pool.map(write, range(8)))
        self.assertEqual(statuses, [200] + [409] * 7)
        tree = self.client.get(f"/api/block-documents/{self.note['id']}").json()
        self.assertEqual(tree["revision"], revision + 1)

if __name__ == "__main__":
    unittest.main()
