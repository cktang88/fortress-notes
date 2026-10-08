import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import assets, block_store, images


class ImageTextCacheTests(unittest.TestCase):
    def test_reuses_hash_cache_for_duplicate_upload_and_updates_asset_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            first = root / "first.png"
            duplicate = root / "duplicate.jpg"
            first.write_bytes(b"same image")
            duplicate.write_bytes(b"same image")
            content_hash = assets.content_hash(first.read_bytes())
            settings = SimpleNamespace(
                assets_path=root,
                block_db_enabled=True,
                block_db_path=database,
                vlm_caption_enabled=False,
            )
            block_store.register_asset(database, first, content_hash, "image/png")
            block_store.register_asset(database, duplicate, content_hash, "image/jpeg")

            with patch("app.images.get_settings", return_value=settings), patch(
                "app.images.vision.describe_image", return_value="A chart\nText in image: sales"
            ) as describe:
                first_result = images.extract_and_store(first)
                duplicate_result = images.extract_and_store(duplicate)

            self.assertEqual(first_result, "A chart\nText in image: sales")
            self.assertEqual(duplicate_result, first_result)
            describe.assert_called_once_with(first)
            self.assertEqual(first.with_suffix(".txt").read_text(), first_result)
            self.assertEqual(duplicate.with_suffix(".txt").read_text(), first_result)
            self.assertEqual((root / f"{content_hash}.txt").read_text(), first_result)
            with block_store.connection_scope(database) as connection:
                rows = connection.execute(
                    "SELECT media_name, text FROM assets ORDER BY media_name"
                ).fetchall()
            self.assertEqual(
                [(row["media_name"], row["text"]) for row in rows],
                [("duplicate.jpg", first_result), ("first.png", first_result)],
            )

    def test_retries_ocr_only_cache_when_captions_are_enabled_then_reuses_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(b"image")
            image.with_suffix(".txt").write_text("Text in image: old OCR")
            settings = SimpleNamespace(
                assets_path=root,
                block_db_path=root / ".fortress.sqlite3",
                vlm_caption_enabled=True,
            )
            with patch("app.images.get_settings", return_value=settings), patch(
                "app.images.vision.describe_image",
                return_value="A small chart\nText in image: old OCR",
            ) as describe:
                first = images.extract_and_store(image)
                second = images.extract_and_store(image)

            self.assertEqual(first, "A small chart\nText in image: old OCR")
            self.assertEqual(second, first)
            describe.assert_called_once_with(image)

    def test_does_not_cache_empty_result_so_later_call_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(b"image")
            settings = SimpleNamespace(
                assets_path=root,
                block_db_path=root / ".fortress.sqlite3",
                vlm_caption_enabled=False,
            )
            with patch("app.images.get_settings", return_value=settings), patch(
                "app.images.vision.describe_image", side_effect=["", "Text in image: retry"]
            ) as describe:
                self.assertEqual(images.extract_and_store(image), "")
                self.assertFalse(image.with_suffix(".txt").exists())
                self.assertEqual(images.extract_and_store(image), "Text in image: retry")

            self.assertEqual(describe.call_count, 2)

    def test_serializes_duplicate_inference_for_concurrent_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.png"
            duplicate = root / "duplicate.png"
            first.write_bytes(b"same image")
            duplicate.write_bytes(b"same image")
            settings = SimpleNamespace(
                assets_path=root,
                block_db_path=root / ".fortress.sqlite3",
                vlm_caption_enabled=False,
            )
            start = threading.Barrier(3)
            entered = threading.Event()
            release = threading.Event()
            calls: list[Path] = []

            def describe(path: Path) -> str:
                calls.append(path)
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("test did not release image extraction")
                return "Shared image text"

            with patch("app.images.get_settings", return_value=settings), patch(
                "app.images.vision.describe_image", side_effect=describe
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(
                        lambda: (start.wait(), images.extract_and_store(first))[1]
                    )
                    duplicate_future = executor.submit(
                        lambda: (start.wait(), images.extract_and_store(duplicate))[1]
                    )
                    start.wait()
                    self.assertTrue(entered.wait(timeout=2))
                    time.sleep(0.05)
                    release.set()
                    self.assertEqual(first_future.result(timeout=3), "Shared image text")
                    self.assertEqual(duplicate_future.result(timeout=3), "Shared image text")

            self.assertEqual(len(calls), 1)
            self.assertEqual(first.with_suffix(".txt").read_text(), "Shared image text")
            self.assertEqual(duplicate.with_suffix(".txt").read_text(), "Shared image text")


if __name__ == "__main__":
    unittest.main()
