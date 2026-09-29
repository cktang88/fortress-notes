"""Large local-store inputs for manual performance checks."""

import tempfile
import unittest
from pathlib import Path

from app.block_store import create_document, document_tree


class BlockStorePerformanceFixtures(unittest.TestCase):
    def test_ten_thousand_blocks_and_one_million_words(self) -> None:
        block_count = 10_000
        word_count = 1_000_000
        many_blocks = "\n\n".join(f"Block {index}" for index in range(block_count))
        many_words = "word " * word_count

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "ten-thousand-blocks",
                "10,000 blocks",
                "rough",
                [],
                many_blocks,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            create_document(
                database,
                "million-words",
                "1 million words",
                "rough",
                [],
                many_words,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )

            block_tree = document_tree(database, "ten-thousand-blocks")
            word_tree = document_tree(database, "million-words")
            assert block_tree is not None and word_tree is not None
            self.assertEqual(len(block_tree["children"]), block_count)
            self.assertEqual(len(word_tree["children"][0]["text"].split()), word_count)


if __name__ == "__main__":
    unittest.main()
