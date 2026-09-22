import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    apply_transaction,
    backup_database,
    bootstrap_markdown,
    check_integrity,
    connection_scope,
    create_document,
    document_tree,
    replace_document_from_markdown,
    search_blocks,
    sync_markdown,
)
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
                ["heading", "paragraph", "list", "list", "code"],
            )
            self.assertEqual(
                [block["position"] for block in tree["children"]], [0, 1, 2, 3, 4]
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
            with connection_scope(database) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM blocks_fts WHERE block_id = ?", (ids[2],)
                    ).fetchone()
                )

    def test_imports_nested_tasks_and_rich_markdown_as_child_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text(
                "# Guide\n\n"
                "- [x] Parent with [link](https://example.com)\n"
                "  - Child with ![image](/media/image.png)\n"
                "    - Grandchild\n\n"
                "> A quote\n\n"
                "```python\nprint('ok')\n```\n",
                encoding="utf-8",
            )
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)

            tree = document_tree(database, "one")
            assert tree is not None
            self.assertEqual(
                [block["type"] for block in tree["children"]],
                ["heading", "list", "quote", "code"],
            )
            parent = tree["children"][1]
            self.assertEqual(parent["attrs"], {"checked": True})
            self.assertIn("[link](https://example.com)", parent["text"])
            self.assertEqual(len(parent["children"]), 1)
            child = parent["children"][0]
            self.assertIn("![image](/media/image.png)", child["text"])
            self.assertEqual([nested["text"] for nested in child["children"]], ["- Grandchild"])

    def test_searches_nested_blocks_through_fts5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text(
                "Parent keyword\n\n  - Child phrase\n\nOther text\n", encoding="utf-8"
            )
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)

            parent_hits = search_blocks(database, "keyword")
            child_hits = search_blocks(database, "phrase")
            self.assertEqual(len(parent_hits), 1)
            self.assertEqual(parent_hits[0]["document_id"], "one")
            self.assertEqual(child_hits[0]["text"], "- Child phrase")
            self.assertEqual(search_blocks(database, "not-present"), [])

    def test_compatibility_mirror_tracks_block_store_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_path = root / "one.md"
            note_path.write_text(
                "---\n"
                "title: One\n"
                "status: rough\n"
                "tags: [demo]\n"
                "custom: keep\n"
                "---\n"
                "Original\n",
                encoding="utf-8",
            )
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)
            tree = document_tree(database, "one")
            assert tree is not None
            block_id = tree["children"][0]["id"]

            apply_transaction(
                database,
                "one",
                [
                    BlockOperation(
                        operation="update",
                        block_id=block_id,
                        content={"markdown": "Updated"},
                        text="Updated",
                    )
                ],
            )
            sync_markdown(root, database, "one")

            mirrored = note_path.read_text(encoding="utf-8")
            self.assertIn("custom: keep", mirrored)
            self.assertIn("Updated", mirrored)
            self.assertNotIn("Original", mirrored)

    def test_markdown_replacement_and_backup_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                ["demo"],
                "First\n\nSecond",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            replace_document_from_markdown(
                database,
                "one",
                "Updated",
                "polished",
                ["new"],
                "# Heading\n\nBody",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )
            backup = root / "backup.sqlite3"
            backup_database(database, backup)

            tree = document_tree(backup, "one")
            self.assertIsNotNone(tree)
            assert tree is not None
            self.assertEqual(tree["title"], "Updated")
            self.assertEqual(tree["status"], "polished")
            self.assertEqual([block["text"] for block in tree["children"]], ["# Heading", "Body"])
            self.assertTrue(check_integrity(backup))


if __name__ == "__main__":
    unittest.main()
