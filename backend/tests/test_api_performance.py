"""Performance bugs on the hot paths the native app hits (each proven by a test)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import block_store
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


if __name__ == "__main__":
    unittest.main()
