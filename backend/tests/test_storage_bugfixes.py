"""Regression tests for storage-core bugs (block moves, positions, trash, migrations)."""

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import block_store, documents, workspace
from app.block_store import (
    apply_transaction,
    block_link_targets,
    connection_scope,
    create_block_document,
    create_document,
    create_folder,
    delete_document,
    move_document,
    navigation,
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

    def test_inserting_an_existing_block_id_is_a_conflict_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], "one")
            other = create_block_document(database, "O", "rough", [], "two")
            for target, existing in (
                (created, created["children"][0]["id"]),
                (other, created["children"][0]["id"]),
            ):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    apply_transaction(
                        database,
                        target["id"],
                        [BlockOperation(operation="insert", block_id=existing, text="x")],
                        target["revision"],
                    )
            tree = document_tree(database, created["id"])
            assert tree is not None
            self.assertEqual(tree["revision"], created["revision"])


class LinkTargetTests(unittest.TestCase):
    def test_title_match_offers_the_first_top_level_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(
                database, "Zebra facts", "rough", [], "- parent\n  - child\n"
            )

            targets = block_link_targets(database, "zebra")

            self.assertEqual(
                [target["block_id"] for target in targets], [created["children"][0]["id"]]
            )


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

    def test_unicode_line_separators_do_not_shift_block_text(self) -> None:
        # str.splitlines() also splits on U+2028, form feed, etc.; Markdown does not.
        body = "a\u2028b\n\nsecond\n\nx\x0cy\n\nthird\r\n\r\nfourth\n"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], body)

            self.assertEqual(
                [child["text"] for child in created["children"]],
                ["a\u2028b", "second", "x\x0cy", "third", "fourth"],
            )


def _run_together(count: int, work) -> list[BaseException]:
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def run(index: int) -> None:
        barrier.wait()
        try:
            work(index)
        except BaseException as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class ConcurrentOrganizationTests(unittest.TestCase):
    def test_simultaneous_creates_and_moves_get_distinct_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            folder = create_folder(database, "F")
            ids = [
                create_block_document(database, f"N{i}", "rough", [], "x")["id"]
                for i in range(8)
            ]

            errors = _run_together(
                16,
                lambda i: create_block_document(database, f"New{i}", "rough", [], "x")
                if i % 2
                else move_document(database, ids[i // 2], folder["id"], 0),
            )
            errors += _run_together(8, lambda i: create_folder(database, f"G{i}"))

            self.assertEqual(errors, [])
            items = navigation(database)["items"]

            def positions(nodes: list[dict], kind: str) -> list[int]:
                return [node["position"] for node in nodes if node["kind"] == kind]

            root_docs = positions(items, "document")
            root_folders = positions(items, "folder")
            folder_docs = positions(
                next(node for node in items if node["id"] == folder["id"])["children"],
                "document",
            )
            self.assertEqual(root_docs, list(range(8)))
            self.assertEqual(root_folders, list(range(9)))
            self.assertEqual(sorted(folder_docs), list(range(8)))


class MirrorRaceTests(unittest.TestCase):
    def test_a_slow_mirror_write_cannot_overwrite_a_newer_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            database = notes / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], "old text")
            real_tree = block_store.document_tree
            slow_read_done = threading.Event()
            release = threading.Event()
            calls = {"count": 0}

            def tree(path, document_id):
                result = real_tree(path, document_id)
                calls["count"] += 1
                if calls["count"] == 1:  # the first writer stalls after reading
                    slow_read_done.set()
                    release.wait(5)
                return result

            with patch.object(block_store, "document_tree", tree):
                slow = threading.Thread(
                    target=documents.mirror_document, args=(database, notes, created["id"])
                )
                slow.start()
                slow_read_done.wait(5)
                apply_transaction(
                    database,
                    created["id"],
                    [BlockOperation(
                        operation="update", block_id=created["children"][0]["id"],
                        text="new text",
                    )],
                    created["revision"],
                )
                fast = threading.Thread(
                    target=documents.mirror_document, args=(database, notes, created["id"])
                )
                fast.start()
                time.sleep(0.2)
                release.set()
                slow.join()
                fast.join()

            self.assertIn("new text", (notes / f"{created['id']}.md").read_text(encoding="utf-8"))


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
