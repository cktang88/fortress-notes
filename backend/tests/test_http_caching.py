"""ETags, 304s, compression, media caching, batch reads and error shapes."""

import gzip
import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app import block_store, http_cache
from app.media import MediaFiles
from app.models import BlockOperation


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

        self.main = main
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.notes = Path(self._directory.name)
        self.db = self.notes / ".fortress.sqlite3"
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=self.db,
            notes_path=self.notes,
            assets_path=self.notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
            frontend_origin="http://localhost:5173",
        )
        settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        main._clear_response_caches()
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, title: str, body: str) -> dict:
        response = self.client.post("/api/block-documents", json={"title": title, "body": body})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def transact(self, document_id: str, *operations: dict) -> dict:
        revision = block_store.document_tree(self.db, document_id)["revision"]
        response = self.client.post(
            f"/api/block-documents/{document_id}/transactions",
            json={"base_revision": revision, "operations": list(operations)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def revalidate(self, url: str, tag: str) -> int:
        return self.client.get(url, headers={"If-None-Match": tag}).status_code


class DocumentValidatorTests(ApiCase):
    def test_document_get_has_etag_and_answers_304_until_it_changes(self) -> None:
        note = self.create("Note", "First paragraph")
        url = f"/api/block-documents/{note['id']}"
        first = self.client.get(url)
        self.assertEqual(first.headers["cache-control"], "no-cache")
        tag = first.headers["etag"]
        self.assertTrue(tag.startswith('"') and tag.endswith('"'))
        again = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        self.assertEqual(again.headers["etag"], tag)
        self.assertEqual(self.revalidate(url, f"W/{tag}, \"other\""), 304)

        block = first.json()["children"][0]["id"]
        self.transact(note["id"], {"operation": "update", "block_id": block, "text": "Edited"})
        changed = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["etag"], tag)
        self.assertEqual(changed.json()["children"][0]["text"], "Edited")

    def test_validator_follows_link_targets_and_writes_outside_transactions(self) -> None:
        target = self.create("Target", "Target paragraph")
        target_block = target["children"][0]["id"]
        source = self.create("Source", "Intro")
        self.transact(source["id"], {
            "operation": "insert", "block_id": "01J00000000000000000000001",
            "text": f"See [[{target['id']}|Target]] and (({target_block}))",
        })
        url = f"/api/block-documents/{source['id']}"
        tag = self.client.get(url).headers["etag"]

        # Renaming the linked note changes resolved_links and the served label.
        self.client.patch(f"/api/block-documents/{target['id']}", json={"title": "Renamed"})
        response = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["resolved_links"]["documents"][target["id"]]["title"], "Renamed"
        )
        tag = response.headers["etag"]

        # Editing the referenced block changes its text in resolved_links.
        self.transact(target["id"], {
            "operation": "update", "block_id": target_block, "text": "New target text",
        })
        response = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["resolved_links"]["blocks"][target_block]["text"], "New target text"
        )
        tag = response.headers["etag"]

        # A write that bumps no revision (e.g. settling a source conflict as
        # "keep local" rewrites provenance only) still changes the validator.
        with block_store.connection_scope(self.db) as connection:
            connection.execute(
                "UPDATE blocks SET provenance_json = ? WHERE document_id = ?",
                ('{"source": "paste"}', source["id"]),
            )
        self.assertEqual(self.revalidate(url, tag), 200)

    def test_trashing_a_link_target_changes_the_validator(self) -> None:
        target = self.create("Target", "x")
        source = self.create("Source", f"[[{target['id']}|Target]]")
        url = f"/api/block-documents/{source['id']}"
        tag = self.client.get(url).headers["etag"]
        self.assertEqual(self.client.delete(f"/api/block-documents/{target['id']}").status_code, 204)
        response = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resolved_links"]["documents"], {})

    def test_opening_writes_last_opened_at_at_most_once_a_minute(self) -> None:
        note = self.create("Note", "x")
        url = f"/api/block-documents/{note['id']}"
        first = self.client.get(url)
        opened = first.json()["last_opened_at"]
        self.assertIsNotNone(opened)
        with patch.object(block_store, "_now", return_value="2099-01-01T00:00:00+00:00"):
            self.assertEqual(self.revalidate(url, first.headers["etag"]), 304)
            later = time.monotonic() + block_store.OPEN_MARK_INTERVAL_S + 1
            with patch.object(block_store.time, "monotonic", return_value=later):
                reopened = self.client.get(url, headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json()["last_opened_at"], "2099-01-01T00:00:00+00:00")

    def test_opening_does_not_wait_for_a_busy_writer(self) -> None:
        note = self.create("Note", "x")
        block_store._opened_marks.clear()
        with closing(sqlite3.connect(self.db, timeout=0)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            response = self.client.get(f"/api/block-documents/{note['id']}")
            elapsed = time.perf_counter() - started
            writer.rollback()
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 1.0)  # the busy timeout is 5 s

    def test_missing_document_is_404_json(self) -> None:
        response = self.client.get("/api/block-documents/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Block document not found"})


class WorkspaceValidatorTests(ApiCase):
    def test_navigation_trash_info_backlinks_and_backups_revalidate(self) -> None:
        note = self.create("Note", "x")
        other = self.create("Other", f"[[{note['id']}|Note]]")
        urls = [
            "/api/navigation",
            "/api/trash",
            f"/api/block-documents/{note['id']}/info",
            f"/api/block-documents/{note['id']}/backlinks",
            "/api/backups",
        ]
        tags = {}
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response.headers["cache-control"], "no-cache", url)
            tags[url] = response.headers["etag"]
            self.assertEqual(self.revalidate(url, tags[url]), 304, url)
        self.assertEqual(len(self.client.get(urls[3]).json()), 1)

        # Editing the linking note changes backlinks/info/navigation.
        self.transact(other["id"], {
            "operation": "insert", "block_id": "01J00000000000000000000002", "text": "more",
        })
        for url in urls[:1] + urls[2:4]:
            self.assertEqual(self.revalidate(url, tags[url]), 200, url)
        self.client.delete(f"/api/block-documents/{other['id']}")
        self.assertEqual(self.revalidate("/api/trash", tags["/api/trash"]), 200)
        self.assertEqual(self.client.post("/api/backups").status_code, 201)
        self.assertEqual(self.revalidate("/api/backups", tags["/api/backups"]), 200)

    def test_cached_navigation_matches_a_fresh_render(self) -> None:
        self.create("Note", "x")
        first = self.client.get("/api/navigation").json()
        second = self.client.get("/api/navigation").json()
        self.assertEqual(first, second)
        self.assertEqual(first, block_store.navigation(self.db))
        self.client.post("/api/folders", json={"name": "Folder"})
        self.assertEqual(self.client.get("/api/navigation").json(), block_store.navigation(self.db))

    def test_restore_never_reuses_an_old_validator(self) -> None:
        self.create("Note", "x")
        backup = self.client.post("/api/backups").json()["name"]
        before = self.client.get("/api/navigation").headers["etag"]
        restored = self.client.post(f"/api/backups/{backup}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.revalidate("/api/navigation", before), 200)


class CompressionTests(ApiCase):
    def test_large_json_is_gzipped_with_a_gzip_etag(self) -> None:
        for number in range(30):
            self.create(f"Note {number}", "words " * 40)
        response = self.client.get("/api/navigation", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.headers["content-encoding"], "gzip")
        tag = response.headers["etag"]
        self.assertTrue(tag.endswith('-gzip"'))
        self.assertIn("Accept-Encoding", response.headers["vary"])
        self.assertEqual(
            self.client.get(
                "/api/navigation", headers={"If-None-Match": tag, "Accept-Encoding": "gzip"}
            ).status_code,
            304,
        )
        plain = self.client.get("/api/navigation", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.headers["etag"] + "", tag.replace("-gzip", ""))

    def test_small_json_is_not_gzipped(self) -> None:
        response = self.client.get("/api/health", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", response.headers)

    def test_ask_stream_is_never_gzipped(self) -> None:
        from app import ask

        async def stream(_question):
            for number in range(200):
                yield {"type": "sentence", "text": f"Sentence number {number} " * 5}

        with patch.object(ask, "ask_stream", stream):
            response = self.client.post(
                "/api/ask",
                json={"question": "What?"},
                headers={"Accept": "application/x-ndjson", "Accept-Encoding": "gzip"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(len(response.text.splitlines()), 200)


class ErrorShapeTests(ApiCase):
    def test_unknown_route_and_unexpected_errors_are_json(self) -> None:
        self.assertEqual(self.client.get("/api/nothing-here").json(), {"detail": "Not Found"})
        with patch.object(block_store, "navigation", side_effect=RuntimeError("boom")):
            self.main._clear_response_caches()
            response = self.client.get("/api/navigation")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal server error"})

    def test_a_locked_database_is_503_json(self) -> None:
        error = sqlite3.OperationalError("database is locked")
        with patch.object(block_store, "list_documents", side_effect=error):
            response = self.client.get("/api/block-documents")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertIn("busy", response.json()["detail"])


class MediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.assets = Path(self._directory.name)
        app = Starlette(routes=[Mount("/media", MediaFiles(directory=self.assets))])
        self.client = TestClient(app)

    def test_content_addressed_media_is_immutable_with_hash_etag_and_ranges(self) -> None:
        data = bytes(range(256)) * 4096
        digest = hashlib.sha256(data).hexdigest()
        (self.assets / f"{digest}.m4a").write_bytes(data)
        url = f"/media/{digest}.m4a"
        response = self.client.get(url)
        self.assertEqual(response.headers["content-type"], "audio/mp4")
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(response.headers["etag"], f'"{digest}"')
        self.assertIn("last-modified", response.headers)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(
            self.client.get(url, headers={"If-None-Match": f'"{digest}"'}).status_code, 304
        )
        part = self.client.get(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, data[100:200])
        self.assertEqual(part.headers["content-range"], f"bytes 100-199/{len(data)}")

    def test_upload_names_are_immutable_but_text_sidecars_revalidate(self) -> None:
        (self.assets / "01J00000000000000000000000.webm").write_bytes(b"x")
        (self.assets / "01J00000000000000000000000.txt").write_text("OCR", encoding="utf-8")
        upload = self.client.get("/media/01J00000000000000000000000.webm")
        self.assertEqual(upload.headers["content-type"], "audio/webm")
        self.assertIn("immutable", upload.headers["cache-control"])
        sidecar = self.client.get("/media/01J00000000000000000000000.txt")
        self.assertEqual(sidecar.headers["cache-control"], "no-cache")
        tag = sidecar.headers["etag"]
        self.assertEqual(
            self.client.get(
                "/media/01J00000000000000000000000.txt", headers={"If-None-Match": tag}
            ).status_code,
            304,
        )


class ValidatorHelperTests(unittest.TestCase):
    def test_if_none_match_parsing(self) -> None:
        tag = http_cache.etag("x", 1)
        self.assertTrue(http_cache.if_none_match(tag, tag))
        self.assertTrue(http_cache.if_none_match(f'"a", W/{tag}', tag))
        self.assertTrue(http_cache.if_none_match(tag[:-1] + '-gzip"', tag))
        self.assertTrue(http_cache.if_none_match("*", tag))
        self.assertFalse(http_cache.if_none_match('"nope"', tag))
        self.assertFalse(http_cache.if_none_match(None, tag))
        self.assertEqual(gzip.decompress(gzip.compress(b"x")), b"x")


if __name__ == "__main__":
    unittest.main()
