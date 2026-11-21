"""Regression tests for storage-core bugs (block moves, positions, trash, initialize)."""

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import block_store, documents
from app.block_store import (
    apply_transaction,
    block_link_targets,
    connection_scope,
    create_block_document,
    create_document,
    create_folder,
    delete_document,
    document_backlinks,
    document_tree,
    fts_is_consistent,
    SCHEMA_VERSION,
    initialize,
    move_document,
    navigation,
    register_asset,
    render_markdown,
    store_asset_text,
)
from app.corpus import embedding_documents
from app.models import BlockOperation

_T = "2026-01-01T00:00:00+00:00"


def _shape(nodes: list[dict]) -> list[tuple[str, int, list]]:
    return [(node["text"], node["position"], _shape(node["children"])) for node in nodes]


class BlockMoveTests(unittest.TestCase):
    def test_outdenting_a_middle_child_keeps_sibling_positions_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(
                database, "T", "- a\n  - c0\n  - c1\n  - c2\n- b\n"
            )
            c1 = created["children"][0]["children"][1]["id"]

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=c1, after_id=created["children"][0]["id"])],
                created["revision"],
            )

            self.assertEqual(
                _shape(moved["children"]),
                [
                    ("a", 0, [("c0", 0, []), ("c2", 1, [])]),
                    ("c1", 1, []),
                    ("b", 2, []),
                ],
            )

    def test_inserting_an_existing_block_id_is_a_conflict_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "one")
            other = create_block_document(database, "O", "two")
            for target, existing in (
                (created, created["children"][0]["id"]),
                (other, created["children"][0]["id"]),
            ):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    apply_transaction(
                        database,
                        target["id"],
                        [BlockOperation(operation="insert", block_id=existing, type="paragraph")],
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
                database, "Zebra facts", "- parent\n  - child\n"
            )

            targets = block_link_targets(database, "zebra")

            self.assertEqual(
                [target["block_id"] for target in targets], [created["children"][0]["id"]]
            )


class TrashRestoreTests(unittest.TestCase):
    def test_restore_recovers_links_written_while_the_document_was_in_the_trash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(database, "target", "Target", "Target block", _T, _T)
            target = document_tree(database, "target")
            assert target is not None
            block_id = target["children"][0]["id"]
            delete_document(database, "target")
            create_document(
                database, "source", "Source", f"See [[target]] and [[#^{block_id}]]", _T, _T,
            )

            documents.restore_document(database, "target")

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
                database, "Pic", f"![x](/media/{media.name})"
            )
            delete_document(database, document["id"])

            store_asset_text(database, image_hash, "late OCR text")

            self.assertTrue(fts_is_consistent(database))


class EmbeddingCorpusTests(unittest.TestCase):
    def test_corpus_cache_sees_edits_to_notes_with_future_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            future = "2999-01-01T00:00:00+00:00"
            create_document(database, "imported", "Imported", "old words\n\nother", future, future)
            create_document(database, "second", "Second", "second", future, future)
            tree = document_tree(database, "imported")
            assert tree is not None
            self.assertEqual([item[3] for item in embedding_documents(database)], ["old words", "other", "second"])

            apply_transaction(
                database,
                "imported",
                [BlockOperation(
                    operation="update", block_id=tree["children"][0]["id"], content=[{"type": "text", "text": "new words"}]
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
            created = create_block_document(database, "T", body)

            first = created["children"][0]
            self.assertEqual((first["type"], first["text"]), ("check_item", "first paragraph"))
            self.assertEqual(first["attrs"], {"checked": False})
            self.assertEqual(
                [(child["type"], child["text"]) for child in first["children"]],
                [
                    ("paragraph", "second paragraph"),
                    ("heading", "heading inside"),
                    ("html", "raw html"),
                    ("bullet_item", "nested"),
                ],
            )
            self.assertEqual(created["children"][1]["text"], "next item")
            rendered = render_markdown(created["children"])
            for text in ("first paragraph", "second paragraph", "heading inside", "raw html"):
                self.assertIn(text, rendered)
            # The export imports back to the same shape.
            again = create_block_document(database, "T2", rendered)
            self.assertEqual(
                render_markdown(again["children"]),
                render_markdown(created["children"]),
            )

    def test_unicode_line_separators_do_not_shift_block_text(self) -> None:
        # str.splitlines() also splits on U+2028, form feed, etc.; Markdown does not.
        body = "a\u2028b\n\nsecond\n\nx\x0cy\n\nthird\r\n\r\nfourth\n"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", body)

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
                create_block_document(database, f"N{i}", "x")["id"]
                for i in range(8)
            ]

            errors = _run_together(
                16,
                lambda i: create_block_document(database, f"New{i}", "x")
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


class InitializeTests(unittest.TestCase):
    def test_concurrent_first_use_of_a_new_database_does_not_fail(self) -> None:
        for _ in range(10):
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / ".fortress.sqlite3"

                errors = _run_together(8, lambda _index, path=database: initialize(path))

                self.assertEqual(errors, [])
                with connection_scope(database) as connection:
                    versions = connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(len(versions), 1)
                self.assertEqual(mode, "wal")

    def _database_at(self, directory: str, version: int) -> Path:
        """A database written by an earlier build, recorded at schema ``version``."""

        database = Path(directory) / ".fortress.sqlite3"
        block_store.create_block_document(database, "Kept", "Some text")
        with connection_scope(database) as connection:
            connection.execute("UPDATE schema_migrations SET version = ?", (version,))
        block_store._ready_files.pop(database, None)
        return database

    def test_a_schema_14_database_is_refused_with_how_to_upgrade_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database_at(directory, 14)
            with self.assertRaises(block_store.WorkspaceTooOld) as raised:
                initialize(database)
            message = str(raised.exception)
            self.assertIn("schema version 14", message)
            self.assertIn("last-migrating-release", message)
            with connection_scope(database) as connection:  # left as it was
                version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertEqual(version, 14)

    def test_a_schema_15_database_not_yet_cleaned_up_is_refused(self) -> None:
        # What last-migrating-release removes at startup, keeping the database at 15.
        for statement in (
            "CREATE TABLE block_sync (block_id TEXT PRIMARY KEY, source_hash TEXT)",
            "ALTER TABLE blocks ADD COLUMN provenance_json TEXT",
            "ALTER TABLE documents ADD COLUMN status TEXT",
            "ALTER TABLE documents ADD COLUMN tags_json TEXT",
            "CREATE TABLE document_history (id INTEGER PRIMARY KEY)",
        ):
            with self.subTest(statement), tempfile.TemporaryDirectory() as directory:
                database = self._database_at(directory, 15)
                with connection_scope(database) as connection:
                    connection.execute(statement)
                block_store._ready_files.pop(database, None)
                with self.assertRaisesRegex(block_store.WorkspaceTooOld, "schema version 15"):
                    initialize(database)

    def test_notes_without_a_schema_version_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            with connection_scope(database) as connection:
                connection.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT)")
            with self.assertRaises(block_store.WorkspaceTooOld):
                initialize(database)

    def test_a_cleaned_up_schema_15_database_opens_and_is_recorded_as_16(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 16)
        for version in (15, 16):
            with self.subTest(version), tempfile.TemporaryDirectory() as directory:
                database = self._database_at(directory, version)
                initialize(database)
                self.assertEqual([item["title"] for item in block_store.list_documents(database)], ["Kept"])
                with connection_scope(database) as connection:
                    recorded = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                self.assertEqual(recorded, 16)

    def test_startup_recreates_every_trigger_and_drops_retired_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database_at(directory, SCHEMA_VERSION)
            with connection_scope(database) as connection:
                # An older build's body for a current trigger, and a retired one.
                connection.execute("DROP TRIGGER folders_changed_insert")
                connection.execute(
                    "CREATE TRIGGER folders_changed_insert AFTER INSERT ON folders BEGIN SELECT 1; END"
                )
                connection.execute(
                    "CREATE TRIGGER retired_trigger AFTER INSERT ON folders BEGIN SELECT 1; END"
                )
                connection.execute("CREATE TABLE retired_table (id INTEGER)")
            block_store._ready_files.pop(database, None)
            with patch.object(block_store, "_RETIRED", ("DROP TABLE IF EXISTS retired_table",)):
                initialize(database)
            with connection_scope(database) as connection:
                names = {row[0]: row[1] for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'table')"
                )}
                seq = connection.execute("SELECT seq FROM change_log").fetchone()[0]
            self.assertNotIn("retired_trigger", names)
            self.assertNotIn("retired_table", names)
            self.assertLessEqual(set(block_store._CHANGE_TRIGGERS), set(names))
            block_store.create_folder(database, "F")
            with connection_scope(database) as connection:
                self.assertEqual(connection.execute("SELECT seq FROM change_log").fetchone()[0], seq + 1)

    def test_the_retired_user_attributes_table_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database_at(directory, SCHEMA_VERSION)
            with connection_scope(database) as connection:
                connection.execute(
                    "CREATE TABLE block_user_attrs (block_id TEXT, name TEXT, value TEXT)"
                )
            block_store._ready_files.pop(database, None)
            initialize(database)
            with connection_scope(database) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )}
            self.assertNotIn("block_user_attrs", tables)
            self.assertEqual([item["title"] for item in block_store.list_documents(database)], ["Kept"])

    def test_a_backup_of_an_older_schema_is_not_restored(self) -> None:
        from app import backups

        with tempfile.TemporaryDirectory() as directory:
            database = self._database_at(directory, 14)
            with self.assertRaisesRegex(ValueError, "last-migrating-release"):
                backups._verify_snapshot(database)

    def test_a_newer_database_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database_at(directory, SCHEMA_VERSION + 1)
            with self.assertRaisesRegex(RuntimeError, "Unsupported block database version"):
                initialize(database)

    def test_a_new_workspace_starts_with_the_welcome_note_once(self) -> None:
        from fastapi.testclient import TestClient

        from app import main
        from support import make_settings, use_settings

        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory), welcome_note=True)
            with use_settings(settings):
                with TestClient(main.app):
                    pass
                [note] = block_store.list_documents(settings.block_db_path)
                self.assertEqual(note["title"], "Welcome to Fortress Notes")
                tree = block_store.document_tree(settings.block_db_path, note["id"])
                self.assertEqual(tree["children"][0]["text"], "Welcome to Fortress Notes")
                block_store.delete_document(settings.block_db_path, note["id"])
                with TestClient(main.app):
                    pass  # an existing workspace is never seeded again
                self.assertEqual(block_store.list_documents(settings.block_db_path), [])

    def test_the_backend_does_not_start_on_an_old_database_and_says_why(self) -> None:
        from fastapi.testclient import TestClient

        from app import main
        from support import make_settings, use_settings

        with tempfile.TemporaryDirectory() as directory:
            self._database_at(directory, 14)
            with use_settings(make_settings(Path(directory))), \
                    self.assertLogs("fortress", "ERROR") as logged, \
                    self.assertRaises(block_store.WorkspaceTooOld):
                with TestClient(main.app):
                    pass
            self.assertIn(main.STARTUP_ERROR_PREFIX + "This notes database", logged.output[0])


if __name__ == "__main__":
    unittest.main()
