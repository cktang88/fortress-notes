import tempfile
import unittest
from pathlib import Path

from app.block_store import apply_transaction, bootstrap_markdown, connect, document_tree
from app.models import BlockOperation


class BlockStoreTests(unittest.TestCase):
    def test_bootstraps_markdown_once_and_preserves_block_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text(
                "---\n"
                "title: One\n"
                "status: polished\n"
                "tags: [demo]\n"
                "---\n"
                "# Heading\n\n"
                "Paragraph\n\n"
                "- item one\n"
                "- item two\n\n"
                "```python\n"
                "print(1)\n"
                "```\n",
                encoding="utf-8",
            )
            database = root / ".fortress.sqlite3"

            self.assertEqual(bootstrap_markdown(root, database), 1)
            self.assertEqual(bootstrap_markdown(root, database), 0)

            tree = document_tree(database, "one")
            self.assertIsNotNone(tree)
            assert tree is not None
            self.assertEqual(tree["title"], "One")
            self.assertEqual(tree["tags"], ["demo"])
            self.assertEqual(
                [block["type"] for block in tree["children"]],
                ["heading", "paragraph", "list", "code"],
            )
            self.assertEqual(
                [block["position"] for block in tree["children"]], [0, 1, 2, 3]
            )

    def test_transactions_insert_update_move_and_delete_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text("---\ntitle: One\n---\nA\n\nB\n", encoding="utf-8")
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="insert", position=1, text="X")],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["A", "X", "B"])
            ids = [block["id"] for block in tree["children"]]

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="move", block_id=ids[0], position=2)],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "B", "A"])

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="update", block_id=ids[2], text="BB")],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "BB", "A"])

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="delete", block_id=ids[2])],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "A"])
            with connect(database) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM blocks_fts WHERE block_id = ?", (ids[2],)
                    ).fetchone()
                )


if __name__ == "__main__":
    unittest.main()
