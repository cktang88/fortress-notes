import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import block_store, link_checks


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = False
        self.closed = False

    async def __aiter__(self):
        self.read = True
        yield b"large response body"

    async def aclose(self) -> None:
        self.closed = True


class LinkCheckTests(unittest.TestCase):
    def test_checks_old_blocks_deduplicates_urls_and_keeps_tree_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            block_store.create_document(
                database,
                "old-note",
                "Old note",
                "rough",
                [],
                "https://good.example/page\n\nhttps://good.example/page\n\nhttps://bad.example/page\n\nlinked text",
                old,
                old,
            )
            before = block_store.document_tree(database, "old-note")
            assert before is not None
            blocks = before["children"]
            href_block = blocks[3]
            with block_store.connection_scope(database) as connection:
                connection.execute(
                    "UPDATE blocks SET text = ?, content_json = ? WHERE id = ?",
                    (
                        "See [Wikipedia](https://example.test/wiki/Foo_(bar))",
                        json.dumps(
                            {
                                "blocknote": [
                                    {
                                        "type": "text",
                                        "href": "https://example.test/wiki/Foo_(bar)",
                                    }
                                ]
                            }
                        ),
                        href_block["id"],
                    ),
                )
            before = block_store.document_tree(database, "old-note")
            assert before is not None
            calls: list[str] = []
            streams: list[TrackingStream] = []

            def respond(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                status = 404 if request.url.host == "bad.example" else 200
                stream = TrackingStream()
                streams.append(stream)
                return httpx.Response(status, stream=stream, request=request)

            transport = httpx.MockTransport(respond)
            result = self._run_check(database, "old-note", transport)

            self.assertEqual(
                set(calls),
                {
                    "https://example.test/wiki/Foo_(bar)",
                    "https://good.example/page",
                    "https://bad.example/page",
                },
            )
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(stream.closed for stream in streams))
            self.assertTrue(all(not stream.read for stream in streams))
            by_url = {link["url"]: link for link in result["links"]}
            self.assertEqual(
                by_url["https://example.test/wiki/Foo_(bar)"]["status"], "ok"
            )
            self.assertEqual(by_url["https://bad.example/page"]["status"], "404")
            self.assertEqual(
                by_url["https://good.example/page"]["block_ids"],
                [block["id"] for block in before["children"][:2]],
            )
            self.assertIsInstance(
                by_url["https://example.test/wiki/Foo_(bar)"]["checked_at"], str
            )

            block_store.initialize(database)
            second_calls: list[str] = []
            def retry_failed(request: httpx.Request) -> httpx.Response:
                second_calls.append(str(request.url))
                status = 404 if request.url.host == "bad.example" else 500
                return httpx.Response(status, request=request)

            cached = self._run_check(
                database, "old-note", httpx.MockTransport(retry_failed)
            )
            self.assertEqual(second_calls, ["https://bad.example/page"])
            self.assertEqual(
                {link["url"]: link["status"] for link in cached["links"]},
                {link["url"]: link["status"] for link in result["links"]},
            )

            after = block_store.document_tree(database, "old-note")
            assert after is not None
            self.assertEqual(after["revision"], before["revision"])
            self.assertEqual(
                [block["updated_at"] for block in after["children"]],
                [block["updated_at"] for block in before["children"]],
            )

    def test_skips_recent_blocks_retries_failures_and_reuses_recent_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            now = datetime.now(timezone.utc)
            old = (now - timedelta(days=31)).isoformat()
            recent = (now - timedelta(days=29)).isoformat()
            block_store.create_document(
                database,
                "mixed-note",
                "Mixed note",
                "rough",
                [],
                "https://old.example/page\n\nhttps://recent.example/page",
                old,
                old,
            )
            tree = block_store.document_tree(database, "mixed-note")
            assert tree is not None
            with block_store.connection_scope(database) as connection:
                connection.execute(
                    "UPDATE blocks SET updated_at = ? WHERE id = ?",
                    (recent, tree["children"][1]["id"]),
                )
            calls: list[str] = []

            def unavailable(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                return httpx.Response(503, request=request)

            first = self._run_check(
                database, "mixed-note", httpx.MockTransport(unavailable)
            )
            self.assertEqual(calls, ["https://old.example/page"])
            self.assertEqual(
                [(item["url"], item["status"]) for item in first["links"]],
                [("https://old.example/page", "503")],
            )

            def restored(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                return httpx.Response(200, request=request)

            second = self._run_check(
                database, "mixed-note", httpx.MockTransport(restored)
            )
            self.assertEqual(calls, ["https://old.example/page"] * 2)
            self.assertEqual(second["links"][0]["status"], "ok")

            third = self._run_check(
                database,
                "mixed-note",
                httpx.MockTransport(
                    lambda request: self.fail(f"unexpected request: {request.url}")
                ),
            )
            self.assertEqual(third, second)

    def test_removed_links_are_not_returned_from_persistent_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            block_store.create_document(
                database,
                "changed-note",
                "Changed note",
                "rough",
                [],
                "https://removed.example/page",
                old,
                old,
            )
            self._run_check(
                database,
                "changed-note",
                httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
            )
            tree = block_store.document_tree(database, "changed-note")
            assert tree is not None
            with block_store.connection_scope(database) as connection:
                connection.execute(
                    "UPDATE blocks SET text = ?, content_json = '{}' WHERE id = ?",
                    ("No links remain", tree["children"][0]["id"]),
                )
            result = self._run_check(
                database,
                "changed-note",
                httpx.MockTransport(
                    lambda request: self.fail(f"unexpected request: {request.url}")
                ),
            )
            self.assertEqual(result, {"links": []})

    def test_missing_document_endpoint_returns_404(self) -> None:
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                block_db_enabled=True,
                block_db_import_on_startup=False,
                block_db_path=root / ".fortress.sqlite3",
                notes_path=root,
                assets_path=root / "assets",
                embeddings_enabled=False,
                vision_enabled=False,
                reindex_interval_s=5.0,
            )
            with patch.object(main, "settings", settings), TestClient(main.app) as client:
                response = client.post("/api/block-documents/missing/link-checks")
            self.assertEqual(response.status_code, 404)

    @staticmethod
    def _run_check(database: Path, document_id: str, transport: httpx.AsyncBaseTransport):
        import asyncio

        return asyncio.run(
            link_checks.check_document_links(
                database, document_id, transport=transport
            )
        )


if __name__ == "__main__":
    unittest.main()
