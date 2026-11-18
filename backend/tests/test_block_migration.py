"""Schema 14: every legacy block shape becomes a canonical block.

``fixtures/legacy_v13.sql`` is a database written by the code before the block
model (schema 13): a Markdown import (with the quote that swallowed the rest of
the note), a note written through the editor's BlockNote-shaped transactions
(tables with nested blocks, columns, callouts, math, user attributes, links and
block references), and a OneNote-style source page with a conflict that was then
removed from its source. Search rows are left out: the migration rebuilds them.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import block_store, sources

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE_PAGE = [
    {"id": "P/1", "type": "paragraph", "inline": [{"text": "Hello ", "b": 1}, {"text": "world"}]},
    {"id": "P/2", "type": "quote", "inline": "Quoted text"},
    {"id": "P/3", "type": "table",
     "rows": [["a", "b"], [{"inline": "c", "background": "#FF0000"},
                           {"text": "d", "blocks": [{"type": "list", "text": "inner"}]}]],
     "column_widths": [100, 200], "borders": True},
    {"type": "paragraph", "text": "no id para"},
    {"id": "P/4", "type": "list", "checked": False, "text": "task"},
    {"id": "P/5", "type": "paragraph", "text": "changed in source"},
]


def _shape(nodes: list[dict]) -> list:
    return [
        (node["type"], node["attrs"], node["text"], _shape(node["children"]))
        if node["children"] else (node["type"], node["attrs"], node["text"])
        for node in nodes
    ]


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db = Path(self._directory.name) / ".fortress.sqlite3"
        with sqlite3.connect(self.db) as connection:
            connection.executescript((FIXTURES / "legacy_v13.sql").read_text(encoding="utf-8"))
        connection.close()
        block_store.initialize(self.db)
        self.ids = {item["title"]: item["id"] for item in block_store.list_documents(self.db)}

    def tree(self, title: str) -> dict:
        tree = block_store.document_tree(self.db, self.ids[title])
        assert tree is not None
        return tree

    def test_schema_is_current_and_consistent(self) -> None:
        with sqlite3.connect(self.db) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(blocks)")}
            legacy_markdown = connection.execute(
                "SELECT COUNT(*) FROM blocks WHERE content_json LIKE '%\"markdown\"%'"
            ).fetchone()[0]
        connection.close()
        self.assertEqual(version, 14)
        self.assertIn("block_sync", tables)
        self.assertFalse({"revisions", "block_attrs"} & tables)
        self.assertNotIn("position", columns)
        self.assertEqual((foreign_keys, integrity, legacy_markdown), ([], "ok", 0))
        self.assertTrue(block_store.fts_is_consistent(self.db))
        self.assertEqual(
            sorted(hit["text"] for hit in block_store.search_blocks(self.db, "quoted")),
            ["Quoted text", "quoted one\nquoted two"],
        )
        # Old undo steps describe rows that no longer exist in that shape.
        self.assertFalse(self.tree("Editor")["can_undo"])

    def test_markdown_import_is_parsed_and_the_quote_keeps_only_its_own_lines(self) -> None:
        tree = self.tree("MD")
        self.assertEqual(
            [node["type"] for node in tree["children"]],
            ["heading", "paragraph", "quote", "paragraph", "bullet_item", "check_item",
             "code", "divider", "table", "math", "image", "html"],
        )
        quote = tree["children"][2]
        self.assertEqual(_shape(quote["children"]), [("paragraph", {}, "quoted one\nquoted two")])
        self.assertEqual(tree["children"][3]["text"], "After the quote.")
        self.assertEqual(tree["children"][1]["content"][1],
                         {"type": "text", "text": "bold", "marks": {"bold": True}})
        self.assertEqual(
            _shape(tree["children"][8]["children"]),
            [
                ("table_row", {}, "", [("table_cell", {}, "", [("paragraph", {}, "a")]),
                                       ("table_cell", {}, "", [("paragraph", {}, "b")])]),
                ("table_row", {}, "", [("table_cell", {}, "", [("paragraph", {}, "1")]),
                                       ("table_cell", {}, "", [("paragraph", {}, "2")])]),
            ],
        )
        self.assertEqual(tree["children"][8]["attrs"], {"header_rows": 1})
        self.assertEqual(tree["children"][9]["payload"], {"latex": "e=mc^2"})
        self.assertEqual(tree["children"][10]["attrs"]["alt"], "pic")
        self.assertEqual(tree["children"][11]["payload"], {"html": '<div class="raw">raw</div>'})

    def test_editor_blocks_get_canonical_types_attrs_and_inline_nodes(self) -> None:
        tree = self.tree("Editor")
        md_id = self.ids["MD"]
        children = tree["children"]
        self.assertEqual(
            [node["type"] for node in children],
            ["heading", "paragraph", "check_item", "bullet_item", "quote", "callout", "code",
             "math", "image", "file", "divider", "table", "columns", "bullet_item"],
        )
        self.assertEqual(children[0]["attrs"], {"level": 5})
        mixed = children[1]["content"]
        self.assertEqual(mixed[0], {"type": "text", "text": "under",
                                    "marks": {"underline": True, "color": "red"}})
        self.assertEqual(mixed[1]["type"], "blockRef")
        self.assertEqual(mixed[2], {"type": "docLink", "id": md_id})
        self.assertEqual(_shape(children[4]["children"]), [("paragraph", {}, "inline quote")])
        self.assertEqual(children[5]["attrs"], {"icon": "!"})
        self.assertEqual((children[6]["attrs"], children[6]["text"]), ({"language": "py"}, "x = '```'"))
        self.assertEqual(children[7]["payload"], {"latex": "a^2+b^2"})
        # BlockNote's previewWidth is the width; an unknown attribute is kept.
        self.assertEqual(children[8]["attrs"], {"url": "/media/a.png", "alt": "A", "custom": "k", "width": 100})
        self.assertEqual(children[9]["attrs"], {"url": "/media/f.pdf", "name": "f.pdf", "size": 2048})
        table = children[11]
        self.assertEqual(table["attrs"], {"column_widths": [100, None], "header_rows": 1})
        cell_a, cell_b = table["children"][1]["children"]
        self.assertEqual([node["type"] for node in cell_a["children"]], ["paragraph", "image"])
        self.assertEqual(cell_b["attrs"], {"background": "#DEEAF6"})
        column = children[12]["children"][0]
        self.assertEqual((column["type"], column["attrs"]), ("column", {"width": 0.3}))
        self.assertEqual(_shape(column["children"]), [("paragraph", {}, "left")])
        self.assertEqual(children[13]["user_attrs"], {"due": "today"})
        backlinks = block_store.document_backlinks(self.db, md_id)
        self.assertEqual({link["source_document_id"] for link in backlinks}, {tree["id"]})
        self.assertEqual(len(backlinks), 2)

    def test_source_page_keeps_its_sync_state_and_reimports_cleanly(self) -> None:
        document_id = self.ids["OneNote page"]
        info = sources.document_info(self.db, document_id)
        [conflict] = info["conflicts"]
        self.assertEqual((conflict["kind"], conflict["local_text"], conflict["source_text"]),
                         ("block", "edited locally", "changed in source"))
        self.assertIsNotNone(info["sync"]["source_removed_at"])
        self.assertEqual(
            _shape(self.tree("OneNote page")["children"][2]["children"][1]["children"][1]["children"]),
            [("paragraph", {}, "d"), ("bullet_item", {}, "inner")],
        )

        blocks = sources.source_blocks(SOURCE_PAGE)
        for _ in range(2):
            result = sources.upsert_document_from_source(
                self.db, source="onenote_import", external_id="PAGE", title="OneNote page",
                blocks=blocks,
            )
            self.assertEqual(result.document_id, document_id)
            self.assertEqual(
                (result.blocks_created, result.blocks_updated, result.blocks_deleted,
                 len(result.conflicts)),
                (0, 0, 0, 0),
            )
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(self.tree("OneNote page")["children"][5]["text"], "edited locally")


if __name__ == "__main__":
    unittest.main()
