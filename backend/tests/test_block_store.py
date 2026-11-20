import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    SCHEMA_VERSION,
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
    ensure_fts_integrity,
    fts_is_consistent,
    initialize,
    move_document,
    move_folder,
    navigation,
    list_documents,
    rename_document,
    rename_folder,
    redo_transaction,
    rebuild_fts,
    register_asset,
    search_blocks,
    store_asset_text,
    undo_transaction,
)
from app.corpus import embedding_documents
from app.block_schema import BlockShapeError
from app.models import BlockOperation, BlockTransaction
from pydantic import ValidationError


def runs(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def paragraph(text: str, **placement) -> BlockOperation:
    return BlockOperation(operation="insert", type="paragraph", content=runs(text), **placement)


class BlockStoreTests(unittest.TestCase):
    def test_fresh_database_includes_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"

            initialize(database)

            with connection_scope(database) as connection:
                tables = {
                    row["name"] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                version = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                ).fetchone()["version"]
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(blocks)")}
            self.assertIn("transaction_receipts", tables)
            self.assertIn("block_sync", tables)
            self.assertFalse({"revisions", "block_attrs"} & tables)
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertTrue({"order_key", "payload_json", "content_json"} <= columns)
            self.assertNotIn("position", columns)


    def test_fresh_database_has_the_folder_position_index_migrations_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            initialize(database)
            initialize(database)  # and initializing again is harmless
            with connection_scope(database) as connection:
                index = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'documents_folder_position'"
                ).fetchone()
            self.assertIsNotNone(index)
            self.assertIn("folder_id, position", index["sql"])

    def test_empty_document_starts_with_a_persisted_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"

            document = create_block_document(database, "Blank", "rough", "")

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
            self.assertEqual(len(search_blocks(database, "old OCR")), 1)

    def test_ocr_text_updates_block_search_and_embedding_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media = root / "01ARZ3NDEKTSV4RRFFQ69G5FAV.png"
            media.write_bytes(b"new image")
            document = create_block_document(database, "Asset search", "rough", "")
            image_hash = hashlib.sha256(b"new image").hexdigest()
            register_asset(database, media, image_hash, "image/png")
            inserted = apply_transaction(
                database,
                document["id"],
                [BlockOperation(operation="insert", type="image",
                                attrs={"url": f"/media/{media.name}", "alt": "Photo"})],
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
                [BlockOperation(operation="update", block_id=block_id, attrs={"alt": "Image with OCR"})],
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
            document = create_block_document(database, "Asset owner", "rough", "")
            register_asset(database, media, "a" * 64, "image/png")

            result = apply_transaction(
                database,
                document["id"],
                [BlockOperation(operation="insert", block_id="123e4567-e89b-12d3-a456-426614174000",
                                type="image", attrs={"url": f"/media/{media.name}"})],
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


    def test_document_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "Parent")
            self.assertEqual(created["title"], "Draft")
            self.assertEqual([item["id"] for item in list_documents(database)], [created["id"]])
            parent = created["children"][0]
            updated = apply_transaction(
                database,
                created["id"],
                [paragraph("Child", parent_id=parent["id"])],
                created["revision"],
            )
            child = updated["children"][0]["children"][0]
            self.assertEqual(child["parent_id"], parent["id"])

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
            self.assertNotIn("tags", tree)  # retired: front matter tags are ignored
            self.assertEqual(
                [block["type"] for block in tree["children"]],
                ["heading", "paragraph", "bullet_item", "bullet_item", "code"],
            )
            self.assertEqual(
                [block["text"] for block in tree["children"]],
                ["Heading", "Paragraph", "item one", "item two", "print(1)"],
            )
            self.assertEqual(
                [block["position"] for block in tree["children"]], [0, 1, 2, 3, 4]
            )
            orders = [block["order"] for block in tree["children"]]
            self.assertEqual(orders, sorted(orders))


    def test_transactions_insert_update_move_and_delete_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.md").write_text("---\ntitle: One\n---\nA\n\nB\n", encoding="utf-8")
            database = root / ".fortress.sqlite3"
            bootstrap_markdown(root, database)
            tree = document_tree(database, "one")
            assert tree is not None
            a_id = tree["children"][0]["id"]

            tree = apply_transaction(database, "one", [paragraph("X", after_id=a_id)], tree["revision"])
            self.assertEqual([block["text"] for block in tree["children"]], ["A", "X", "B"])
            ids = [block["id"] for block in tree["children"]]

            tree = apply_transaction(
                database, "one",
                [BlockOperation(operation="move", block_id=ids[0], after_id=ids[2])],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "B", "A"])

            tree = apply_transaction(
                database, "one",
                [BlockOperation(operation="update", block_id=ids[2], content=runs("BB"))],
                tree["revision"],
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "BB", "A"])

            tree = apply_transaction(
                database, "one", [BlockOperation(operation="delete", block_id=ids[2])], tree["revision"]
            )
            self.assertEqual([block["text"] for block in tree["children"]], ["X", "A"])
            self.assertEqual(tree["revision"], 4)
            with connection_scope(database) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM blocks_fts WHERE block_id = ?", (ids[2],)
                    ).fetchone()
                )


    def test_failed_operation_rolls_back_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "Start")
            block_id = created["children"][0]["id"]

            updated = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="update", block_id=block_id, content=runs("Saved"))],
                created["revision"],
            )
            with self.assertRaises(BlockShapeError):
                apply_transaction(
                    database,
                    created["id"],
                    [
                        BlockOperation(operation="update", block_id=block_id, content=runs("Not saved")),
                        BlockOperation(operation="insert", type="table_cell"),
                    ],
                    updated["revision"],
                )

            # A new connection models a process restart: committed content remains,
            # while the failed batch leaves no trace.
            recovered = document_tree(database, created["id"])
            assert recovered is not None
            self.assertEqual(recovered["children"][0]["text"], "Saved")
            self.assertEqual(recovered["revision"], updated["revision"])


    def test_persistent_undo_redo_restores_block_state_and_invalidates_redo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "Start")
            block = created["children"][0]
            apply_transaction(
                database,
                created["id"],
                [
                    BlockOperation(
                        operation="update", block_id=block["id"], type="heading",
                        attrs={"level": 2}, content=runs("Changed"),
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
            for key in ("id", "type", "attrs", "content", "payload", "text", "order", "updated_at"):
                self.assertEqual(restored[key], block[key], key)
            self.assertEqual(restored["user_attrs"], {})
            self.assertTrue(undone["can_redo"])

            redone = redo_transaction(database, created["id"], undone["revision"])
            replayed = redone["children"][0]
            self.assertEqual(replayed["id"], block["id"])
            self.assertEqual(replayed["text"], "Changed")
            self.assertEqual(replayed["attrs"], {"level": 2})
            self.assertEqual(replayed["user_attrs"], {"review": "keep"})

            undone_again = undo_transaction(database, created["id"], redone["revision"])
            branched = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="update", block_id=block["id"], content=runs("Branch"))],
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


    def test_undo_restores_a_deleted_subtree_and_links_into_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "Parent")
            parent = created["children"][0]
            nested = apply_transaction(
                database, created["id"],
                [paragraph("Child", block_id="550e8400-e29b-41d4-a716-446655440000", parent_id=parent["id"])],
                created["revision"],
            )
            child = nested["children"][0]["children"][0]
            reference = create_block_document(
                database, "Reference", "rough", f"Link (({parent['id']}))"
            )
            self.assertEqual(reference["children"][0]["text"], "Link Parent")
            deleted = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="delete", block_id=parent["id"])],
                nested["revision"],
            )
            self.assertEqual(document_backlinks(database, created["id"]), [])
            # The link stays, reading as broken while its target is gone.
            self.assertEqual(document_tree(database, reference["id"])["children"][0]["text"], "Link ")
            restored = undo_transaction(database, created["id"], deleted["revision"])
            self.assertEqual(restored["children"][0]["id"], parent["id"])
            self.assertEqual(restored["children"][0]["children"][0]["id"], child["id"])
            self.assertEqual(len(document_backlinks(database, created["id"])), 1)
            self.assertEqual(document_tree(database, reference["id"])["children"][0]["text"], "Link Parent")
            self.assertTrue(fts_is_consistent(database))
            redone = redo_transaction(database, created["id"], restored["revision"])
            self.assertEqual(redone["children"], [])
            self.assertTrue(fts_is_consistent(database))



    def test_undo_redo_restore_sibling_order_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "A\n\nB\n\nC")
            initial_ids = [node["id"] for node in created["children"]]
            inserted = apply_transaction(
                database, created["id"], [paragraph("B2", after_id=initial_ids[1])], created["revision"]
            )
            new_id = inserted["children"][2]["id"]
            undo_insert = undo_transaction(database, created["id"], inserted["revision"])
            self.assertEqual([node["id"] for node in undo_insert["children"]], initial_ids)
            redo_insert = redo_transaction(database, created["id"], undo_insert["revision"])
            self.assertEqual(
                [node["id"] for node in redo_insert["children"]],
                [*initial_ids[:2], new_id, initial_ids[2]],
            )

            moved = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="move", block_id=initial_ids[0], after_id=initial_ids[2])],
                redo_insert["revision"],
            )
            moved_ids = [node["id"] for node in moved["children"]]
            self.assertEqual(moved_ids, [initial_ids[1], new_id, initial_ids[2], initial_ids[0]])
            undo_move = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual(
                [node["id"] for node in undo_move["children"]], [*initial_ids[:2], new_id, initial_ids[2]]
            )
            redo_move = redo_transaction(database, created["id"], undo_move["revision"])
            self.assertEqual([node["id"] for node in redo_move["children"]], moved_ids)

            deleted = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="delete", block_id=moved_ids[1])],
                redo_move["revision"],
            )
            # The freed slot is reused by an insert between the same neighbours...
            refilled = apply_transaction(
                database, created["id"], [paragraph("again", after_id=moved_ids[0])], deleted["revision"]
            )
            undo_refill = undo_transaction(database, created["id"], refilled["revision"])
            undo_delete = undo_transaction(database, created["id"], undo_refill["revision"])
            # ...and undoing both puts the original block back in its own slot.
            self.assertEqual([node["id"] for node in undo_delete["children"]], moved_ids)


    def test_nested_move_undo_redo_restores_sibling_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Draft", "rough", "Parent")
            parent_id = created["children"][0]["id"]
            inserted = apply_transaction(
                database,
                created["id"],
                [
                    paragraph("A", block_id="550e8400-e29b-41d4-a716-446655440001", parent_id=parent_id),
                    paragraph("B", block_id="550e8400-e29b-41d4-a716-446655440002", parent_id=parent_id),
                    paragraph("C", block_id="550e8400-e29b-41d4-a716-446655440003", parent_id=parent_id),
                ],
                created["revision"],
            )
            original_ids = [node["id"] for node in inserted["children"][0]["children"]]
            moved = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="move", block_id=original_ids[0], parent_id=parent_id)],
                inserted["revision"],
            )
            moved_ids = [node["id"] for node in moved["children"][0]["children"]]
            self.assertEqual(moved_ids, [original_ids[1], original_ids[2], original_ids[0]])
            undone = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual([node["id"] for node in undone["children"][0]["children"]], original_ids)
            redone = redo_transaction(database, created["id"], undone["revision"])
            self.assertEqual([node["id"] for node in redone["children"][0]["children"]], moved_ids)
            # Moving a block to the top level, before its old parent.
            out = apply_transaction(
                database, created["id"],
                [BlockOperation(operation="move", block_id=original_ids[1], before_id=parent_id)],
                redone["revision"],
            )
            self.assertEqual([node["id"] for node in out["children"]], [original_ids[1], parent_id])
            with self.assertRaisesRegex(ValueError, "descendant"):
                apply_transaction(
                    database, created["id"],
                    [BlockOperation(operation="move", block_id=parent_id, parent_id=original_ids[2])],
                    out["revision"],
                )


    def test_user_attributes_round_trip_update_separately_from_rendering_props(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database, "user-attrs", "User attributes", "rough", "",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            empty = document_tree(database, "user-attrs")
            assert empty is not None
            self.assertEqual(empty["children"], [])
            inserted = apply_transaction(
                database,
                "user-attrs",
                [
                    BlockOperation(
                        operation="insert", type="heading", attrs={"level": 2},
                        user_attrs={"topic": "plan", "labels": ["work"]}, content=runs("Heading"),
                    ),
                    BlockOperation(
                        operation="insert", type="image",
                        attrs={"url": "/media/picture.png", "width": 320},
                        user_attrs={"purpose": "cover"},
                    ),
                ],
                empty["revision"],
            )
            heading_id = inserted["children"][0]["id"]

            updated = apply_transaction(
                database,
                "user-attrs",
                [BlockOperation(operation="update", block_id=heading_id,
                                user_attrs={"topic": "final", "labels": None})],
                inserted["revision"],
            )
            heading, image = updated["children"]

            self.assertEqual(heading["user_attrs"], {"topic": "final"})
            self.assertEqual(heading["attrs"], {"level": 2})
            self.assertEqual(image["user_attrs"], {"purpose": "cover"})
            self.assertEqual(image["attrs"], {"url": "/media/picture.png", "width": 320})
            self.assertEqual(document_tree(database, "user-attrs"), updated)




    def test_failed_structural_operation_rolls_back_the_whole_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "one", "One", "rough", "A\n\nB", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
            before = document_tree(database, "one"); assert before is not None
            with self.assertRaises(KeyError):
                apply_transaction(database, "one", [paragraph("C"), BlockOperation(operation="move", block_id="missing")], before["revision"])
            after = document_tree(database, "one"); assert after is not None
            self.assertEqual([node["text"] for node in after["children"]], ["A", "B"])
            self.assertEqual(after["revision"], before["revision"])


    def test_transactions_require_and_increment_document_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database, "one", "One", "rough", "A\n\nB",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            before = document_tree(database, "one")
            assert before is not None
            self.assertEqual(before["revision"], 0)

            updated = apply_transaction(
                database, "one", [paragraph("C"), paragraph("D")], before["revision"]
            )
            self.assertEqual(updated["revision"], 1)
            self.assertEqual([block["text"] for block in updated["children"]], ["A", "B", "C", "D"])
            self.assertEqual({block["revision"] for block in updated["children"][2:]}, {1})
            self.assertEqual({block["revision"] for block in updated["children"][:2]}, {None})

            with self.assertRaises(DocumentRevisionConflict) as raised:
                apply_transaction(database, "one", [paragraph("must not be inserted")], before["revision"])
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
            self.assertEqual([block["text"] for block in after["children"]], ["A", "B", "C", "D"])


    def test_stale_revision_protects_concurrent_block_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database, "one", "One", "rough", "A\n\nB\n\nC",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            initial = document_tree(database, "one")
            assert initial is not None
            block_ids = [block["id"] for block in initial["children"]]

            first_move = apply_transaction(
                database, "one",
                [BlockOperation(operation="move", block_id=block_ids[0], after_id=block_ids[2])],
                initial["revision"],
            )
            self.assertEqual([block["text"] for block in first_move["children"]], ["B", "C", "A"])
            with self.assertRaises(DocumentRevisionConflict):
                apply_transaction(
                    database, "one",
                    [BlockOperation(operation="move", block_id=block_ids[1], after_id=block_ids[2])],
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
                database, "receipt-note", "Receipt note", "rough", "Original",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            initial = document_tree(database, "receipt-note")
            assert initial is not None
            block_id = initial["children"][0]["id"]
            operation = BlockOperation(operation="update", block_id=block_id, content=runs("Saved once"))
            first = apply_transaction(database, "receipt-note", [operation], 0, "tx-1")
            # A fresh initialization/connection models replay after server restart.
            initialize(database)
            replay = apply_transaction(database, "receipt-note", [operation], 0, "tx-1")
            self.assertEqual(replay, first)
            self.assertEqual(document_tree(database, "receipt-note")["revision"], 1)
            with self.assertRaisesRegex(ValueError, "already used"):
                apply_transaction(
                    database, "receipt-note",
                    [BlockOperation(operation="update", block_id=block_id, content=runs("Different"))],
                    0, "tx-1",
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
                database, "legacy-filename-id", "Legacy", "rough", "",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            before = document_tree(database, "legacy-filename-id")
            assert before is not None

            after = apply_transaction(
                database, "legacy-filename-id", [paragraph("New block")], before["revision"]
            )

            block_id = after["children"][-1]["id"]
            self.assertRegex(block_id, r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
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
                ["heading", "check_item", "quote", "code"],
            )
            parent = tree["children"][1]
            self.assertEqual(parent["attrs"], {"checked": True})
            self.assertEqual(parent["text"], "Parent with link")
            self.assertEqual(parent["content"][1], {
                "type": "link", "href": "https://example.com",
                "content": [{"type": "text", "text": "link"}],
            })
            child = parent["children"][0]
            self.assertEqual((child["type"], child["text"]), ("bullet_item", "Child with"))
            self.assertEqual(
                [(nested["type"], nested["attrs"].get("url"), nested["text"]) for nested in child["children"]],
                [("image", "/media/image.png", "image"), ("bullet_item", None, "Grandchild")],
            )
            quote = tree["children"][2]
            self.assertEqual([(node["type"], node["text"]) for node in quote["children"]],
                             [("paragraph", "A quote")])


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
            self.assertEqual(child_hits[0]["text"], "Child phrase")
            self.assertEqual(search_blocks(database, "not-present"), [])


    def test_rebuilds_stale_fts_rows_from_active_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                "Indexed text",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            with connection_scope(database) as connection:
                connection.execute("DELETE FROM blocks_fts")
            self.assertFalse(fts_is_consistent(database))
            self.assertTrue(ensure_fts_integrity(database))
            self.assertEqual(len(search_blocks(database, "Indexed")), 1)

            # Trashing drops the document from the index straight away.
            delete_document(database, "one")
            self.assertTrue(fts_is_consistent(database))
            self.assertEqual(search_blocks(database, "Indexed"), [])
            rebuild_fts(database)
            self.assertTrue(fts_is_consistent(database))

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
            # A reference preview looks a block up by its ID.
            second = block_link_targets(database, "second")[0]
            by_id = block_link_targets(database, str(second["block_id"]), limit=1)
            self.assertEqual([target["text"] for target in by_id], ["Second block"])

            # A reference's own block ID finds that block first (for reference previews).
            second_id = block_link_targets(database, "second")[0]["block_id"]
            by_id = block_link_targets(database, second_id, limit=1)
            self.assertEqual([target["text"] for target in by_id], ["Second block"])

    def test_rebuilds_block_and_document_backlinks_in_the_same_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            create_document(
                database, "target", "Target", "rough", "Target block",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            target = document_tree(database, "target")
            assert target is not None
            target_block_id = target["children"][0]["id"]
            create_document(
                database, "source", "Source", "rough", f"(( {target_block_id} \"block label\" )) and [[target|document label]]",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )

            source = document_tree(database, "source")
            self.assertEqual(source["children"][0]["content"], [
                {"type": "blockRef", "id": target_block_id},
                {"type": "text", "text": " and "},
                {"type": "docLink", "id": "target"},
            ])
            # Labels are the targets' current text and title, never the stored ones.
            self.assertEqual(source["children"][0]["text"], "Target block and Target")
            self.assertEqual(set(source["resolved_links"]["documents"]), {"target"})
            backlinks = document_backlinks(database, "target")
            self.assertEqual(len(backlinks), 2)
            self.assertEqual({backlink["source_document_id"] for backlink in backlinks}, {"source"})

            rename_document(database, "target", "Renamed")
            self.assertEqual(
                document_tree(database, "source")["children"][0]["text"], "Target block and Renamed"
            )

            apply_transaction(
                database, "source",
                [BlockOperation(operation="update", block_id=source["children"][0]["id"],
                                content=runs("No links now"))],
                source["revision"],
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
                connection.execute(
                    """CREATE TABLE blocks (
                           id TEXT PRIMARY KEY,
                           document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                           parent_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
                           position INTEGER NOT NULL,
                           type TEXT NOT NULL,
                           attrs_json TEXT NOT NULL DEFAULT '{}',
                           content_json TEXT NOT NULL DEFAULT '{}',
                           text TEXT NOT NULL DEFAULT '',
                           created_at TEXT NOT NULL,
                           updated_at TEXT NOT NULL,
                           UNIQUE (document_id, parent_id, position))"""
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
                connection.executemany(
                    """INSERT INTO blocks(id, document_id, parent_id, position, type, content_json,
                           text, created_at, updated_at)
                       VALUES (?, 'older', NULL, ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
                    [
                        ("b1", 0, "heading", '{"markdown": "## Hello **world**"}', "## Hello **world**"),
                        ("b2", 1, "list", '{"markdown": "- [x] done"}', "- [x] done"),
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
                self.assertEqual([row["version"] for row in versions], list(range(2, SCHEMA_VERSION + 1)))
                tables = {
                    row["name"] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {"document_refs", "folders", "block_user_attrs", "block_assets", "block_sync"} <= tables
                )
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(documents)")
                }
                self.assertTrue({"folder_id", "position", "deleted_at", "revision"} <= columns)
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
            tree = document_tree(database, "older")
            self.assertEqual(
                [(node["id"], node["type"], node["attrs"], node["text"]) for node in tree["children"]],
                [("b1", "heading", {"level": 2}, "Hello world"), ("b2", "check_item", {"checked": True}, "done")],
            )


    def test_navigation_excludes_soft_deleted_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "one",
                "One",
                "rough",
                "First",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            create_document(
                database,
                "two",
                "Two",
                "polished",
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
                "First",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            create_document(
                database,
                "two",
                "Two",
                "rough",
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
                database, "one",
                [BlockOperation(operation="update", block_id=block_id, content=runs("Updated"))],
                tree["revision"],
            )
            from app.documents import mirror_document

            mirror_document(database, root, "one")

            # The format-1 mirror "one.md" becomes "One.md" (named after the title).
            self.assertFalse((root / "one.md").exists() and not (root / "one.md").samefile(root / "One.md"))
            mirrored = (root / "One.md").read_text(encoding="utf-8")
            self.assertIn("custom: keep", mirrored)
            self.assertNotIn("tags:", mirrored)
            self.assertIn("\nUpdated\n", mirrored)
            self.assertNotIn("Original", mirrored)






if __name__ == "__main__":
    unittest.main()
