"""The note ETag and 304s, Cache-Control, media caching and error shapes."""

import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app import block_store, http_cache
from app.media import MediaFiles

from support import make_settings, use_settings


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        from app import main

        self.main = main
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.notes = Path(self._directory.name)
        self.db = self.notes / ".fortress.sqlite3"
        settings = make_settings(self.notes, reindex_interval_s=5.0)
        patcher = use_settings(settings)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.transact(note["id"], {
            "operation": "update", "block_id": block, "content": [{"type": "text", "text": "Edited"}],
        })
        changed = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["etag"], tag)
        self.assertEqual(changed.json()["children"][0]["text"], "Edited")

    def test_validator_follows_link_targets_and_writes_outside_transactions(self) -> None:
        target = self.create("Target", "Target paragraph")
        target_block = target["children"][0]["id"]
        source = self.create("Source", "Intro")
        self.transact(source["id"], {
            "operation": "insert", "block_id": "01J00000000000000000000001", "type": "paragraph",
            "content": [
                {"type": "text", "text": "See "}, {"type": "docLink", "id": target["id"]},
                {"type": "text", "text": " and "}, {"type": "blockRef", "id": target_block},
            ],
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
            "operation": "update", "block_id": target_block,
            "content": [{"type": "text", "text": "New target text"}],
        })
        response = self.client.get(url, headers={"If-None-Match": tag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["resolved_links"]["blocks"][target_block]["text"], "New target text"
        )
        tag = response.headers["etag"]

        # A write that bumps no revision still changes the validator.
        with block_store.connection_scope(self.db) as connection:
            connection.execute(
                "UPDATE blocks SET attrs_json = ? WHERE document_id = ?",
                ('{"text_alignment": "center"}', source["id"]),
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

    def test_metadata_changes_answer_metadata_without_building_the_tree(self) -> None:
        note = self.create("Note", "one\n\ntwo")
        url = f"/api/block-documents/{note['id']}"
        with patch.object(block_store, "document_tree", side_effect=AssertionError("built")):
            response = self.client.patch(url, json={"title": "Renamed"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["title"], body["children"]), ("Renamed", []))
        self.assertFalse(body["title_auto"])
        full = self.client.get(url)
        self.assertEqual(full.headers["etag"], response.headers["x-document-etag"])
        self.assertEqual(len(full.json()["children"]), 2)
        cleared = self.client.patch(url, json={"title": ""}).json()
        self.assertEqual((cleared["title"], cleared["title_auto"]), ("one", True))
        self.assertEqual(self.client.patch(url, json={"body": "nope"}).status_code, 422)
        self.assertEqual(
            self.client.patch("/api/block-documents/missing", json={"title": "x"}).status_code, 404
        )

    def test_transactions_answer_metadata_unless_asked_for_the_document(self) -> None:
        note = self.create("Note", "one")
        block = note["children"][0]["id"]
        url = f"/api/block-documents/{note['id']}/transactions"
        meta = self.client.post(url, json={"base_revision": note["revision"], "operations": [
            {"operation": "update", "block_id": block, "content": [{"type": "text", "text": "two"}]}]})
        self.assertEqual(meta.json()["children"], [])
        full = self.client.post(url + "?response=document", json={
            "base_revision": meta.json()["revision"],
            "operations": [{"operation": "update", "block_id": block,
                            "content": [{"type": "text", "text": "three"}]}]})
        self.assertEqual(full.json()["children"][0]["text"], "three")
        got = self.client.get(f"/api/block-documents/{note['id']}", headers={
            "If-None-Match": full.headers["x-document-etag"]})
        self.assertEqual(got.status_code, 304)

    def test_reading_never_writes_and_opening_keeps_the_etag(self) -> None:
        note = self.create("Note", "x")
        url = f"/api/block-documents/{note['id']}"
        def change_state():
            with closing(sqlite3.connect(self.db)) as connection:
                return connection.execute("SELECT epoch, seq FROM change_log").fetchone()

        state = change_state()
        first = self.client.get(url)
        self.assertNotIn("last_opened_at", first.json())
        self.assertEqual(change_state(), state)  # a GET wrote nothing
        block_store._opened_marks.clear()
        self.assertEqual(self.client.post(f"{url}/opened").status_code, 204)
        self.assertEqual(self.revalidate(url, first.headers["etag"]), 304)
        info = self.client.get(f"{url}/info").json()
        self.assertIsNotNone(info["last_opened_at"])
        self.assertEqual(self.client.post("/api/block-documents/missing/opened").status_code, 404)

    def test_opening_is_recorded_at_most_once_a_minute(self) -> None:
        note = self.create("Note", "x")
        url = f"/api/block-documents/{note['id']}"
        block_store._opened_marks.clear()
        self.client.post(f"{url}/opened")
        opened = self.client.get(f"{url}/info").json()["last_opened_at"]
        with patch.object(block_store, "utc_now", return_value="2099-01-01T00:00:00+00:00"):
            self.client.post(f"{url}/opened")
            self.assertEqual(self.client.get(f"{url}/info").json()["last_opened_at"], opened)
            later = time.monotonic() + block_store.OPEN_MARK_INTERVAL_S + 1
            with patch.object(block_store.time, "monotonic", return_value=later):
                self.client.post(f"{url}/opened")
        self.assertEqual(
            self.client.get(f"{url}/info").json()["last_opened_at"], "2099-01-01T00:00:00+00:00"
        )

    def test_opening_does_not_wait_for_a_busy_writer(self) -> None:
        note = self.create("Note", "x")
        block_store._opened_marks.clear()
        with closing(sqlite3.connect(self.db, timeout=0)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            response = self.client.get(f"/api/block-documents/{note['id']}")
            opened = self.client.post(f"/api/block-documents/{note['id']}/opened")
            elapsed = time.perf_counter() - started
            writer.rollback()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(opened.status_code, 204)  # skipped, not waited for
        self.assertLess(elapsed, 1.0)  # the busy timeout is 5 s

    def test_missing_document_is_404_json(self) -> None:
        response = self.client.get("/api/block-documents/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Block document not found"})


class WorkspaceResponseTests(ApiCase):
    def test_workspace_routes_are_plain_no_cache_json(self) -> None:
        note = self.create("Note", "x")
        self.create("Other", f"[[{note['id']}|Note]]")
        for url in [
            "/api/navigation",
            "/api/trash",
            f"/api/block-documents/{note['id']}/info",
            f"/api/block-documents/{note['id']}/backlinks",
            "/api/backups",
        ]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response.headers["cache-control"], "no-cache", url)
        self.assertEqual(len(self.client.get(f"/api/block-documents/{note['id']}/backlinks").json()), 1)
        self.assertEqual(self.client.get("/api/navigation").json(), block_store.navigation(self.db))
        for url in ["/api/block-documents/nope/info", "/api/block-documents/nope/backlinks"]:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_restore_never_reuses_an_old_document_etag(self) -> None:
        note = self.create("Note", "x")
        url = f"/api/block-documents/{note['id']}"
        backup = self.client.post("/api/backups").json()["name"]
        before = self.client.get(url).headers["etag"]
        restored = self.client.post(f"/api/backups/{backup}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.revalidate(url, before), 200)


class AskStreamTests(ApiCase):
    def test_ask_stream_is_never_cached(self) -> None:
        from app import ask

        async def stream(_question):
            for number in range(200):
                yield {"type": "sentence", "text": f"Sentence number {number} " * 5}

        with patch.object(ask, "ask_stream", stream):
            response = self.client.post(
                "/api/ask",
                json={"question": "What?"},
                headers={"Accept": "application/x-ndjson"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(len(response.text.splitlines()), 200)


class ErrorShapeTests(ApiCase):
    def test_unknown_route_and_unexpected_errors_are_json(self) -> None:
        self.assertEqual(self.client.get("/api/nothing-here").json(), {"detail": "Not Found"})
        with patch.object(block_store, "navigation", side_effect=RuntimeError("boom")):
            response = self.client.get("/api/navigation")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal server error"})

    def test_a_locked_database_is_503_json(self) -> None:
        error = sqlite3.OperationalError("database is locked")
        from app import workspace

        with patch.object(workspace, "list_trash", side_effect=error):
            response = self.client.get("/api/trash")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertIn("busy", response.json()["detail"])


class MediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.assets = Path(self._directory.name)
        app = Starlette(routes=[Mount("/media", MediaFiles(lambda: self.assets))])
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
        self.assertTrue(http_cache.if_none_match("*", tag))
        self.assertFalse(http_cache.if_none_match('"nope"', tag))
        self.assertFalse(http_cache.if_none_match(None, tag))


if __name__ == "__main__":
    unittest.main()
