import tempfile
import unittest
from pathlib import Path

from app.block_store import create_document, document_tree
from app.portability import (
    export_all_markdown,
    export_document_markdown,
    import_markdown_directory,
)


class PortabilityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
