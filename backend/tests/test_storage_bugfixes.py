"""Regression tests for storage-core bugs (block moves, positions, trash, migrations)."""

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from app import workspace
from app.block_store import (
    apply_transaction,
    connection_scope,
    create_block_document,
    create_document,
    delete_document,
    document_backlinks,
    document_tree,
    embedding_documents,
    fts_is_consistent,
    initialize,
    register_asset,
    render_markdown,
    store_asset_text,
    undo_transaction,
)
from app.models import BlockOperation

_T = "2026-01-01T00:00:00+00:00"


def _shape(nodes: list[dict]) -> list[tuple[str, int, list]]:
    return [(node["text"], node["position"], _shape(node["children"])) for node in nodes]


class BlockMoveTests(unittest.TestCase):
    def test_outdenting_a_middle_child_keeps_sibling_positions_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(
                database, "T", "rough", [], "- a\n  - c0\n  - c1\n  - c2\n- b\n"
            )
            c1 = created["children"][0]["children"][1]["id"]

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=c1, parent_id=None, position=1)],
                created["revision"],
            )

            self.assertEqual(
                _shape(moved["children"]),
                [
                    ("- a", 0, [("- c0", 0, []), ("- c2", 1, [])]),
                    ("- c1", 1, []),
                    ("- b", 2, []),
                ],
            )
            undone = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual(_shape(undone["children"]), _shape(created["children"]))

    def test_positions_past_the_end_append_without_leaving_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], "one\n\ntwo\n")
            first = created["children"][0]["id"]

            inserted = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="insert", position=50, text="three")],
                created["revision"],
            )
            self.assertEqual([n["position"] for n in inserted["children"]], [0, 1, 2])

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=first, position=99)],
                inserted["revision"],
            )
            self.assertEqual(
                [(n["text"], n["position"]) for n in moved["children"]],
                [("two", 0), ("three", 1), ("one", 2)],
            )

            duplicated = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="duplicate", block_id=first, position=99)],
                moved["revision"],
            )
            self.assertEqual([n["position"] for n in duplicated["children"]], [0, 1, 2, 3])

            # Contiguous positions keep "merge with next sibling" working.
            merged = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="merge", block_id=duplicated["children"][1]["id"])],
                duplicated["revision"],
            )
            self.assertEqual([n["text"] for n in merged["children"]], ["two", "threeone", "one"])


class TrashRestoreTests(unittest.TestCase):
    def test_restore_recovers_links_written_while_the_document_was_in_the_trash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "target", "Target", "rough", [], "Target block", _T, _T)
            target = document_tree(database, "target")
            assert target is not None
            block_id = target["children"][0]["id"]
            delete_document(database, "target")
            create_document(
                database, "source", "Source", "rough", [],
                f"See [[target]] and (({block_id}))", _T, _T,
            )

            workspace.restore_document(database, "target")

            self.assertEqual(
                {(link["source_document_id"], link["target_block_id"])
                 for link in document_backlinks(database, "target")},
                {("source", None), ("source", block_id)},
            )

    def test_image_text_arriving_for_a_trashed_note_keeps_it_out_of_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            media = root / "01ARZ3NDEKTSV4RRFFQ69G5FAV.png"
            media.write_bytes(b"image")
            image_hash = hashlib.sha256(b"image").hexdigest()
            register_asset(database, media, image_hash, "image/png")
            document = create_block_document(
                database, "Pic", "rough", [], f"![x](/media/{media.name})"
            )
            delete_document(database, document["id"])

            store_asset_text(database, image_hash, "late OCR text")

            self.assertTrue(fts_is_consistent(database))


class EmbeddingCorpusTests(unittest.TestCase):
    def test_corpus_cache_sees_edits_to_notes_with_future_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            future = "2999-01-01T00:00:00+00:00"
            create_document(database, "imported", "Imported", "rough", [], "old words\n\nother", future, future)
            create_document(database, "second", "Second", "rough", [], "second", future, future)
            tree = document_tree(database, "imported")
            assert tree is not None
            self.assertEqual([item[3] for item in embedding_documents(database)], ["old words", "other", "second"])

            apply_transaction(
                database,
                "imported",
                [BlockOperation(
                    operation="update", block_id=tree["children"][0]["id"], text="new words"
                )],
                tree["revision"],
            )

            self.assertEqual([item[3] for item in embedding_documents(database)], ["new words", "other", "second"])


class MarkdownImportTests(unittest.TestCase):
    def test_list_items_keep_every_block_they_contain(self) -> None:
        body = (
            "- [ ] first paragraph\n"
            "\n"
            "  second paragraph\n"
            "\n"
            "  ## heading inside\n"
            "\n"
            "  <div>raw html</div>\n"
            "\n"
            "  - nested\n"
            "- next item\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], body)

            first = created["children"][0]
            self.assertEqual(first["text"], "- [ ] first paragraph")
            self.assertEqual(first["attrs"], {"checked": False})
            self.assertEqual(
                [(child["type"], child["text"]) for child in first["children"]],
                [
                    ("paragraph", "second paragraph"),
                    ("heading", "## heading inside"),
                    ("raw", "<div>raw html</div>"),
                    ("list", "- nested"),
                ],
            )
            self.assertEqual(created["children"][1]["text"], "- next item")
            rendered = render_markdown(created["children"])
            for text in ("first paragraph", "second paragraph", "heading inside", "raw html"):
                self.assertIn(text, rendered)
            # The export imports back to the same shape.
            again = create_block_document(database, "T2", "rough", [], rendered)
            self.assertEqual(
                render_markdown(again["children"]), rendered
            )


class InitializeTests(unittest.TestCase):
    def test_concurrent_first_use_of_a_new_database_does_not_fail(self) -> None:
        for _ in range(10):
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / ".fortress.sqlite3"
                barrier = threading.Barrier(8)
                errors: list[BaseException] = []

                def first_use() -> None:
                    barrier.wait()
                    try:
                        initialize(database)
                    except BaseException as error:  # noqa: BLE001 - collected for the assertion
                        errors.append(error)

                threads = [threading.Thread(target=first_use) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                with connection_scope(database) as connection:
                    versions = connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(len(versions), 1)
                self.assertEqual(mode, "wal")


if __name__ == "__main__":
    unittest.main()
