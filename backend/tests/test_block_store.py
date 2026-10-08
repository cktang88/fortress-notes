import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    DocumentRevisionConflict,
    apply_transaction,
    backup_database,
    backfill_assets,
    block_link_targets,
    bootstrap_markdown,
    check_integrity,
    connection_scope,
    create_folder,
    create_document,
    create_block_document,
    delete_folder,
    delete_document,
    document_backlinks,
    document_tree,
    document_subtree,
    embedding_documents,
    ensure_fts_integrity,
    fts_is_consistent,
    initialize,
    move_document,
    move_folder,
    navigation,
    list_documents,
    rename_document,
    rename_folder,
    replace_document_from_markdown,
    redo_transaction,
    rebuild_fts,
    register_asset,
    search_blocks,
    store_asset_text,
    sync_markdown,
    undo_transaction,
)
from app.models import BlockOperation, BlockTransaction
from pydantic import ValidationError


class BlockStoreTests(unittest.TestCase):
    def test_fresh_database_includes_current_transaction_receipt_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"

            initialize(database)

            with connection_scope(database) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'transaction_receipts'"
                ).fetchone()
                version = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                ).fetchone()["version"]
            self.assertIsNotNone(table)
            self.assertEqual(version, 11)

    def test_empty_document_starts_with_a_persisted_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"

            document = create_block_document(database, "Blank", "rough", [], "")

            self.assertEqual(len(document["children"]), 1)
            block = document["children"][0]
            self.assertEqual(block["type"], "paragraph")
            self.assertEqual(block["text"], "")
            self.assertRegex(block["id"], r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

    def test_backfills_supported_legacy_media_and_sidecar_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media_dir = root / "assets"
            media_dir.mkdir()
            image_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
            image = media_dir / f"{image_id}.png"
            image.write_bytes(b"legacy image")
            (media_dir / f"{image_id}.txt").write_text("old OCR", encoding="utf-8")
            audio_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
            (media_dir / f"{audio_id}.mp3").write_bytes(b"audio")
            linked_id = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
            (media_dir / f"{linked_id}.png").write_bytes(b"linked image")
            outside_text = root / "outside.txt"
            outside_text.write_text("outside OCR", encoding="utf-8")
            linked_sidecar = media_dir / f"{linked_id}.txt"
            try:
                linked_sidecar.symlink_to(outside_text)
            except OSError:
                self.skipTest("symlinks are unavailable")
            (media_dir / "unknown.bin").write_bytes(b"ignore")
            (media_dir / "readme.txt").write_text("ignore", encoding="utf-8")
            create_document(
                database,
                "legacy-image-note",
                "Legacy image",
                "rough",
                [],
                f"![legacy](/media/{image.name})",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )

            count = backfill_assets(database, media_dir)
            repeated_count = backfill_assets(database, media_dir)

            with connection_scope(database) as connection:
                rows = connection.execute(
                    "SELECT media_name, mime_type, text FROM assets ORDER BY media_name"
                ).fetchall()
            self.assertEqual(count, 3)
            self.assertEqual(repeated_count, 3)
            self.assertEqual(
                [(row["media_name"], row["mime_type"], row["text"]) for row in rows],
                [
                    (f"{image_id}.png", "image/png", "old OCR"),
                    (f"{audio_id}.mp3", "audio/mpeg", ""),
                    (f"{linked_id}.png", "image/png", ""),
                ],
            )
            digest = hashlib.sha256(b"legacy image").hexdigest()
            self.assertEqual(
                (media_dir / f"{digest}.txt").read_text(encoding="utf-8"), "old OCR"
            )
            self.assertEqual(len(search_blocks(database, "old OCR")), 1)

    def test_ocr_text_updates_block_search_and_embedding_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media = root / "01ARZ3NDEKTSV4RRFFQ69G5FAV.png"
            media.write_bytes(b"new image")
            document = create_block_document(database, "Asset search", "rough", [], "")
            image_hash = hashlib.sha256(b"new image").hexdigest()
            register_asset(database, media, image_hash, "image/png")
            inserted = apply_transaction(
                database,
                document["id"],
                [
                    BlockOperation(
                        operation="insert",
                        type="image",
                        text="Photo",
                        content={"attrs": {"url": f"/media/{media.name}"}},
                    )
                ],
                0,
            )
            block_id = next(
                block["id"] for block in inserted["children"] if block["type"] == "image"
            )
            with connection_scope(database) as connection:
                original_timestamp = connection.execute(
                    "SELECT updated_at FROM blocks WHERE id = ?", (block_id,)
                ).fetchone()["updated_at"]
            original_embedding_version = next(
                item[2] for item in embedding_documents(database) if item[0] == block_id
            )

            store_asset_text(database, image_hash, "Quarterly revenue chart")
            hits = search_blocks(database, "quarterly revenue")
            embedding_input = next(item for item in embedding_documents(database) if item[0] == block_id)
            with connection_scope(database) as connection:
                current_timestamp = connection.execute(
                    "SELECT updated_at FROM blocks WHERE id = ?", (block_id,)
                ).fetchone()["updated_at"]

            self.assertEqual([item["block_id"] for item in hits], [block_id])
            self.assertIn("Quarterly revenue chart", hits[0]["text"])
            self.assertEqual(current_timestamp, original_timestamp)
            self.assertNotEqual(embedding_input[2], original_embedding_version)
            self.assertIn("Quarterly revenue chart", embedding_input[3])
            updated = apply_transaction(
                database,
                document["id"],
                [
                    BlockOperation(
                        operation="update",
                        block_id=block_id,
                        text="Image with OCR",
                        expected_updated_at=original_timestamp,
                    )
                ],
                inserted["revision"],
            )
            self.assertEqual(
                next(block for block in updated["children"] if block["type"] == "image")["text"],
                "Image with OCR",
            )
            rebuild_fts(database)
            self.assertEqual(
                [item["block_id"] for item in search_blocks(database, "quarterly revenue")],
                [block_id],
            )

    def test_asset_text_is_stored_by_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media = root / "01JABCDEF0123456789ABCDEFGH.png"
            media.write_bytes(b"same image")
            register_asset(database, media, "b" * 64, "image/png")

            store_asset_text(database, "b" * 64, "chart text")

            with connection_scope(database) as connection:
                row = connection.execute(
                    "SELECT content_hash, text FROM assets WHERE content_hash = ?",
                    ("b" * 64,),
                ).fetchone()
            self.assertEqual((row["content_hash"], row["text"]), ("b" * 64, "chart text"))

    def test_transaction_links_uploaded_asset_to_exact_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media = root / "01JABCDEF0123456789ABCDEFGH.png"
            media.write_bytes(b"image")
            document = create_block_document(database, "Asset owner", "rough", [], "")
            register_asset(database, media, "a" * 64, "image/png")

            result = apply_transaction(
                database,
                document["id"],
                [
                    BlockOperation(
                        operation="insert",
                        block_id="123e4567-e89b-12d3-a456-426614174000",
                        type="image",
                        content={
                            "type": "image",
                            "attrs": {"url": f"/media/{media.name}"},
                        },
                    )
                ],
                0,
            )
            block_id = next(
                block["id"] for block in result["children"] if block["type"] == "image"
            )
            with connection_scope(database) as connection:
                links = connection.execute(
                    "SELECT block_id, content_hash FROM block_assets"
                ).fetchall()

            self.assertEqual(
                [(row["block_id"], row["content_hash"]) for row in links],
                [(block_id, "a" * 64)],
            )

    def test_document_lifecycle_and_focused_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", ["work"], "Parent")
            self.assertEqual(created["title"], "Draft")
            self.assertEqual([item["id"] for item in list_documents(database)], [created["id"]])
            parent = created["children"][0]
            updated = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="insert", parent_id=parent["id"], text="Child")],
                created["revision"],
            )
            child = updated["children"][0]["children"][0]

            subtree = document_subtree(database, created["id"], child["id"])
            assert subtree is not None
            self.assertEqual(subtree["parent"]["id"], parent["id"])
            self.assertEqual(subtree["children"], [])
            self.assertEqual(subtree["depth"], 1)
            self.assertEqual([item["id"] for item in subtree["breadcrumbs"]], [parent["id"], child["id"]])

            renamed = rename_document(database, created["id"], "Renamed")
            self.assertEqual(renamed["title"], "Renamed")
            delete_document(database, created["id"])
            self.assertEqual(list_documents(database), [])
            self.assertIsNone(document_tree(database, created["id"]))

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

    def test_transaction_journal_survives_reopen_and_rolls_back_with_failed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", [], "Start")
            block_id = created["children"][0]["id"]

            updated = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="update", block_id=block_id, text="Saved")],
                created["revision"],
            )
            with connection_scope(database) as connection:
                journal = connection.execute(
                    "SELECT block_id, operation_json FROM revisions "
                    "WHERE document_id = ? ORDER BY id",
                    (created["id"],),
                ).fetchall()

            self.assertEqual(len(journal), 1)
            self.assertEqual(journal[0]["block_id"], block_id)
            self.assertEqual(json.loads(journal[0]["operation_json"])["text"], "Saved")

            with self.assertRaises(ValueError):
                apply_transaction(
                    database,
                    created["id"],
                    [
                        BlockOperation(operation="update", block_id=block_id, text="Not saved"),
                        BlockOperation(operation="split", block_id=block_id, split_at=999),
                    ],
                    updated["revision"],
                )

            # A new connection models a process restart: committed content and
            # its journal entry remain, while the failed batch leaves no trace.
            recovered = document_tree(database, created["id"])
            assert recovered is not None
            self.assertEqual(recovered["children"][0]["text"], "Saved")
            with connection_scope(database) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM revisions WHERE document_id = ?",
                    (created["id"],),
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_persistent_undo_redo_restores_block_state_and_invalidates_redo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", [], "Start")
            block = created["children"][0]
            changed = apply_transaction(
                database,
                created["id"],
                [
                    BlockOperation(
                        operation="update",
                        block_id=block["id"],
                        type="heading",
                        attrs={"level": 2},
                        content={"blocknote": [{"type": "text", "text": "Changed"}]},
                        text="Changed",
                    ),
                    BlockOperation(
                        operation="set_user_attrs",
                        block_id=block["id"],
                        user_attrs={"review": "keep"},
                    ),
                ],
                created["revision"],
            )

            # History remains available after the database is reopened.
            recovered = document_tree(database, created["id"])
            assert recovered is not None
            self.assertTrue(recovered["can_undo"])
            undone = undo_transaction(database, created["id"], recovered["revision"])
            restored = undone["children"][0]
            self.assertEqual(restored["id"], block["id"])
            self.assertEqual(restored["type"], block["type"])
            self.assertEqual(restored["attrs"], block["attrs"])
            self.assertEqual(restored["content"], block["content"])
            self.assertEqual(restored["text"], block["text"])
            self.assertEqual(restored["user_attrs"], {})
            self.assertTrue(undone["can_redo"])

            redone = redo_transaction(database, created["id"], undone["revision"])
            replayed = redone["children"][0]
            self.assertEqual(replayed["id"], block["id"])
            self.assertEqual(replayed["text"], "Changed")
            self.assertEqual(replayed["user_attrs"], {"review": "keep"})

            undone_again = undo_transaction(database, created["id"], redone["revision"])
            branched = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="update", block_id=block["id"], text="Branch")],
                undone_again["revision"],
            )
            self.assertFalse(branched["can_redo"])
            with self.assertRaises(ValueError):
                redo_transaction(database, created["id"], branched["revision"])
            with self.assertRaises(DocumentRevisionConflict):
                undo_transaction(database, created["id"], branched["revision"] - 1)
            unchanged = document_tree(database, created["id"])
            assert unchanged is not None
            self.assertEqual(unchanged["children"][0]["text"], "Branch")

    def test_undo_restores_nested_deleted_subtree_and_split_merge_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", [], "Parent")
            parent = created["children"][0]
            nested = apply_transaction(
                database,
                created["id"],
                [
                    BlockOperation(
                        operation="insert",
                        block_id="550e8400-e29b-41d4-a716-446655440000",
                        parent_id=parent["id"],
                        text="Child",
                        content={"blocknote": [{"type": "text", "text": "Child"}]},
                    )
                ],
                created["revision"],
            )
            child = nested["children"][0]["children"][0]
            create_block_document(
                database,
                "Reference",
                "rough",
                [],
                f'Link (({parent["id"]} "Parent"))',
            )
            deleted = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="delete", block_id=parent["id"])],
                nested["revision"],
            )
            restored = undo_transaction(database, created["id"], deleted["revision"])
            self.assertEqual(restored["children"][0]["id"], parent["id"])
            self.assertEqual(restored["children"][0]["children"][0]["id"], child["id"])
            self.assertEqual(len(document_backlinks(database, created["id"])), 1)
            self.assertTrue(fts_is_consistent(database))

            leaf = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="delete", block_id=child["id"])],
                restored["revision"],
            )
            before_split = leaf
            original_id = before_split["children"][0]["id"]
            split = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="split", block_id=original_id, split_at=3)],
                before_split["revision"],
            )
            split_ids = [node["id"] for node in split["children"]]
            merged = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="merge", block_id=original_id)],
                split["revision"],
            )
            undo_merge = undo_transaction(database, created["id"], merged["revision"])
            self.assertEqual([node["id"] for node in undo_merge["children"]], split_ids)
            undo_split = undo_transaction(database, created["id"], undo_merge["revision"])
            self.assertEqual([node["id"] for node in undo_split["children"]], [original_id])
            redo_split = redo_transaction(database, created["id"], undo_split["revision"])
            self.assertEqual([node["id"] for node in redo_split["children"]], split_ids)

    def test_undo_redo_preserves_duplicate_ids_and_sibling_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", [], "A\n\nB\n\nC")
            initial_ids = [node["id"] for node in created["children"]]
            duplicated = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="duplicate", block_id=initial_ids[1])],
                created["revision"],
            )
            duplicate_id = duplicated["children"][2]["id"]
            undo_duplicate = undo_transaction(database, created["id"], duplicated["revision"])
            self.assertEqual([node["id"] for node in undo_duplicate["children"]], initial_ids)
            redo_duplicate = redo_transaction(database, created["id"], undo_duplicate["revision"])
            self.assertEqual(
                [node["id"] for node in redo_duplicate["children"]],
                [*initial_ids[:2], duplicate_id, initial_ids[2]],
            )

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=initial_ids[0], position=3)],
                redo_duplicate["revision"],
            )
            moved_ids = [node["id"] for node in moved["children"]]
            self.assertEqual(moved_ids, [initial_ids[1], duplicate_id, initial_ids[2], initial_ids[0]])
            undo_move = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual([node["id"] for node in undo_move["children"]], [*initial_ids[:2], duplicate_id, initial_ids[2]])
            redo_move = redo_transaction(database, created["id"], undo_move["revision"])
            self.assertEqual([node["id"] for node in redo_move["children"]], moved_ids)

            deleted_id = moved_ids[1]
            deleted = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="delete", block_id=deleted_id)],
                redo_move["revision"],
            )
            undo_delete = undo_transaction(database, created["id"], deleted["revision"])
            self.assertEqual([node["id"] for node in undo_delete["children"]], moved_ids)

    def test_nested_move_undo_redo_restores_unique_sibling_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", [], "Parent")
            parent_id = created["children"][0]["id"]
            inserted = apply_transaction(
                database,
                created["id"],
                [
                    BlockOperation(operation="insert", block_id="550e8400-e29b-41d4-a716-446655440001", parent_id=parent_id, text="A"),
                    BlockOperation(operation="insert", block_id="550e8400-e29b-41d4-a716-446655440002", parent_id=parent_id, text="B"),
                    BlockOperation(operation="insert", block_id="550e8400-e29b-41d4-a716-446655440003", parent_id=parent_id, text="C"),
                ],
                created["revision"],
            )
            original_ids = [node["id"] for node in inserted["children"][0]["children"]]
            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(
                    operation="move", block_id=original_ids[0], parent_id=parent_id, position=2
                )],
                inserted["revision"],
            )
            moved_ids = [node["id"] for node in moved["children"][0]["children"]]
            self.assertEqual(moved_ids, [original_ids[1], original_ids[2], original_ids[0]])
            undone = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual([node["id"] for node in undone["children"][0]["children"]], original_ids)
            redone = redo_transaction(database, created["id"], undone["revision"])
            self.assertEqual([node["id"] for node in redone["children"][0]["children"]], moved_ids)

    def test_user_attributes_round_trip_update_separately_from_rendering_props(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "user-attrs",
                "User attributes",
                "rough",
                [],
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            empty = document_tree(database, "user-attrs")
            assert empty is not None
            inserted = apply_transaction(
                database,
                "user-attrs",
                [
                    BlockOperation(
                        operation="insert",
                        type="heading",
                        attrs={"level": 2},
                        user_attrs={"topic": "plan", "labels": ["work"]},
                        text="Heading",
                    ),
                    BlockOperation(
                        operation="insert",
                        type="image",
                        attrs={"src": "/media/picture.png", "width": 320},
                        user_attrs={"purpose": "cover"},
                    ),
                ],
                empty["revision"],
            )
            heading_id = inserted["children"][0]["id"]

            updated = apply_transaction(
                database,
                "user-attrs",
                [
                    BlockOperation(
                        operation="set_user_attrs",
                        block_id=heading_id,
                        user_attrs={"topic": "final"},
                    )
                ],
                inserted["revision"],
            )
            heading, image = updated["children"]

            self.assertEqual(heading["user_attrs"], {"topic": "final"})
            self.assertEqual(heading["attrs"], {"level": 2})
            self.assertEqual(
                image["user_attrs"],
                {"purpose": "cover"},
            )
            self.assertEqual(
                image["attrs"],
                {"src": "/media/picture.png", "width": 320},
            )
            self.assertEqual(document_tree(database, "user-attrs"), updated)

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

    def test_transaction_receipt_makes_replay_idempotent_and_rejects_id_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "blocks.sqlite3"
            create_document(
                database,
                "receipt-note",
                "Receipt note",
                "rough",
                [],
                "Original",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            initial = document_tree(database, "receipt-note")
            assert initial is not None
            block_id = initial["children"][0]["id"]
            operation = BlockOperation(operation="update", block_id=block_id, text="Saved once")
            first = apply_transaction(database, "receipt-note", [operation], 0, "tx-1")
            # A fresh initialization/connection models replay after server restart.
            initialize(database)
            replay = apply_transaction(database, "receipt-note", [operation], 0, "tx-1")
            self.assertEqual(replay, first)
            self.assertEqual(document_tree(database, "receipt-note")["revision"], 1)
            with self.assertRaisesRegex(ValueError, "already used"):
                apply_transaction(
                    database,
                    "receipt-note",
                    [BlockOperation(operation="update", block_id=block_id, text="Different")],
                    0,
                    "tx-1",
                )

    def test_insert_ids_accept_client_uuids_and_ulids_and_reject_malformed_values(self) -> None:
        uuid_id = "550e8400-e29b-41d4-a716-446655440000"
        ulid_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

        self.assertEqual(
            BlockOperation(operation="insert", block_id=uuid_id).block_id,
            uuid_id,
        )
        self.assertEqual(
            BlockOperation(operation="insert", block_id=ulid_id).block_id,
            ulid_id,
        )
        with self.assertRaises(ValidationError):
            BlockOperation(operation="insert", block_id="not-an-id")

    def test_insert_without_id_uses_server_generated_ulid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "legacy-filename-id",
                "Legacy",
                "rough",
                [],
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            before = document_tree(database, "legacy-filename-id")
            assert before is not None

            after = apply_transaction(
                database,
                "legacy-filename-id",
                [BlockOperation(operation="insert", text="New block")],
                before["revision"],
            )

            block_id = after["children"][0]["id"]
            self.assertEqual(len(block_id), 26)
            self.assertIn(block_id[0], "01234567")
            self.assertTrue(
                all(character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for character in block_id)
            )
            self.assertEqual(after["id"], "legacy-filename-id")

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
                self.assertEqual(
                    [row["version"] for row in versions], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
                )
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
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'block_user_attrs'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'block_assets'"
                    ).fetchone()
                )
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(documents)")
                }
                self.assertTrue(
                    {"folder_id", "position", "deleted_at", "revision"} <= columns
                )
                asset_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(assets)")
                }
                self.assertIn("text_updated_at", asset_columns)
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

    def test_markdown_replacement_advances_revision_and_invalidates_old_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                [],
                "Original",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            original = document_tree(database, "one")
            assert original is not None
            block_id = original["children"][0]["id"]
            edited = apply_transaction(
                database,
                "one",
                [BlockOperation(operation="update", block_id=block_id, text="Block edit")],
                original["revision"],
            )

            replace_document_from_markdown(
                database,
                "one",
                "One",
                "rough",
                [],
                "Markdown edit",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )
            replaced = document_tree(database, "one")
            assert replaced is not None
            self.assertEqual(replaced["revision"], edited["revision"] + 1)
            self.assertEqual(replaced["children"][0]["text"], "Markdown edit")
            with self.assertRaises(DocumentRevisionConflict):
                apply_transaction(database, "one", [BlockOperation(operation="update", block_id=block_id, text="Stale")], edited["revision"])
            with self.assertRaisesRegex(ValueError, "nothing to undo"):
                undo_transaction(database, "one", replaced["revision"])

if __name__ == "__main__":
    unittest.main()
