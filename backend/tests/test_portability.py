import tempfile
import unittest
from pathlib import Path

from app.block_store import create_document, delete_document, document_tree
from app.portability import (
    export_all_markdown,
    export_document_markdown,
    import_markdown_directory,
    preview_markdown_directory,
)


class PortabilityTests(unittest.TestCase):
    def test_preview_reports_ready_skipped_conflict_and_malformed_files_without_importing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            create_document(
                database,
                "existing",
                "Existing",
                "rough",
                [],
                "Stored body",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            create_document(
                database,
                "deleted",
                "Deleted",
                "rough",
                [],
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            delete_document(database, "deleted")
            (root / "new.md").write_text("A new document\n", encoding="utf-8")
            (root / "existing.md").write_text("Should be skipped\n", encoding="utf-8")
            (root / "deleted.md").write_text("Cannot replace deleted document\n", encoding="utf-8")
            (root / "broken.md").write_bytes(b"\xff not valid UTF-8\n")

            preview = preview_markdown_directory(database, root)

            self.assertEqual(
                [(item.document_id, item.status) for item in preview.items],
                [
                    ("broken", "error"),
                    ("deleted", "conflict"),
                    ("existing", "skipped"),
                    ("new", "ready"),
                ],
            )
            self.assertEqual((preview.ready_count, preview.skipped_count), (1, 1))
            self.assertEqual((preview.conflict_count, preview.error_count), (1, 1))
            self.assertIsNone(document_tree(database, "new"))
            self.assertFalse((root / ".fortress-import-backups").exists())

    def test_exports_nested_blocks_without_sqlite_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "guide",
                "Guide",
                "polished",
                ["docs"],
                "# Guide\n\n- Parent\n  - Child\n\nAfter",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )

            markdown = export_document_markdown(database, "guide")
            tree = document_tree(database, "guide")
            assert tree is not None

            self.assertIn("title: Guide", markdown)
            self.assertIn("  - Child", markdown)
            self.assertNotIn(tree["children"][0]["id"], markdown)
            self.assertEqual(set(export_all_markdown(database)), {"guide"})

    def test_export_import_round_trip_preserves_document_and_block_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_database = root / "source.sqlite3"
            imported_database = root / "imported.sqlite3"
            create_document(
                source_database,
                "round-trip",
                "Round trip",
                "polished",
                ["portable", "notes"],
                "# Heading\n\nParagraph with **formatting**.\n\n- First\n  - Nested\n- Second",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )
            markdown = export_document_markdown(source_database, "round-trip")
            source_tree = document_tree(source_database, "round-trip")
            assert source_tree is not None
            (root / "round-trip.md").write_text(markdown, encoding="utf-8")

            report = import_markdown_directory(imported_database, root)

            self.assertEqual(report.imported_count, 1)
            imported_tree = document_tree(imported_database, "round-trip")
            assert imported_tree is not None
            self.assertEqual(imported_tree["title"], source_tree["title"])
            self.assertEqual(imported_tree["status"], source_tree["status"])
            self.assertEqual(imported_tree["tags"], source_tree["tags"])
            self.assertEqual(
                _block_shape(imported_tree["children"]),
                _block_shape(source_tree["children"]),
            )

    def test_empty_document_exports_and_imports_with_no_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_database = root / "source.sqlite3"
            imported_database = root / "imported.sqlite3"
            create_document(
                source_database,
                "empty",
                "Empty",
                "rough",
                [],
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            (root / "empty.md").write_text(
                export_document_markdown(source_database, "empty"), encoding="utf-8"
            )

            report = import_markdown_directory(imported_database, root)

            self.assertEqual(report.imported_count, 1)
            imported_tree = document_tree(imported_database, "empty")
            assert imported_tree is not None
            self.assertEqual(imported_tree["title"], "Empty")
            self.assertEqual(imported_tree["children"], [])

    def test_import_creates_backups_and_preserves_originals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            original = "---\ntitle: Imported\ntags: [portable]\n---\n# Imported\n\nBody\n"
            source = root / "imported.md"
            source.write_text(original, encoding="utf-8")

            report = import_markdown_directory(database, root)

            self.assertEqual(report.imported_count, 1)
            self.assertEqual(report.error_count, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual((report.backup_directory / "imported.md").read_text(encoding="utf-8"), original)
            tree = document_tree(database, "imported")
            assert tree is not None
            self.assertEqual(tree["title"], "Imported")

    def test_repeated_import_reports_existing_document_and_makes_new_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            source = root / "one.md"
            source.write_text("First body\n", encoding="utf-8")

            first = import_markdown_directory(database, root)
            source.write_text("Changed source body\n", encoding="utf-8")
            second = import_markdown_directory(database, root)

            self.assertEqual(first.imported_count, 1)
            self.assertEqual(second.skipped_count, 1)
            self.assertNotEqual(first.backup_directory, second.backup_directory)
            self.assertEqual(
                (second.backup_directory / "one.md").read_text(encoding="utf-8"),
                "Changed source body\n",
            )
            tree = document_tree(database, "one")
            assert tree is not None
            self.assertEqual(tree["children"][0]["text"], "First body")

    def test_malformed_input_is_reported_and_source_is_left_intact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            source = root / "broken.md"
            original = b"\xff not valid UTF-8\n"
            source.write_bytes(original)

            report = import_markdown_directory(database, root)

            self.assertEqual(report.error_count, 1)
            self.assertEqual(report.items[0].document_id, "broken")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual((report.backup_directory / "broken.md").read_bytes(), original)
            self.assertIsNone(document_tree(database, "broken"))

    def test_malformed_frontmatter_is_reported_and_source_is_left_intact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".fortress.sqlite3"
            source = root / "broken-frontmatter.md"
            original = b"---\ntitle: [unterminated\n---\nBody\n"
            source.write_bytes(original)

            report = import_markdown_directory(database, root)

            self.assertEqual(report.error_count, 1)
            self.assertEqual(report.items[0].document_id, "broken-frontmatter")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(
                (report.backup_directory / "broken-frontmatter.md").read_bytes(), original
            )
            self.assertIsNone(document_tree(database, "broken-frontmatter"))


def _block_shape(blocks: list[dict]) -> list[tuple[str, str, list]]:
    return [
        (block["type"], block["text"], _block_shape(block["children"]))
        for block in blocks
    ]


if __name__ == "__main__":
    unittest.main()
