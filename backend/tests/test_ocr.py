"""OCR queue, cache, PDF text layer, and OCR text reaching search, ColBERT and Ask."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import block_store, ocr
from app.models import BlockOperation

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def text_pdf(lines_by_page: list[list[str]]) -> bytes:
    """A minimal PDF whose pages carry a real text layer (Helvetica)."""

    objects: list[bytes] = []
    page_ids = []
    font_id = 3 + 2 * len(lines_by_page)
    for index, lines in enumerate(lines_by_page):
        stream = b"BT /F1 14 Tf 72 720 Td 18 TL " + b" ".join(
            b"(" + line.encode("latin-1") + b") Tj T*" for line in lines
        ) + b" ET"
        page_id, content_id = 3 + 2 * index, 4 + 2 * index
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    body = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(),
        *objects,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(body, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(body) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(body) + 1, xref))
    return out.getvalue()


def text_image(lines: list[str], size: int = 40):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(FONT, size) if Path(FONT).exists() else ImageFont.load_default(size)
    image = Image.new("RGB", (60 + size * 18, 60 + int(size * 1.6) * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((30, 30 + int(size * 1.6) * row), line, font=font, fill="black")
    return image


class _Workspace(unittest.TestCase):
    """A temporary database with assets registered the way uploads register them."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.db = self.root / ".fortress.sqlite3"
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.settings = SimpleNamespace(
            block_db_path=self.db, notes_path=self.root, assets_path=self.assets, ocr_threads=2,
            vision_enabled=True,
        )
        for item in (
            patch.object(ocr, "get_settings", lambda: self.settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        block_store.initialize(self.db)

    def asset(self, name: str, data: bytes, mime: str) -> tuple[Path, str]:
        path = self.assets / name
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        block_store.register_asset(self.db, path, digest, mime)
        return path, digest

    def note_with(self, title: str, media: list[str], alt: str = "") -> tuple[str, list[str]]:
        document = block_store.create_block_document(self.db, title, "rough", "")
        tree = block_store.apply_transaction(
            self.db,
            document["id"],
            [
                BlockOperation(
                    operation="insert",
                    type="image",
                    attrs={"url": f"/media/{name}", **({"alt": alt} if alt else {})},
                )
                for name in media
            ],
            0,
        )
        return document["id"], [b["id"] for b in tree["children"] if b["type"] == "image"]

    def drain(self) -> int:
        count = 0
        while ocr.process_one(self.db):
            count += 1
            self.assertLess(count, 100)
        return count

    def asset_texts(self) -> dict[str, str]:
        with block_store.connection_scope(self.db) as connection:
            return dict(connection.execute("SELECT media_name, text FROM assets").fetchall())


class QueueAndCacheTests(_Workspace):
    def test_same_bytes_are_read_once_and_every_copy_gets_the_text(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"same picture", "image/png")
        self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.jpg", b"same picture", "image/jpeg")
        self.asset("01JCCCCCCCCCCCCCCCCCCCCCCC.mp3", b"audio", "audio/mpeg")
        with patch.object(ocr, "read_asset", return_value=ocr.Result("Quarterly invoice", "ocr")) as read:
            self.assertEqual(self.drain(), 1)
        read.assert_called_once()
        texts = self.asset_texts()
        self.assertEqual(texts["01JAAAAAAAAAAAAAAAAAAAAAAA.png"], "Quarterly invoice")
        self.assertEqual(texts["01JBBBBBBBBBBBBBBBBBBBBBBB.jpg"], "Quarterly invoice")
        self.assertEqual(texts["01JCCCCCCCCCCCCCCCCCCCCCCC.mp3"], "")
        self.assertEqual(ocr.status_counts(self.db)["done"], 1)
        self.assertEqual(ocr.status_counts(self.db)["queued"], 0)

        # A later upload of the same bytes is a cache hit: no work at all.
        self.asset("01JDDDDDDDDDDDDDDDDDDDDDDD.png", b"same picture", "image/png")
        with patch.object(ocr, "read_asset") as read:
            self.assertEqual(self.drain(), 0)
        read.assert_not_called()

    def test_a_new_model_rereads_lazily_and_keeps_the_old_text_meanwhile(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"picture", "image/png")
        with patch.object(ocr, "read_asset", return_value=ocr.Result("old text", "ocr")):
            self.drain()
        with patch.object(ocr, "MODEL", "next-model/1"):
            self.assertEqual(len(ocr.pending(self.db)), 1)
            self.assertEqual(self.asset_texts()["01JAAAAAAAAAAAAAAAAAAAAAAA.png"], "old text")
            with patch.object(ocr, "read_asset", return_value=ocr.Result("new text", "ocr")):
                self.assertEqual(self.drain(), 1)
        self.assertEqual(self.asset_texts()["01JAAAAAAAAAAAAAAAAAAAAAAA.png"], "new text")
        with block_store.connection_scope(self.db) as connection:
            models = [row[0] for row in connection.execute("SELECT model FROM ocr_results")]
        self.assertEqual(models, ["next-model/1"])

    def test_failures_are_retried_then_given_up_and_survive_a_restart(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"broken", "image/png")
        with patch.object(ocr, "read_asset", side_effect=OSError("cannot identify image")) as read:
            self.drain()
        self.assertEqual(read.call_count, ocr.MAX_ATTEMPTS)
        self.assertEqual(ocr.pending(self.db), [])
        counts = ocr.status_counts(self.db)
        self.assertEqual((counts["failed"], counts["queued"]), (1, 0))

        # A process that died mid-read leaves "running": the next start retries it.
        _path, digest = self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"crash", "image/png")
        ocr._mark(self.db, digest, status="running", attempts=1)
        self.assertEqual([row["content_hash"] for row in ocr.pending(self.db)], [digest])

    def test_newest_first_and_pictures_with_onenote_text_last(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"older", "image/png")
        self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"has alt", "image/png")
        self.note_with("Imported", ["01JBBBBBBBBBBBBBBBBBBBBBBB.png"], alt="OneNote OCR text")
        _path, newest = self.asset("01JCCCCCCCCCCCCCCCCCCCCCCC.png", b"newest", "image/png")
        order = [row["content_hash"] for row in ocr.pending(self.db, limit=10)]
        self.assertEqual(order[0], newest)
        self.assertEqual(order[-1], hashlib.sha256(b"has alt").hexdigest())

    def test_missing_file_fails_without_stopping_the_queue(self) -> None:
        path, _digest = self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"gone", "image/png")
        path.unlink()
        self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"fine", "image/png")
        with patch.object(ocr, "read_asset", return_value=ocr.Result("fine text", "ocr")):
            self.drain()
        self.assertEqual(self.asset_texts()["01JBBBBBBBBBBBBBBBBBBBBBBB.png"], "fine text")
        self.assertEqual(ocr.status_counts(self.db)["failed"], 1)

    def test_background_worker_drains_the_queue(self) -> None:
        self.asset("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"one", "image/png")
        with patch.object(ocr, "read_asset", return_value=ocr.Result("worker text", "ocr")):
            ocr.start(self.db)
            self.addCleanup(ocr.stop)
            self.assertTrue(ocr.wait_idle(10))
            self.asset("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"two", "image/png")
            ocr.wake()
            self.assertTrue(ocr.wait_idle(10))
        self.assertEqual(set(self.asset_texts().values()), {"worker text"})


class PdfTests(_Workspace):
    def test_text_layer_is_used_and_only_image_pages_are_ocrd(self) -> None:
        from PIL import Image

        layered = self.root / "layered.pdf"
        layered.write_bytes(text_pdf([["Lease agreement with Aurora Holdings"], ["Page two mentions the zeppelin clause."]]))
        with patch.object(ocr, "ocr_image") as ocr_image:
            result = ocr.read_asset(layered, "application/pdf")
        ocr_image.assert_not_called()
        self.assertEqual(result.method, "pdf-text")
        self.assertEqual(result.pages, 2)
        self.assertIn("Aurora Holdings", result.text)
        self.assertIn("zeppelin clause", result.text)

        scanned = self.root / "scanned.pdf"
        Image.new("RGB", (850, 1100), "white").save(scanned, "PDF", resolution=100)
        with patch.object(ocr, "ocr_image", return_value="SCANNED INVOICE 2291") as ocr_image:
            result = ocr.read_asset(scanned, "application/pdf")
        ocr_image.assert_called_once()
        self.assertEqual((result.method, result.text), ("pdf-text+ocr", "SCANNED INVOICE 2291"))


class RealModelTests(_Workspace):
    """The bundled PP-OCRv6 models on real pictures (a few seconds on a laptop CPU)."""

    def test_reads_a_screenshot_a_transparent_png_and_a_scanned_pdf(self) -> None:
        from PIL import Image

        image = text_image(["Invoice 48213 Northwind", "Total due 12,480.50"])
        self.assertIn("Northwind", ocr.ocr_image(image))

        transparent = Image.new("RGBA", image.size, (0, 0, 0, 0))
        transparent.putalpha(Image.eval(image.convert("L"), lambda v: 255 - v))
        path = self.root / "clip.png"
        transparent.save(path)  # black text on a fully transparent background
        self.assertIn("Northwind", ocr.read_asset(path, "image/png").text)

        scanned = self.root / "scan.pdf"
        text_image(["Bill to Cobalt Logistics"], 30).save(scanned, "PDF", resolution=72)
        result = ocr.read_asset(scanned, "application/pdf")
        self.assertEqual(result.method, "pdf-text+ocr")
        self.assertIn("Cobalt", result.text)


class OcrTextIsSearchableTests(unittest.TestCase):
    """OCR'd words reach unified search, the ColBERT index and Ask, via the API."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app import ask, documents, embeddings, main, search

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        self.settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,  # the test drives the queue itself
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
            ask_model="test/fast-model",
            ocr_threads=2,
        )
        self.settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", self.settings),
            patch.object(documents, "get_settings", lambda: self.settings),
            patch.object(search, "get_settings", lambda: self.settings),
            patch.object(ask, "get_settings", lambda: self.settings),
            patch.object(ocr, "get_settings", lambda: self.settings),
            patch("app.assets.get_settings", lambda: self.settings),
            patch("app.images.get_settings", lambda: self.settings),
            patch.object(embeddings, "_get_model", lambda: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = self.settings.block_db_path

        # A pasted screenshot in a note whose own words say nothing about it.
        upload = self.client.post(
            "/api/files", files={"file": ("shot.png", b"\x89PNG fake screenshot", "image/png")}
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.url = upload.json()["props"]["url"]
        self.document_id = self.client.post(
            "/api/block-documents", json={"title": "Screenshots", "body": "Pasted from my phone"}
        ).json()["id"]
        tree = self.client.get(f"/api/block-documents/{self.document_id}").json()
        response = self.client.post(
            f"/api/block-documents/{self.document_id}/transactions?response=document",
            json={"base_revision": tree["revision"], "operations": [
                {"operation": "insert", "type": "image", "attrs": {"url": self.url}}
            ]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.image_block = next(
            block["id"] for block in response.json()["children"] if block["type"] == "image"
        )
        self.client.post("/api/block-documents", json={"title": "Groceries", "body": "Milk and eggs"})
        self.before = {b: v for b, _d, v, _t in block_store.embedding_documents(self.db)}

        # Nothing is readable until the queue has run.
        self.assertEqual(self.client.get("/api/unified-search", params={"q": "zeppelin"}).json(), [])
        info = self.client.get(f"/api/block-documents/{self.document_id}/info").json()
        self.assertEqual(info["picture_text"][0]["status"], "queued")
        with patch.object(
            ocr, "read_asset",
            return_value=ocr.Result("Boarding pass\nZeppelin NT flight 7731 to Friedrichshafen", "ocr"),
        ):
            self.assertEqual(ocr.process_one(self.db), True)

    def test_unified_search_finds_the_words_and_shows_them_in_the_snippet(self) -> None:
        results = self.client.get("/api/unified-search", params={"q": "zeppelin friedrichshafen"}).json()
        self.assertEqual(results[0]["block_id"], self.image_block)
        self.assertIn("words", results[0]["matched"])
        start, end = results[0]["highlights"][0]
        self.assertEqual(results[0]["text"][start:end].lower(), "zeppelin")
        # The note body is unchanged: the text is searchable, not shown as content.
        markdown = self.client.get(f"/api/block-documents/{self.document_id}/markdown").text
        self.assertNotIn("Zeppelin", markdown)

    def test_embedding_index_reencodes_only_the_block_that_got_text(self) -> None:
        from app import embeddings

        after = {b: (v, t) for b, _d, v, t in block_store.embedding_documents(self.db)}
        self.assertIn("Friedrichshafen", after[self.image_block][1])
        changed = [b for b, (v, _t) in after.items() if self.before.get(b) != v]
        self.assertEqual(changed, [self.image_block])
        # Meaning search (keyword stand-in for ColBERT here) scores the block.
        docs = [(b, v, t) for b, (v, t) in after.items()]
        scores = embeddings.score_documents("zeppelin flight", docs)
        self.assertEqual(max(scores, key=scores.get), self.image_block)

    def test_ask_grep_finds_it_and_the_model_sees_and_can_quote_it(self) -> None:
        from app import ask, llm

        hits = ask.grep_blocks("friedrichs?hafen")
        self.assertEqual([hit["id"] for hit in hits], [self.image_block])
        self.assertIn("Friedrichshafen", hits[0]["asset_text"])

        reply = {"sentences": [{"text": "Your Zeppelin flight is 7731.", "citations": [
            {"id": "S1", "quote": "Zeppelin NT flight 7731"}]}]}
        fake = AsyncMock(return_value=reply)

        async def stream(*args, **kwargs):
            yield json.dumps(await fake(*args, **kwargs))

        with patch.object(llm, "chat_json_stream", stream):
            response = self.client.post("/api/ask", json={"question": "Which zeppelin flight?"})
        answer = response.json()
        self.assertEqual(answer["status"], "answered", answer)
        citation = answer["answer"][0]["citations"][0]
        self.assertEqual((citation["block_id"], citation["document_id"]),
                         (self.image_block, self.document_id))
        prompt = " ".join(str(arg) for arg in fake.call_args.args)
        self.assertIn("Zeppelin NT flight 7731", prompt)

    def test_info_shows_the_text_and_status_and_its_etag_changes(self) -> None:
        response = self.client.get(f"/api/block-documents/{self.document_id}/info")
        [picture] = response.json()["picture_text"]
        self.assertEqual(picture["status"], "done")
        self.assertEqual(picture["method"], "ocr")
        self.assertEqual(picture["model"], ocr.MODEL)
        self.assertIn("Zeppelin", picture["text"])
        self.assertEqual(picture["block_id"], self.image_block)
        status = self.client.get("/api/ocr").json()
        self.assertEqual((status["done"], status["queued"]), (1, 0))

        etag = response.headers["etag"]
        with patch.object(ocr, "MODEL", "next-model/1"), patch.object(
            ocr, "read_asset", return_value=ocr.Result("Zeppelin NT flight 7731, gate B", "ocr")
        ):
            ocr.process_one(self.db)
        again = self.client.get(
            f"/api/block-documents/{self.document_id}/info", headers={"If-None-Match": etag}
        )
        self.assertEqual(again.status_code, 200)
        self.assertIn("gate B", again.json()["picture_text"][0]["text"])


class EndToEndTests(unittest.TestCase):
    """The app's own worker and model: upload a screenshot, then find its words."""

    def test_uploaded_screenshot_becomes_searchable(self) -> None:
        from fastapi.testclient import TestClient

        from app import ask, documents, embeddings, main, search

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            settings = SimpleNamespace(
                block_db_import_on_startup=False, block_db_path=notes / ".fortress.sqlite3",
                notes_path=notes, assets_path=notes / "assets", embeddings_enabled=False,
                vision_enabled=True, auto_backup_enabled=False, reindex_interval_s=5.0,
                ocr_threads=2,
            )
            settings.assets_path.mkdir()
            patches = [
                patch.object(main, "settings", settings),
                patch.object(documents, "get_settings", lambda: settings),
                patch.object(search, "get_settings", lambda: settings),
                patch.object(ask, "get_settings", lambda: settings),
                patch.object(ocr, "get_settings", lambda: settings),
                patch("app.assets.get_settings", lambda: settings),
                patch.object(embeddings, "_get_model", lambda: None),
            ]
            for item in patches:
                item.start()
                self.addCleanup(item.stop)
            buffer = io.BytesIO()
            text_image(["Quarterly offsite in Reykjavik", "Budget approved 48,000"]).save(buffer, "PNG")
            with TestClient(main.app) as client:
                url = client.post(
                    "/api/files", files={"file": ("shot.png", buffer.getvalue(), "image/png")}
                ).json()["props"]["url"]
                document_id = client.post(
                    "/api/block-documents", json={"title": "Pasted", "body": ""}
                ).json()["id"]
                tree = client.get(f"/api/block-documents/{document_id}").json()
                client.post(f"/api/block-documents/{document_id}/transactions", json={
                    "base_revision": tree["revision"],
                    "operations": [{"operation": "insert", "type": "image", "attrs": {"url": url}}],
                })
                self.assertTrue(ocr.wait_idle(120))
                results = client.get("/api/unified-search", params={"q": "reykjavik"}).json()
                self.assertEqual([r["document_id"] for r in results], [document_id])
                self.assertEqual(ask.grep_blocks("reykjav")[0]["document_id"], document_id)


@unittest.skipUnless(
    os.environ.get("FORTRESS_REAL_COLBERT")
    or (Path.home() / ".cache/huggingface/hub/models--lightonai--Agent-ModernColBERT").is_dir(),
    "ColBERT model not downloaded",
)
class RealColbertTests(unittest.TestCase):
    """The real ColBERT model ranks a block by the text read from its picture."""

    def test_colbert_matches_ocr_text(self) -> None:
        from app import embeddings

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = block_store.create_block_document(database, "Trips", "rough", "")
            assets = Path(directory)
            names = []
            for name, data in (("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"a"), ("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"b")):
                path = assets / name
                path.write_bytes(data)
                block_store.register_asset(database, path, hashlib.sha256(data).hexdigest(), "image/png")
                names.append(name)
            block_store.apply_transaction(database, document["id"], [
                BlockOperation(operation="insert", type="image", attrs={"url": f"/media/{n}"})
                for n in names
            ], 0)
            block_store.store_asset_text(database, hashlib.sha256(b"a").hexdigest(),
                                         "Boarding pass Zeppelin NT flight to Friedrichshafen, gate 4")
            block_store.store_asset_text(database, hashlib.sha256(b"b").hexdigest(),
                                         "Grocery receipt: milk, eggs, bread, total 6.20")
            settings = SimpleNamespace(
                block_db_path=database, colbert_model="lightonai/Agent-ModernColBERT",
                embeddings_precision="float32", precompute_query_embeddings=False,
            )
            with patch.object(embeddings, "get_settings", return_value=settings), patch.object(
                embeddings, "_cache", embeddings._Cache()
            ):
                if embeddings._get_model() is None:
                    self.skipTest("ColBERT model could not be loaded")
                self.assertEqual(embeddings.warm_index(queries=False), 2)
                docs = [(b, v, t) for b, _d, v, t in block_store.embedding_documents(database)]
                scores = embeddings.score_documents("airship trip boarding", docs)
                best = max(scores, key=scores.get)
                self.assertIn("Zeppelin", dict((b, t) for b, _v, t in docs)[best])


if __name__ == "__main__":
    unittest.main()
