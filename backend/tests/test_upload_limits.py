import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class UploadLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import assets, documents, images, main, search

        self.main = main
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            vlm_caption_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=3600.0,
        )
        settings.assets_path.mkdir()
        self.assets_path = settings.assets_path
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
            patch.object(search, "get_settings", lambda: settings),
            patch.object(images, "get_settings", lambda: settings),
            patch.object(assets, "get_settings", lambda: settings),
            patch.object(images, "extract_and_store", lambda _path: None),
            patch.object(main, "MAX_UPLOAD_BYTES", 3 * 1024 * 1024 + 5),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_oversized_uploads_are_refused_with_413_and_nothing_is_saved(self) -> None:
        too_big = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
        for endpoint, content_type in (("/api/files", "application/pdf"), ("/api/images", "image/png")):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    endpoint, files={"file": ("big.bin", too_big, content_type)}
                )
                self.assertEqual(response.status_code, 413, response.text)
                self.assertIn("larger than", response.json()["detail"])
        self.assertEqual(list(self.assets_path.iterdir()), [])

    def test_uploads_at_the_limit_are_saved(self) -> None:
        fits = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024 - 3)
        for endpoint, content_type in (("/api/files", "application/pdf"), ("/api/images", "image/png")):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    endpoint, files={"file": ("ok.bin", fits, content_type)}
                )
                self.assertEqual(response.status_code, 200, response.text)
        saved = sorted(path.stat().st_size for path in self.assets_path.iterdir())
        self.assertEqual(saved, [len(fits), len(fits)])

    def test_reading_stops_once_the_limit_is_passed(self) -> None:
        import asyncio

        class Upload:
            def __init__(self) -> None:
                self.read_bytes = 0

            async def read(self, size: int = -1) -> bytes:
                self.assertBounded(size)
                self.read_bytes += size
                return b"x" * size

            def assertBounded(self, size: int) -> None:
                assert 0 < size <= 1024 * 1024, size

        upload = Upload()
        with self.assertRaises(self.main.HTTPException) as raised:
            asyncio.run(self.main._read_upload(upload, limit=2 * 1024 * 1024))
        self.assertEqual(raised.exception.status_code, 413)
        self.assertLessEqual(upload.read_bytes, 3 * 1024 * 1024)


class DefaultUploadLimitTests(unittest.TestCase):
    def test_the_limit_is_a_generous_fifty_megabytes(self) -> None:
        from app import main

        self.assertEqual(main.MAX_UPLOAD_BYTES, 50 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
