import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    apply_transaction,
    backup_database,
    block_link_targets,
    bootstrap_markdown,
    check_integrity,
    connection_scope,
    create_document,
    delete_document,
    document_backlinks,
    document_tree,
    initialize,
    navigation,
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

    def test_lists_stable_block_targets_for_reference_autocomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text(
                "---\ntitle: One\n---\nFirst block\n\nSecond block\n", encoding="utf-8"
            )
            (root / "two.md").write_text(
                "---\ntitle: Two\n---\nAnother block\n", encoding="utf-8"
            )
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)

            all_targets = block_link_targets(database, "block", limit=10)
            self.assertEqual(len(all_targets), 3)
            self.assertEqual(all_targets[0]["document_id"], "two")
            self.assertEqual(block_link_targets(database, "second")[0]["text"], "Second block")
            self.assertEqual(len(block_link_targets(database, "One")), 1)
            self.assertEqual(block_link_targets(database, "missing"), [])
            self.assertEqual(block_link_targets(database, "%"), [])
            self.assertEqual(block_link_targets(database), [])

    def test_rebuilds_block_and_document_backlinks_in_the_same_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            create_document(
                database,
                "target",
                "Target",
                "rough",
                [],
                "Target block",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            target = document_tree(database, "target")
            assert target is not None
            target_block_id = target["children"][0]["id"]
            create_document(
                database,
                "source",
                "Source",
                "rough",
                [],
                f"(( {target_block_id} \"block label\" )) and [[target|document label]]",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )

            backlinks = document_backlinks(database, "target")
            self.assertEqual(len(backlinks), 2)
            self.assertEqual(
                {backlink["label"] for backlink in backlinks},
                {"block label", "document label"},
            )
            self.assertEqual(
                {backlink["source_document_id"] for backlink in backlinks}, {"source"}
            )

            replace_document_from_markdown(
                database,
                "source",
                "Source",
                "rough",
                [],
                "No links now",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )
            self.assertEqual(document_backlinks(database, "target"), [])

    def test_upgrades_schema_version_one_to_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            with connection_scope(database) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    ("2026-01-01T00:00:00+00:00",),
                )
            initialize(database)
            with connection_scope(database) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual([row["version"] for row in versions], [1, 2, 3])
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_refs'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'folders'"
                    ).fetchone()
                )

    def test_navigation_excludes_soft_deleted_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                [],
                "First",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            create_document(
                database,
                "two",
                "Two",
                "polished",
                ["demo"],
                "Second",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )

            result = navigation(database, recent_limit=1)
            self.assertEqual([item["id"] for item in result["items"]], ["one", "two"])
            self.assertEqual([item["id"] for item in result["recent"]], ["two"])

            delete_document(database, "one")
            result = navigation(database)
            self.assertEqual([item["id"] for item in result["items"]], ["two"])
            self.assertIsNone(document_tree(database, "one"))

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
