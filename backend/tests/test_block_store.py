import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    DocumentRevisionConflict,
    apply_transaction,
    backup_database,
    block_link_targets,
    bootstrap_markdown,
    check_integrity,
    connection_scope,
    create_folder,
    create_document,
    delete_folder,
    delete_document,
    document_backlinks,
    document_tree,
    ensure_fts_integrity,
    fts_is_consistent,
    initialize,
    move_document,
    move_folder,
    navigation,
    rename_folder,
    replace_document_from_markdown,
    rebuild_fts,
    search_blocks,
    sync_markdown,
)
from app.models import BlockOperation, BlockTransaction
from pydantic import ValidationError


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
            tree = document_tree(database, "one")
            assert tree is not None

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="insert", position=1, text="X")],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["A", "X", "B"])
            ids = [block["id"] for block in tree["children"]]

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="move", block_id=ids[0], position=2)],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "B", "A"])

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="update", block_id=ids[2], text="BB")],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "BB", "A"])

            tree = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="delete", block_id=ids[2])],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "A"])
            self.assertEqual(tree["revision"], 4)
            with connection_scope(database) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM blocks_fts WHERE block_id = ?", (ids[2],)
                    ).fetchone()
                )

    def test_duplicate_preserves_subtree_shape_with_new_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "one", "One", "rough", [], "Parent\n\nChild", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
            before = document_tree(database, "one"); assert before is not None
            original = before["children"][0]
            nested = apply_transaction(database, "one", [BlockOperation(operation="insert", parent_id=original["id"], text="Child")], before["revision"])
            apply_transaction(database, "one", [BlockOperation(operation="duplicate", block_id=original["id"])], nested["revision"])
            tree = document_tree(database, "one"); assert tree is not None
            self.assertEqual([node["text"] for node in tree["children"]], ["Parent", "Parent", "Child"])
            self.assertNotEqual(tree["children"][0]["id"], tree["children"][1]["id"])
            self.assertEqual(tree["children"][1]["children"][0]["text"], "Child")

    def test_split_and_merge_preserve_first_id_and_refresh_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "one", "One", "rough", [], "abcdef\n\nghijkl", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
            before = document_tree(database, "one"); assert before is not None
            first_id = before["children"][0]["id"]
            split = apply_transaction(database, "one", [BlockOperation(operation="split", block_id=first_id, split_at=3)], before["revision"])
            self.assertEqual([node["text"] for node in split["children"]], ["abc", "def", "ghijkl"])
            self.assertEqual(split["children"][0]["id"], first_id)
            merged = apply_transaction(database, "one", [BlockOperation(operation="merge", block_id=first_id)], split["revision"])
            self.assertEqual([node["text"] for node in merged["children"]], ["abcdef", "ghijkl"])
            self.assertEqual(merged["children"][0]["id"], first_id)
            self.assertEqual([hit["text"] for hit in search_blocks(database, "abcdef")], ["abcdef"])

    def test_failed_structural_operation_rolls_back_the_whole_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "one", "One", "rough", [], "A\n\nB", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
            before = document_tree(database, "one"); assert before is not None
            with self.assertRaises(ValueError):
                apply_transaction(database, "one", [BlockOperation(operation="insert", text="C"), BlockOperation(operation="split", block_id=before["children"][0]["id"], split_at=99)], before["revision"])
            after = document_tree(database, "one"); assert after is not None
            self.assertEqual([node["text"] for node in after["children"]], ["A", "B"])
            self.assertEqual(after["revision"], before["revision"])

    def test_transactions_require_and_increment_document_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                [],
                "A\n\nB",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            before = document_tree(database, "one")
            assert before is not None
            self.assertEqual(before["revision"], 0)

            updated = apply_transaction(
                database,
                "one",
                [
                    BlockOperation(operation="insert", text="C"),
                    BlockOperation(operation="insert", text="D"),
                ],
                before["revision"],
            )
            self.assertEqual(updated["revision"], 1)
            self.assertEqual(
                [block["text"] for block in updated["children"]], ["A", "B", "C", "D"]
            )

            with self.assertRaises(DocumentRevisionConflict) as raised:
                apply_transaction(
                    database,
                    "one",
                    [BlockOperation(operation="insert", text="must not be inserted")],
                    before["revision"],
                )
            self.assertEqual(
                raised.exception.detail(),
                {
                    "code": "document_revision_conflict",
                    "document_id": "one",
                    "base_revision": 0,
                    "current_revision": 1,
                },
            )
            after = document_tree(database, "one")
            assert after is not None
            self.assertEqual(after["revision"], 1)
            self.assertEqual(
                [block["text"] for block in after["children"]], ["A", "B", "C", "D"]
            )

    def test_stale_revision_protects_concurrent_block_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                [],
                "A\n\nB\n\nC",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            initial = document_tree(database, "one")
            assert initial is not None
            block_ids = [block["id"] for block in initial["children"]]

            first_move = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="move", block_id=block_ids[0], position=2)],
                initial["revision"],
            )
            self.assertEqual(
                [block["text"] for block in first_move["children"]], ["B", "C", "A"]
            )
            with self.assertRaises(DocumentRevisionConflict):
                apply_transaction(
                    database,
                    "one",
                    [BlockOperation(operation="move", block_id=block_ids[1], position=2)],
                    initial["revision"],
                )
            final = document_tree(database, "one")
            assert final is not None
            self.assertEqual(final["revision"], 1)
            self.assertEqual([block["text"] for block in final["children"]], ["B", "C", "A"])

    def test_block_transactions_require_a_non_negative_base_revision(self) -> None:
        with self.assertRaises(ValidationError):
            BlockTransaction(operations=[BlockOperation(operation="insert")])
        with self.assertRaises(ValidationError):
            BlockTransaction(base_revision=-1, operations=[BlockOperation(operation="insert")])

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

    def test_rebuilds_stale_fts_rows_from_active_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                [],
                "Indexed text",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            with connection_scope(database) as connection:
                connection.execute("DELETE FROM blocks_fts")
            self.assertFalse(fts_is_consistent(database))
            self.assertTrue(ensure_fts_integrity(database))
            self.assertEqual(len(search_blocks(database, "Indexed")), 1)

            delete_document(database, "one")
            self.assertFalse(fts_is_consistent(database))
            rebuild_fts(database)
            self.assertTrue(fts_is_consistent(database))
            self.assertEqual(search_blocks(database, "Indexed"), [])

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

    def test_upgrades_legacy_documents_table_to_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            with connection_scope(database) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.execute(
                    """CREATE TABLE documents(
                           id TEXT PRIMARY KEY,
                           title TEXT NOT NULL,
                           status TEXT NOT NULL,
                           tags_json TEXT NOT NULL,
                           created_at TEXT NOT NULL,
                           updated_at TEXT NOT NULL
                       )"""
                )
                connection.executemany(
                    """INSERT INTO documents(
                           id, title, status, tags_json, created_at, updated_at
                       ) VALUES (?, ?, 'rough', '[]', ?, ?)""",
                    [
                        ("older", "Older", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                        ("newer", "Newer", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
                    ],
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                    ("2026-01-01T00:00:00+00:00",),
                )
            initialize(database)
            with connection_scope(database) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual([row["version"] for row in versions], [2, 3, 4])
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
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(documents)")
                }
                self.assertTrue(
                    {"folder_id", "position", "deleted_at", "revision"} <= columns
                )
                self.assertEqual(
                    [row["revision"] for row in connection.execute(
                        "SELECT revision FROM documents ORDER BY id"
                    )],
                    [0, 0],
                )
                self.assertEqual(
                    [row["position"] for row in connection.execute(
                        "SELECT position FROM documents ORDER BY position"
                    )],
                    [0, 1],
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

    def test_organizes_folders_with_stable_ordering_and_cycle_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            first = create_folder(database, "First")
            second = create_folder(database, "Second", position=0)
            child = create_folder(database, "Child", parent_id=first["id"])

            self.assertEqual(
                [item["name"] for item in navigation(database)["items"]],
                ["Second", "First"],
            )
            renamed = rename_folder(database, first["id"], "Renamed")
            self.assertEqual(renamed["name"], "Renamed")

            with self.assertRaisesRegex(ValueError, "descendant"):
                move_folder(database, first["id"], child["id"])

            moved = move_folder(database, second["id"], first["id"], position=0)
            self.assertEqual(moved["parent_id"], first["id"])
            tree = navigation(database)["items"]
            self.assertEqual(tree[0]["name"], "Renamed")
            self.assertEqual(
                [item["name"] for item in tree[0]["children"]], ["Second", "Child"]
            )

    def test_deletes_only_empty_folders_and_moves_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            source = create_folder(database, "Source")
            destination = create_folder(database, "Destination")
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
                "rough",
                [],
                "Second",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(move_document(database, "one", source["id"])["position"], 0)
            self.assertEqual(
                move_document(database, "two", source["id"], position=0)["position"], 0
            )
            result = navigation(database)
            source_node = next(item for item in result["items"] if item["id"] == source["id"])
            self.assertEqual(
                [item["id"] for item in source_node["children"]], ["two", "one"]
            )
            move_document(database, "two")
            source_node = next(item for item in navigation(database)["items"] if item["id"] == source["id"])
            self.assertEqual([item["id"] for item in source_node["children"]], ["one", "two"])

            with self.assertRaisesRegex(ValueError, "not empty"):
                delete_folder(database, source["id"])

            move_document(database, "one", destination["id"])
            move_document(database, "two", destination["id"])
            create_document(
                database,
                "three",
                "Three",
                "rough",
                [],
                "Third",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            move_document(database, "three", source["id"])
            delete_document(database, "three")
            delete_folder(database, source["id"])
            self.assertEqual(
                [item["id"] for item in navigation(database)["items"]], [destination["id"]]
            )
            with connection_scope(database) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT folder_id FROM documents WHERE id = 'three'"
                    ).fetchone()["folder_id"]
                )

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
                tree["revision"],
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
