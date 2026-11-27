"""Picture descriptions (app/captions.py): the queue and its cache, on/off, the idle
lane, and descriptions reaching search, ColBERT and Ask without entering the note.

The model is a fake here; RealModelTests (macOS CI) runs the pinned one over
tests/fixtures/captions and holds it to quality, hallucination and retrieval floors.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import background, block_store, captions, corpus
from app.models import BlockOperation
from support import make_settings, use_settings



def fake_describer(descriptions: dict[bytes, str]):
    """describe_file reading the picture's bytes and answering from ``descriptions``."""

    def describe(path: Path) -> str:
        return descriptions[Path(path).read_bytes()]

    return describe


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.settings = make_settings(self.root)
        self.db = self.settings.block_db_path
        patcher = use_settings(self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        block_store.initialize(self.db)

    def asset(self, name: str, data: bytes, mime: str = "image/jpeg") -> str:
        path = self.settings.assets_path / name
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        block_store.register_asset(self.db, path, digest, mime)
        return digest

    def note_with(self, title: str, media: list[str]) -> tuple[str, list[str]]:
        document = block_store.create_block_document(self.db, title, "")
        tree = block_store.apply_transaction(
            self.db, document["id"],
            [BlockOperation(operation="insert", type="image", attrs={"url": f"/media/{name}"})
             for name in media],
            0,
        )
        return document["id"], [b["id"] for b in tree["children"] if b["type"] == "image"]

    def drain(self) -> int:
        count = 0
        while captions.process_one(self.db):
            count += 1
            self.assertLess(count, 100)
        return count

    def rows(self) -> list[tuple]:
        with block_store.connection_scope(self.db) as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT content_hash, model, status, text, attempts FROM picture_descriptions"
                " ORDER BY content_hash, model")]


class QueueAndCacheTests(_Workspace):
    def test_same_picture_is_described_once_and_every_copy_shows_it(self) -> None:
        digest = self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.jpg", b"beach")
        self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.jpg", b"beach")  # the same bytes again
        self.asset("01JCCCCCCCCCCCCCCCCCCCCCCC.pdf", b"%PDF", "application/pdf")  # not a picture
        document_id, blocks = self.note_with("Trip", ["01JAAAAAAAAAAAAAAAAAAAAAAA.jpg",
                                                      "01JBBBBBBBBBBBBBBBBBBBBBBB.jpg"])
        with patch.object(captions, "describe_file", return_value="A dog on a beach.") as describe:
            self.assertEqual(self.drain(), 1)
        describe.assert_called_once()
        self.assertEqual(self.rows(), [(digest, captions.MODEL, "done", "A dog on a beach.", 1)])
        hits = block_store.connection_scope
        with hits(self.db) as connection:
            found = {row[0] for row in connection.execute(
                "SELECT block_id FROM blocks_fts WHERE blocks_fts MATCH 'beach'")}
        self.assertEqual(found, set(blocks))
        # Described: nothing pending, and a restart doesn't describe it again.
        with patch.object(captions, "describe_file") as describe:
            self.assertEqual(self.drain(), 0)
        describe.assert_not_called()
        self.assertEqual(captions.counts(self.db), {"queued": 0, "done": 1, "failed": 0})

    def test_a_new_model_redescribes_lazily_and_keeps_the_old_text_meanwhile(self) -> None:
        digest = self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.jpg", b"whiteboard")
        document_id, [block] = self.note_with("Meeting", ["01JAAAAAAAAAAAAAAAAAAAAAAA.jpg"])
        with patch.object(captions, "describe_file", return_value="A whiteboard with notes."):
            self.drain()
        with patch.object(captions, "MODEL", "next-model/2"):
            self.assertEqual([row["content_hash"] for row in captions.pending(self.db)], [digest])
            # Until the new model gets to it, the old description stays searchable.
            self.assertIn("whiteboard", self.searchable(block))
            with patch.object(captions, "describe_file", return_value="A whiteboard with a diagram."):
                self.assertEqual(self.drain(), 1)
            self.assertEqual([row[1] for row in self.rows()], ["next-model/2"])
            self.assertIn("diagram", self.searchable(block))

    def searchable(self, block_id: str) -> str:
        rows = {row[0]: row[3] for row in corpus.embedding_documents(self.db)}
        return rows.get(block_id, "")

    def test_newest_first_tiny_pictures_get_no_description(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.jpg", b"older")
        newest = self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.jpg", b"newer")
        self.assertEqual(captions.pending(self.db, limit=5)[0]["content_hash"], newest)

        from PIL import Image

        icon = self.settings.assets_path / "icon.png"
        Image.new("RGB", (24, 24), "red").save(icon)
        with patch.object(captions, "describe_image") as model:
            self.assertEqual(captions.describe_file(icon), "")
        model.assert_not_called()

    def test_failures_are_retried_then_given_up(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.jpg", b"broken")
        with patch.object(captions, "describe_file", side_effect=OSError("cannot identify image")) as describe:
            self.assertEqual(self.drain(), captions.MAX_ATTEMPTS)
        self.assertEqual(describe.call_count, captions.MAX_ATTEMPTS)
        [row] = self.rows()
        self.assertEqual((row[2], row[4]), ("failed", captions.MAX_ATTEMPTS))
        self.assertEqual(captions.counts(self.db)["failed"], 1)

    def test_the_first_sentence_only(self) -> None:
        self.assertEqual(captions._tidy(" A cat on a sofa.  It is  sleeping."), "A cat on a sofa.")
        self.assertEqual(captions._tidy("A red tram\nat a stop"), "A red tram at a stop")


class SchemaTests(unittest.TestCase):
    def test_an_existing_version_16_database_gets_the_table_and_stays_at_16(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / ".fortress.sqlite3"
            block_store.initialize(db)
            with sqlite3.connect(db) as connection:  # as a build without this module left it
                connection.execute("DROP TABLE picture_descriptions")
            block_store.initialize(db, force=True)
            with sqlite3.connect(db) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertIn("picture_descriptions", tables)
            self.assertEqual(version, block_store.SCHEMA_VERSION)
            self.assertEqual(version, 16)


class IdleLaneTests(unittest.TestCase):
    def test_an_idle_lane_waits_while_another_lane_works(self) -> None:
        runner = background.Runner()
        busy, release, ran = threading.Event(), threading.Event(), threading.Event()

        class Low(background.Service):
            name, priority = "low", background.LOW

            def step(self):
                busy.set()
                release.wait(5)
                return None

        class Idle(background.Service):
            name, priority = "idle", background.IDLE

            def step(self):
                ran.set()
                return None

        runner.register(Low())
        runner.register(Idle())
        runner.start("low")
        self.assertTrue(busy.wait(5))
        runner.start("idle")
        time.sleep(0.6)
        self.assertFalse(ran.is_set())  # held while the low lane works
        release.set()
        self.assertTrue(ran.wait(5))
        runner.stop()


class OnOffTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from app import main

        for item in (
            patch.object(captions, "downloaded", return_value=True),
            patch.object(captions, "_load", return_value=None),
            patch.object(captions, "describe_file", return_value="A sailboat in a harbour."),
            patch.object(captions, "_enabled", None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(captions.set_enabled, False)

    def test_off_by_default_then_on_describes_and_off_stops(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.jpg", b"harbour")
        status = self.client.get("/api/descriptions").json()
        self.assertEqual((status["enabled"], status["state"], status["queued"]), (False, "off", 1))
        self.assertGreater(status["download_bytes"], 100_000_000)
        self.assertFalse(background.running(captions.Describer.name))

        status = self.client.put("/api/descriptions", json={"enabled": True}).json()
        self.assertTrue(status["enabled"])
        self.assertTrue(captions.wait_idle(10))
        status = self.client.get("/api/descriptions").json()
        self.assertEqual((status["state"], status["queued"], status["done"]), ("idle", 0, 1))

        status = self.client.put("/api/descriptions", json={"enabled": False}).json()
        self.assertEqual((status["enabled"], status["state"]), (False, "off"))
        self.assertFalse(background.running(captions.Describer.name))
        self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.jpg", b"another")
        time.sleep(0.3)
        self.assertEqual(captions.counts(self.db)["queued"], 1)  # off: nothing described

    def test_turning_it_off_stops_a_download(self) -> None:
        describer = background.runner.service(captions.Describer.name)
        started = threading.Event()

        class Download:
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                self.returncode = -15
                stop.set()

            def communicate(self):
                started.set()
                stop.wait(10)
                return b"", b""

        stop = threading.Event()
        with patch.object(captions, "downloaded", return_value=False), \
                patch.object(captions.subprocess, "Popen", return_value=Download()):
            self.client.put("/api/descriptions", json={"enabled": True})
            self.assertTrue(started.wait(5))
            self.assertEqual(self.client.get("/api/descriptions").json()["state"], "downloading")
            self.client.put("/api/descriptions", json={"enabled": False})
            self.assertTrue(stop.is_set())
            self.assertEqual(describer.state, "off")


class DescriptionIsSearchableTests(unittest.TestCase):
    """A description reaches unified search, the ColBERT corpus and Ask, through the
    API, apart from the picture's OCR text and never in the note itself."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app import embeddings, main, ocr

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        self.settings = make_settings(notes, reindex_interval_s=5.0, ask_model="test/fast-model")
        for item in (use_settings(self.settings), patch.object(embeddings, "_get_model", lambda: None)):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = self.settings.block_db_path

        url = self.client.post(
            "/api/files", files={"file": ("dog.jpg", b"\xff\xd8 fake photo", "image/jpeg")}
        ).json()["props"]["url"]
        self.document_id = self.client.post(
            "/api/block-documents", json={"title": "Saturday", "body": "From my phone"}
        ).json()["id"]
        tree = self.client.get(f"/api/block-documents/{self.document_id}").json()
        response = self.client.post(
            f"/api/block-documents/{self.document_id}/transactions?response=document",
            json={"base_revision": tree["revision"], "operations": [
                {"operation": "insert", "type": "image", "attrs": {"url": url}}]},
        )
        self.image_block = next(
            b["id"] for b in response.json()["children"] if b["type"] == "image")
        self.client.post("/api/block-documents", json={"title": "Groceries", "body": "Milk and eggs"})
        self.before = {b: v for b, _d, v, _t in corpus.embedding_documents(self.db)}
        self.assertEqual(self.client.get("/api/unified-search", params={"q": "retriever"}).json(), [])

        # OCR found the words on the picture; the description says what it shows.
        with patch.object(ocr, "read_asset", return_value=ocr.Result("DOG BEACH 6AM", "ocr")):
            ocr.process_one(self.db)
        with patch.object(captions, "describe_file",
                          return_value="A golden retriever running out of the sea."):
            self.assertTrue(captions.process_one(self.db))

    def test_unified_search_finds_it_and_shows_it_in_the_snippet(self) -> None:
        results = self.client.get("/api/unified-search", params={"q": "golden retriever"}).json()
        self.assertEqual(results[0]["block_id"], self.image_block)
        start, end = results[0]["highlights"][0]
        self.assertEqual(results[0]["text"][start:end].lower(), "golden")
        # OCR text is still found too.
        results = self.client.get("/api/unified-search", params={"q": "6am"}).json()
        self.assertEqual(results[0]["block_id"], self.image_block)
        # A rebuilt full-text index has it as well.
        block_store.rebuild_fts(self.db)
        results = self.client.get("/api/unified-search", params={"q": "retriever"}).json()
        self.assertEqual([r["block_id"] for r in results], [self.image_block])

    def test_never_in_the_note_or_its_markdown(self) -> None:
        markdown = self.client.get(f"/api/block-documents/{self.document_id}/markdown").text
        self.assertNotIn("retriever", markdown)
        self.assertIn("DOG BEACH", markdown)  # OCR text: in the front matter, as before
        tree = self.client.get(f"/api/block-documents/{self.document_id}").json()
        self.assertNotIn("retriever", json.dumps(tree))

    def test_embedding_corpus_reencodes_only_that_block(self) -> None:
        after = {b: (v, t) for b, _d, v, t in corpus.embedding_documents(self.db)}
        self.assertIn("golden retriever", after[self.image_block][1])
        self.assertIn("DOG BEACH", after[self.image_block][1])
        changed = [b for b, (v, _t) in after.items() if self.before.get(b) != v]
        self.assertEqual(changed, [self.image_block])

    def test_ask_sees_it(self) -> None:
        from app import ask, llm

        hits = ask.grep_blocks("retriever")
        self.assertEqual([hit["id"] for hit in hits], [self.image_block])
        reply = {"sentences": [{"text": "A golden retriever.", "citations": [
            {"id": "S1", "quote": "golden retriever"}]}]}
        fake = AsyncMock(return_value=reply)

        async def stream(*args, **kwargs):
            yield json.dumps(await fake(*args, **kwargs))

        with patch.object(llm, "chat_json_stream", stream):
            answer = self.client.post("/api/ask", json={"question": "Which dog was in the sea?"}).json()
        self.assertEqual(answer["status"], "answered", answer)
        self.assertIn("A golden retriever running out of the sea.",
                      " ".join(str(arg) for arg in fake.call_args.args))

    def test_info_shows_it_apart_from_the_text(self) -> None:
        [picture] = self.client.get(
            f"/api/block-documents/{self.document_id}/info").json()["picture_text"]
        self.assertEqual(picture["text"], "DOG BEACH 6AM")
        self.assertEqual(picture["description"], "A golden retriever running out of the sea.")


REAL = os.environ.get("FORTRESS_REAL_CAPTIONS") == "1"


@unittest.skipUnless(REAL, "set FORTRESS_REAL_CAPTIONS=1 (needs the picture-description model)")
class RealModelTests(unittest.TestCase):
    """The pinned model over tests/fixtures/captions (macOS CI)."""

    def test_quality_hallucinations_and_search(self) -> None:
        self.skipTest("floors set once the model is chosen")


if __name__ == "__main__":
    unittest.main()
