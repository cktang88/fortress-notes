import tempfile
import unittest
from pathlib import Path

from app.block_store import create_document, document_tree
from app.portability import export_document_markdown

from support import import_markdown_files


class PortabilityTests(unittest.TestCase):
    def test_exports_plain_markdown_with_the_note_id_in_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            create_document(
                database,
                "guide",
                "Guide",
                "# Guide\n\n- Parent\n  - Child\n\nAfter",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )

            markdown = export_document_markdown(database, "guide")
            tree = document_tree(database, "guide")
            assert tree is not None

            self.assertIn("title: Guide", markdown)
            self.assertIn("  - Child", markdown)
            # Plain Markdown: the note's id is in its front matter, and no block
            # carries one (nothing refers to them).
            self.assertIn("\nid: guide\n", markdown)
            self.assertIn("\n# Guide\n\n- Parent\n  - Child\n\nAfter\n", markdown)
            self.assertNotIn("fortress:block", markdown)
            self.assertNotIn(tree["children"][0]["id"], markdown)

    def test_export_import_round_trip_preserves_document_and_block_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_database = root / "source.sqlite3"
            imported_database = root / "imported.sqlite3"
            create_document(
                source_database,
                "round-trip",
                "Round trip",
                "# Heading\n\nParagraph with **formatting**.\n\n- First\n  - Nested\n- Second",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            )
            markdown = export_document_markdown(source_database, "round-trip")
            source_tree = document_tree(source_database, "round-trip")
            assert source_tree is not None
            (root / "round-trip.md").write_text(markdown, encoding="utf-8")

            [imported_id] = import_markdown_files(imported_database, root)

            imported_tree = document_tree(imported_database, imported_id)
            assert imported_tree is not None
            self.assertEqual(imported_tree["title"], source_tree["title"])
            # Tags were removed from the product: not exported, ignored on import.
            self.assertNotIn("tags:", markdown)
            self.assertNotIn("tags", imported_tree)
            self.assertEqual(
                _block_shape(imported_tree["children"]),
                _block_shape(source_tree["children"]),
            )

    def test_empty_document_imports_with_one_empty_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_database = root / "source.sqlite3"
            imported_database = root / "imported.sqlite3"
            create_document(
                source_database,
                "empty",
                "Empty",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )
            (root / "empty.md").write_text(
                export_document_markdown(source_database, "empty"), encoding="utf-8"
            )

            [imported_id] = import_markdown_files(imported_database, root)

            imported_tree = document_tree(imported_database, imported_id)
            assert imported_tree is not None
            self.assertEqual(imported_tree["title"], "Empty")
            # Something to type into, created at import (reads never write).
            self.assertEqual(
                [(block["type"], block["text"]) for block in imported_tree["children"]],
                [("paragraph", "")],
            )


class NestedBlockRoundTripTests(unittest.TestCase):
    def test_blocks_inside_list_items_keep_their_indentation_across_round_trips(self) -> None:
        body = (
            "- Install\n\n"
            "  ```bash\n  pip install x\n      indented arg\n  ```\n\n"
            "  A second paragraph\n  over two lines\n\n"
            "  - Nested item\n\n"
            "    ```\n    deep code\n    ```"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_document(
                root / "0.sqlite3", "doc", "Doc", body,
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            )
            first_tree = document_tree(root / "0.sqlite3", "doc")
            first = _body(export_document_markdown(root / "0.sqlite3", "doc"))
            markdown = first
            for round_trip in range(1, 4):
                source = root / f"source{round_trip}"
                source.mkdir()
                (source / "doc.md").write_text(markdown, encoding="utf-8")
                database = root / f"{round_trip}.sqlite3"
                [document_id] = import_markdown_files(database, source)
                markdown = _body(export_document_markdown(database, document_id))
                self.assertEqual(markdown, first, f"round trip {round_trip}")
            tree = document_tree(database, document_id)
            assert tree is not None and first_tree is not None
            self.assertEqual(_block_shape(tree["children"]), _block_shape(first_tree["children"]))
            item = first_tree["children"][0]
            self.assertEqual(
                [child["text"] for child in item["children"][:2]],
                ["pip install x\n    indented arg", "A second paragraph\nover two lines"],
            )
            self.assertEqual(item["children"][0]["attrs"], {"language": "bash"})
            self.assertEqual(item["children"][2]["children"][0]["text"], "deep code")


def _body(markdown: str) -> str:
    """A Markdown file without its front matter."""

    return markdown.split("\n---\n", 1)[1]


def _block_shape(blocks: list[dict]) -> list[tuple[str, str, list]]:
    return [
        (block["type"], block["text"], _block_shape(block["children"]))
        for block in blocks
    ]


if __name__ == "__main__":
    unittest.main()
