"""Rich inline marks, tables, columns, images, files and math (spec.md §2a)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import block_store, block_types, sources
from app.block_store import (
    apply_transaction,
    connection_scope,
    create_document,
    document_backlinks,
    document_tree,
    embedding_documents,
    search_blocks,
    undo_transaction,
)
from app.block_types import BlockShapeError, inline_from_markdown, inline_to_markdown
from app.models import BlockOperation
from app.portability import export_document_markdown, import_markdown_directory
from app.sources import source_block, source_blocks, table_content, upsert_document_from_source

RICH = [
    {"type": "text", "text": "Plain ", "styles": {}},
    {"type": "text", "text": "bold", "styles": {"bold": True}},
    {"type": "text", "text": " ", "styles": {}},
    {"type": "text", "text": "both", "styles": {"bold": True, "italic": True}},
    {"type": "text", "text": " ", "styles": {}},
    {"type": "text", "text": "under", "styles": {"underline": True}},
    {"type": "text", "text": "2", "styles": {"superscript": True}},
    {"type": "text", "text": "x", "styles": {"subscript": True}},
    {"type": "text", "text": "red", "styles": {"textColor": "#C00000"}},
    {"type": "text", "text": "lit", "styles": {"backgroundColor": "#FFFF00", "bold": True}},
    {"type": "text", "text": "gone", "styles": {"strike": True}},
    {"type": "text", "text": " a*b_c [x] ", "styles": {}},
    {"type": "text", "text": "co`de", "styles": {"code": True}},
    {"type": "text", "text": " ", "styles": {}},
    {"type": "link", "href": "https://example.com/a",
     "content": [{"type": "text", "text": "link", "styles": {"italic": True}}]},
    {"type": "text", "text": "\n# not a heading", "styles": {}},
]


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _by_type(tree, block_type):
    return [node for node in _walk(tree["children"]) if node["type"] == block_type]


class InlineMarkTests(unittest.TestCase):
    def test_runs_round_trip_through_markdown(self) -> None:
        markdown = inline_to_markdown(RICH)
        self.assertIn("**bold**", markdown)
        self.assertIn("~~gone~~", markdown)
        self.assertIn("<u>under</u>", markdown)
        self.assertIn("<sup>2</sup><sub>x</sub>", markdown)
        self.assertIn('<span style="color:#C00000">red</span>', markdown)
        self.assertIn('<mark style="background:#FFFF00">**lit**</mark>', markdown)
        self.assertIn("[*link*](https://example.com/a)", markdown)
        self.assertEqual(inline_from_markdown(markdown), block_types.normalize_inline(RICH))
        self.assertEqual(
            block_types.inline_plain_text(RICH),
            "Plain bold both under2xredlitgone a*b_c [x] co`de link\n# not a heading",
        )

    def test_awkward_emphasis_falls_back_to_html_and_still_round_trips(self) -> None:
        runs = [{"type": "text", "text": '"quoted"', "styles": {"bold": True}},
                {"type": "text", "text": "after", "styles": {}}]
        markdown = inline_to_markdown(runs)
        self.assertEqual(markdown, '<strong>"quoted"</strong>after')
        self.assertEqual(inline_from_markdown(markdown), runs)

    def test_compact_runs_and_validation(self) -> None:
        self.assertEqual(
            block_types.normalize_inline(
                [{"text": "x", "b": 1, "u": 1, "color": "#00F", "highlight": "yellow",
                  "link": "https://a.example", "font": "Calibri"}]
            ),
            [{"type": "link", "href": "https://a.example", "content": [{
                "type": "text", "text": "x",
                "styles": {"bold": True, "underline": True, "textColor": "#00F",
                           "backgroundColor": "yellow"}}]}],
        )
        for bad in ([{"type": "text", "text": "x", "styles": {"blink": True}}],
                    [{"type": "text", "text": "x", "styles": {"textColor": "red;x"}}],
                    [{"type": "mention"}], [3], "x" and 5):
            with self.assertRaises(BlockShapeError):
                block_types.normalize_inline(bad)


class BlockTypeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.db = self.root / ".fortress.sqlite3"
        create_document(self.db, "doc", "Doc", "rough", [], "Intro", "t", "t")

    def apply(self, *operations):
        tree = document_tree(self.db, "doc")
        return apply_transaction(self.db, "doc", list(operations), tree["revision"])

    def roundtrip(self, document_id="doc"):
        markdown = export_document_markdown(self.db, document_id)
        other = self.root / f"copy-{document_id}"
        other.mkdir()
        (other / f"{document_id}.md").write_text(markdown, encoding="utf-8")
        target = other / ".fortress.sqlite3"
        report = import_markdown_directory(target, other)
        self.assertEqual(report.error_count, 0, report.items)
        self.assertEqual(export_document_markdown(target, document_id), markdown)
        return markdown, document_tree(target, document_id)

    # --- Inline marks through transactions ------------------------------------

    def test_rich_paragraph_keeps_plain_text_and_exports_marks(self) -> None:
        tree = self.apply(
            BlockOperation(operation="insert", type="paragraph", content={"blocknote": RICH}),
            BlockOperation(operation="insert", type="heading", attrs={"level": 2},
                           content={"blocknote": [{"type": "text", "text": "Hi", "styles": {"underline": True}}]}),
        )
        paragraph, heading = tree["children"][1:]
        self.assertEqual(paragraph["text"], block_types.inline_plain_text(RICH))
        self.assertIn("<u>under</u>", paragraph["content"]["markdown"])
        self.assertEqual(heading["content"]["markdown"], "## <u>Hi</u>")
        self.assertEqual(search_blocks(self.db, "both")[0]["block_id"], paragraph["id"])
        self.assertEqual(search_blocks(self.db, "span"), [])
        markdown, restored = self.roundtrip()
        self.assertIn('<mark style="background:#FFFF00">**lit**</mark>', markdown)
        rich = restored["children"][1]
        self.assertEqual(rich["content"]["blocknote"], block_types.normalize_inline(RICH))
        self.assertNotIn("<u>", rich["text"])  # mark HTML is not indexed
        self.assertEqual(restored["children"][2]["attrs"], {"level": 2})

    def test_web_editor_runs_with_colours_get_markdown_that_keeps_them(self) -> None:
        runs = [{"type": "text", "text": "red", "styles": {"textColor": "red"}}]
        tree = self.apply(BlockOperation(
            operation="insert", type="paragraph", text="red",
            content={"blocknote": runs, "markdown": "red"},  # BlockNote's lossy Markdown
        ))
        self.assertEqual(tree["children"][1]["content"]["markdown"], '<span style="color:red">red</span>')

    def test_check_list_items_keep_checked_state(self) -> None:
        source = source_block({"type": "list", "checked": True, "inline": [{"text": "Done", "s": 1}]})
        result = upsert_document_from_source(
            self.db, source="onenote_import", external_id="P", title="T", blocks=[source]
        )
        item = document_tree(self.db, result.document_id)["children"][0]
        self.assertEqual(item["attrs"], {"checked": True})
        self.assertEqual(item["content"]["markdown"], "- [x] ~~Done~~")
        self.assertEqual(item["text"], "Done")

    # --- Tables ---------------------------------------------------------------

    def test_table_insert_validates_and_indexes_cells(self) -> None:
        table = table_content([["Name", {"text": "Qty", "align": "right"}], ["Apples", {"text": "3", "align": "right"}]],
                              header_rows=1)
        tree = self.apply(BlockOperation(operation="insert", type="table", content={"blocknote": table}))
        block = tree["children"][1]
        self.assertEqual(block["text"], "Name\tQty\nApples\t3")
        self.assertEqual(block["content"]["markdown"],
                         "| Name | Qty |\n| --- | ---: |\n| Apples | 3 |")
        self.assertEqual(search_blocks(self.db, "apples")[0]["block_id"], block["id"])
        self.assertIn("Apples\t3", next(item[3] for item in embedding_documents(self.db) if item[0] == block["id"]))
        for garbage in ({"rows": []}, {"rows": "x"}, {"rows": [{"cells": [{"props": {"backgroundColor": 5}}]}]},
                        {"rows": [[{"content": [{"type": "text", "text": "x", "styles": {"nope": 1}}]}]]},
                        {"rows": [["a"]], "columnWidths": ["wide"]}, None):
            with self.assertRaises(BlockShapeError):
                self.apply(BlockOperation(operation="insert", type="table", content={"blocknote": garbage}))
        # A cell edit re-derives Markdown and text.
        table["rows"][1]["cells"][0]["content"] = [{"type": "text", "text": "Pears", "styles": {}}]
        tree = self.apply(BlockOperation(operation="update", block_id=block["id"], content={"blocknote": table}))
        self.assertEqual(tree["children"][1]["text"], "Name\tQty\nPears\t3")
        with self.assertRaises(ValueError):
            self.apply(BlockOperation(operation="split", block_id=block["id"], split_at=2))

    def test_rich_table_exports_html_and_round_trips(self) -> None:
        table = table_content(
            [
                [{"inline": [{"text": "Shaded", "b": 1}], "background": "#DEEAF6"}, "Plain"],
                [
                    {"text": "Steps", "blocks": [
                        {"type": "list", "ordered": True, "text": "First"},
                        {"type": "list", "ordered": True, "text": "Second", "children": [
                            {"type": "list", "checked": False, "text": "sub task"}]},
                        {"type": "image", "url": "/media/01J00000000000000000000IMG.png", "alt": "scan"},
                        {"type": "table", "rows": [["inner", {"text": "nested", "color": "red"}]]},
                        {"type": "code", "language": "py", "text": "x = 1\n\ny = 2"},
                    ]},
                    {"inline": [{"text": "H"}, {"text": "2", "sub": 1}, {"text": "O"}], "align": "center"},
                ],
            ],
            column_widths=[200, None],
        )
        tree = self.apply(BlockOperation(operation="insert", type="table", attrs={"borders": False},
                                         content={"blocknote": table}))
        block = tree["children"][1]
        self.assertEqual(block["text"], "Shaded\tPlain\nSteps First Second sub task scan inner nested x = 1 y = 2\tH2O")
        self.assertEqual(search_blocks(self.db, "nested")[0]["block_id"], block["id"])
        markdown, restored = self.roundtrip()
        self.assertIn('<table data-borders="false">', markdown)
        self.assertIn('<col style="width:200px">', markdown)
        self.assertIn('<td style="background-color:#DEEAF6"><strong>Shaded</strong></td>', markdown)
        copy = restored["children"][1]
        self.assertEqual(copy["type"], "table")
        self.assertEqual(copy["attrs"], {"borders": False})
        self.assertEqual(copy["content"]["blocknote"], block["content"]["blocknote"])
        # An editor that cannot show nested cell blocks saves the table without
        # them; while the table keeps its shape they stay. "blocks": [] clears.
        web_copy = json.loads(json.dumps(block["content"]["blocknote"]))
        del web_copy["rows"][1]["cells"][0]["blocks"]
        web_copy["rows"][0]["cells"][1]["content"] = [{"type": "text", "text": "Edited", "styles": {}}]
        tree = self.apply(BlockOperation(operation="update", block_id=block["id"],
                                         content={"blocknote": web_copy}))
        cell = tree["children"][1]["content"]["blocknote"]["rows"][1]["cells"][0]
        self.assertEqual(len(cell["blocks"]), 5)
        web_copy["rows"][1]["cells"][0]["blocks"] = []
        tree = self.apply(BlockOperation(operation="update", block_id=block["id"],
                                         content={"blocknote": web_copy}))
        self.assertNotIn("blocks", tree["children"][1]["content"]["blocknote"]["rows"][1]["cells"][0])

    def test_gfm_tables_import_as_tables(self) -> None:
        body = "Intro\n\n| A | B |\n|:-:|---|\n| *x* | y \\| z |\n| <u>u</u> |  |\n"
        create_document(self.db, "gfm", "G", "rough", [], body, "t", "t")
        table = document_tree(self.db, "gfm")["children"][1]
        self.assertEqual(table["type"], "table")
        content = table["content"]["blocknote"]
        self.assertEqual(content["headerRows"], 1)
        self.assertEqual(content["rows"][0]["cells"][0]["props"], {"textAlignment": "center"})
        self.assertEqual(content["rows"][1]["cells"][1]["content"][0]["text"], "y | z")
        self.assertEqual(content["rows"][2]["cells"][0]["content"][0]["styles"], {"underline": True})
        self.assertEqual(table["text"], "A\tB\nx\ty | z\nu\t")
        self.roundtrip("gfm")

    def test_api_rejects_garbage_tables_with_422(self) -> None:
        from app import documents, main

        settings = SimpleNamespace(
            block_db_import_on_startup=False, block_db_path=self.db, notes_path=self.root,
            assets_path=self.root / "assets", embeddings_enabled=False, vision_enabled=False,
            auto_backup_enabled=False, reindex_interval_s=5.0,
        )
        with patch.object(main, "settings", settings), patch.object(
            documents, "get_settings", lambda: settings
        ), TestClient(main.app) as client:
            tree = client.get("/api/block-documents/doc").json()
            bad = client.post("/api/block-documents/doc/transactions", json={
                "base_revision": tree["revision"],
                "operations": [{"operation": "insert", "type": "table",
                                "content": {"blocknote": {"rows": [{"cells": "nope"}]}}}],
            })
            self.assertEqual(bad.status_code, 422, bad.text)
            good = client.post("/api/block-documents/doc/transactions", json={
                "base_revision": tree["revision"],
                "operations": [{"operation": "insert", "type": "table",
                                "content": {"blocknote": {"rows": [{"cells": [
                                    {"type": "tableCell", "props": {"backgroundColor": "default",
                                     "textColor": "default", "textAlignment": "left", "colspan": 1,
                                     "rowspan": 1}, "content": [{"type": "text", "text": "web",
                                                                 "styles": {}}]}]}],
                                    "columnWidths": [None], "type": "tableContent"}}}],
            })
            self.assertEqual(good.status_code, 200, good.text)
            self.assertEqual(good.json()["children"][-1]["text"], "web")

    # --- Columns --------------------------------------------------------------

    def columns_page(self):
        create_document(self.db, "target", "Target", "rough", [], "Target body", "t", "t")
        blocks = source_blocks([
            {"type": "heading", "level": 1, "text": "Top", "id": "h"},
            {"type": "columns", "id": "cols", "attrs": {"onenote_y": 3}, "columns": [
                {"id": "c1", "width": 0.4, "attrs": {"onenote_x": 1}, "blocks": [
                    {"id": "p1", "text": "Left column words"},
                    {"id": "l1", "type": "list", "text": "left item", "children": [
                        {"id": "l2", "type": "list", "text": "nested item"}]},
                ]},
                {"id": "c2", "width": 0.6, "blocks": [
                    {"id": "p2", "markdown": "See [[target|Target]]"},
                    {"id": "img", "type": "image", "url": "/media/x.png", "alt": "whiteboard photo",
                     "width": 320, "height": 200},
                ]},
            ]},
            {"id": "after", "text": "After"},
        ])
        result = upsert_document_from_source(
            self.db, source="onenote_import", external_id="PAGE", title="Cols", blocks=blocks
        )
        return result.document_id

    def test_columns_tree_indexing_backlinks_and_round_trip(self) -> None:
        document_id = self.columns_page()
        tree = document_tree(self.db, document_id)
        columns = tree["children"][1]
        self.assertEqual(columns["type"], "columns")
        self.assertEqual(columns["attrs"], {"onenote_y": 3})
        self.assertEqual([c["attrs"] for c in columns["children"]],
                         [{"onenote_x": 1, "width": 0.4}, {"width": 0.6}])
        left = columns["children"][0]["children"]
        self.assertEqual(left[0]["text"], "Left column words")
        self.assertEqual(left[1]["children"][0]["text"], "nested item")
        self.assertEqual(search_blocks(self.db, "nested item")[0]["block_id"], left[1]["children"][0]["id"])
        self.assertEqual(search_blocks(self.db, "whiteboard")[0]["block_type"], "image")
        self.assertIn(left[0]["id"], {item[0] for item in embedding_documents(self.db)})
        self.assertEqual(len(document_backlinks(self.db, "target")), 1)
        markdown = export_document_markdown(self.db, document_id)
        self.assertIn('<!-- fortress:columns {"onenote_y":3} -->', markdown)
        self.assertIn('<!-- fortress:column {"onenote_x":1,"width":0.4} -->', markdown)
        self.assertIn("<!-- /fortress:columns -->", markdown)
        self.assertIn('<img src="/media/x.png" alt="whiteboard photo" width="320" height="200">', markdown)

        other = self.root / "copy"
        other.mkdir()
        (other / "page.md").write_text(markdown, encoding="utf-8")
        target = other / ".fortress.sqlite3"
        import_markdown_directory(target, other)
        copy = document_tree(target, "page")
        self.assertEqual([n["type"] for n in copy["children"]], ["heading", "columns", "paragraph"])
        self.assertEqual(
            [[child["text"] for child in column["children"]] for column in copy["children"][1]["children"]],
            [["Left column words", "- left item"], ["See [[target|Target]]", "whiteboard photo"]],
        )
        self.assertEqual(export_document_markdown(target, "page"), markdown)
        # Re-importing the same source changes nothing.
        again = upsert_document_from_source(
            self.db, source="onenote_import", external_id="PAGE", title="Cols",
            blocks=sources._from_markdown(markdown.split("---", 2)[2]),
        )
        # (Markdown carries no runs or heading level, but every block is matched.)
        self.assertEqual((again.blocks_created, again.blocks_deleted), (0, 0))

    def test_columns_moves_duplicate_and_undo(self) -> None:
        document_id = self.columns_page()
        tree = document_tree(self.db, document_id)
        columns = tree["children"][1]
        left, right = columns["children"]
        after = tree["children"][2]

        def apply(*operations):
            current = document_tree(self.db, document_id)
            return apply_transaction(self.db, document_id, list(operations), current["revision"])

        # A block moves into a column like into any parent ...
        tree = apply(BlockOperation(operation="move", block_id=after["id"], parent_id=right["id"], position=0))
        self.assertEqual(tree["children"][1]["children"][1]["children"][0]["text"], "After")
        # ... but not directly into the columns block, and columns stay in a layout.
        with self.assertRaises(ValueError):
            apply(BlockOperation(operation="move", block_id=after["id"], parent_id=columns["id"]))
        with self.assertRaises(ValueError):
            apply(BlockOperation(operation="move", block_id=left["id"], parent_id=None))
        with self.assertRaises(ValueError):
            apply(BlockOperation(operation="insert", type="paragraph", parent_id=columns["id"]))
        tree = apply(BlockOperation(operation="insert", type="column", parent_id=columns["id"],
                                    attrs={"width": 1}))
        self.assertEqual(len(tree["children"][1]["children"]), 3)
        # Columns can be reordered within their layout.
        tree = apply(BlockOperation(operation="move", block_id=right["id"], parent_id=columns["id"], position=0))
        self.assertEqual(tree["children"][1]["children"][0]["id"], right["id"])
        tree = apply(BlockOperation(operation="duplicate", block_id=columns["id"]))
        copy = tree["children"][2]
        self.assertEqual(copy["type"], "columns")
        self.assertEqual(len(list(_walk([copy]))), len(list(_walk([tree["children"][1]]))))
        self.assertEqual(len(search_blocks(self.db, "Left column words")), 2)
        tree = apply(BlockOperation(operation="delete", block_id=columns["id"]))
        self.assertEqual(len(search_blocks(self.db, "Left column words")), 1)
        restored = undo_transaction(self.db, document_id, tree["revision"])
        self.assertEqual(restored["children"][1]["type"], "columns")
        self.assertEqual(len(search_blocks(self.db, "Left column words")), 2)

    # --- Images, files, math --------------------------------------------------

    def test_image_file_and_math_blocks(self) -> None:
        tree = self.apply(
            BlockOperation(operation="insert", type="image",
                           attrs={"url": "/media/a.png", "alt": "receipt total 42", "width": 300}),
            BlockOperation(operation="insert", type="file",
                           attrs={"url": "/media/b.pdf", "name": "Contract [v2].pdf", "size": 48213,
                                  "mime": "application/pdf"}),
            BlockOperation(operation="insert", type="math", content={"latex": "E = mc^2"}),
        )
        image, attachment, math = tree["children"][1:]
        self.assertEqual(image["text"], "receipt total 42")
        self.assertEqual(attachment["text"], "Contract [v2].pdf")
        self.assertEqual(math["text"], "E = mc^2")
        self.assertEqual(math["content"]["markdown"], "$$\nE = mc^2\n$$")
        self.assertEqual(search_blocks(self.db, "receipt")[0]["block_id"], image["id"])
        self.assertEqual(search_blocks(self.db, "contract")[0]["block_id"], attachment["id"])
        for bad in (
            BlockOperation(operation="insert", type="image", attrs={"width": "huge"}),
            BlockOperation(operation="insert", type="file", attrs={"size": -1}),
            BlockOperation(operation="insert", type="math", content={"latex": 3}),
            BlockOperation(operation="insert", type="column"),
        ):
            with self.assertRaises(ValueError):
                self.apply(bad)
        # Resizing an image keeps its Markdown in step.
        tree = self.apply(BlockOperation(operation="set_attrs", block_id=image["id"],
                                         attrs={"url": "/media/a.png", "alt": "receipt total 42", "width": 120}))
        self.assertIn('width="120"', tree["children"][1]["content"]["markdown"])
        markdown, restored = self.roundtrip()
        self.assertIn("[Contract \\[v2\\].pdf](/media/b.pdf) (47.1 KB)", markdown)
        self.assertIn('<!-- fortress:file {"mime":"application/pdf"', markdown)
        self.assertEqual([n["type"] for n in restored["children"][1:]], ["image", "file", "math"])
        self.assertEqual(restored["children"][2]["attrs"]["size"], 48213)

    def test_old_raw_img_html_imports_as_an_image_block(self) -> None:
        create_document(self.db, "old", "Old", "rough", [], '<img src="/media/01J.png" width=300>', "t", "t")
        image = document_tree(self.db, "old")["children"][0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["attrs"], {"url": "/media/01J.png", "width": 300})

    # --- Living documents -----------------------------------------------------

    def test_upsert_tracks_structured_blocks(self) -> None:
        def page(cell):
            return source_blocks([
                {"id": "t", "type": "table", "rows": [["k", cell]], "borders": True},
                {"id": "m", "type": "math", "latex": "a^2"},
            ])

        first = upsert_document_from_source(self.db, source="onenote_import", external_id="P",
                                            title="T", blocks=page("v1"))
        self.assertEqual(first.status, "created")
        same = upsert_document_from_source(self.db, source="onenote_import", external_id="P",
                                           title="T", blocks=page("v1"))
        self.assertEqual(same.status, "unchanged")
        changed = upsert_document_from_source(self.db, source="onenote_import", external_id="P",
                                              title="T", blocks=page("v2"))
        self.assertEqual((changed.status, changed.blocks_updated), ("updated", 1))
        tree = document_tree(self.db, first.document_id)
        table = tree["children"][0]
        self.assertEqual(table["text"], "k\tv2")
        # A local edit wins; the source's version waits as a conflict.
        local = table_content([["k", "local"]])
        apply_transaction(self.db, first.document_id, [BlockOperation(
            operation="update", block_id=table["id"], content={"blocknote": local})], tree["revision"])
        conflicted = upsert_document_from_source(self.db, source="onenote_import", external_id="P",
                                                 title="T", blocks=page("v3"))
        self.assertEqual(len(conflicted.conflicts), 1)
        self.assertEqual(document_tree(self.db, first.document_id)["children"][0]["text"], "k\tlocal")
        resolved = sources.resolve_block_conflict(self.db, first.document_id, table["id"], "source")
        self.assertEqual(resolved["children"][0]["text"], "k\tv3")
        self.assertEqual(resolved["children"][0]["attrs"], {"borders": True})

    def test_spec_example_imports_and_round_trips(self) -> None:
        blocks = source_blocks([
            {"id": "{P1}", "type": "heading", "level": 2, "inline": [{"text": "Plan", "u": 1}]},
            {"id": "{P2}", "text": "plain paragraph"},
            {"id": "{P3}", "type": "list", "checked": True, "inline": [{"text": "done", "s": 1}]},
            {"id": "{T1}", "type": "table", "borders": True, "column_widths": [96, None],
             "header_rows": 0, "rows": [["a", {"inline": [{"text": "b", "i": 1}], "background": "#DEEAF6",
                                               "blocks": [{"type": "list", "text": "in cell"},
                                                          {"type": "table", "rows": [["x"]]}]}]]},
            {"id": "{O1}", "type": "columns", "columns": [
                {"id": "{O1a}", "width": 0.5, "blocks": [{"text": "left"}]},
                {"id": "{O1b}", "width": 0.5, "blocks": [{"type": "image", "url": "/media/i.png",
                                                           "alt": "OCR", "width": 300}]}]},
            {"id": "{F1}", "type": "file", "url": "/media/f.pdf", "name": "a.pdf", "size": 1,
             "mime": "application/pdf"},
            {"id": "{M1}", "type": "math", "latex": "a^2"},
        ])
        result = upsert_document_from_source(self.db, source="onenote_import", external_id="SPEC",
                                             title="Spec", blocks=blocks)
        tree = document_tree(self.db, result.document_id)
        self.assertEqual([n["type"] for n in tree["children"]],
                         ["heading", "paragraph", "list", "table", "columns", "file", "math"])
        self.assertEqual([n["text"] for n in tree["children"]],
                         ["Plan", "plain paragraph", "done", "a\tb in cell x", "", "a.pdf", "a^2"])
        self.assertEqual(tree["children"][0]["content"]["markdown"], "## <u>Plan</u>")
        self.roundtrip(result.document_id)

    def test_source_block_helper_rejects_garbage(self) -> None:
        with self.assertRaises(BlockShapeError):
            source_block({"type": "table", "rows": [["ok"], "bad row"]})
        with self.assertRaises(BlockShapeError):
            upsert_document_from_source(self.db, source="x", external_id="1", title="T", blocks=[
                sources.SourceBlock(type="table", content={"blocknote": {"rows": []}})])


if __name__ == "__main__":
    unittest.main()
