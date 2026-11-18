"""Markdown in and out of canonical blocks (docs/block-model.md §7)."""

import tempfile
import unittest
from pathlib import Path

from app.block_markdown import parse_markdown, render_markdown
from app.block_store import create_block_document, document_tree, replace_document_from_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def _nodes(specs, prefix="b"):
    """Stored-tree-like nodes for parsed specs, each with an id."""

    return [
        {
            "id": spec.id or f"{prefix}{index}", "type": spec.type, "attrs": spec.attrs,
            "content": spec.content, "payload": spec.payload, "provenance": spec.provenance,
            "children": _nodes(spec.children, f"{prefix}{index}_"),
        }
        for index, spec in enumerate(specs)
    ]


def _shape(nodes):
    return [(node["id"], node["type"], node["attrs"], node["content"], node["payload"],
             _shape(node["children"])) for node in nodes]


class MarkdownImportTests(unittest.TestCase):
    def test_a_quote_holds_only_its_own_lines(self) -> None:
        # B1: the quote used to swallow every block after it.
        specs = parse_markdown("> one\n> two\n\nAfter\n\n- item\n")
        self.assertEqual([spec.type for spec in specs], ["quote", "paragraph", "bullet_item"])
        [inner] = specs[0].children
        self.assertEqual((inner.type, inner.content), ("paragraph", [{"type": "text", "text": "one\ntwo"}]))

    def test_quotes_are_containers_of_blocks(self) -> None:
        [quote] = parse_markdown("> first\n>\n> - item\n>\n> second\n")
        self.assertEqual([child.type for child in quote.children], ["paragraph", "bullet_item", "paragraph"])

    def test_every_block_kind_parses_to_canonical_blocks(self) -> None:
        specs = parse_markdown((FIXTURES / "every_block.md").read_text(encoding="utf-8"))
        self.assertEqual(
            [spec.type for spec in specs],
            ["heading", "paragraph", "quote", "bullet_item", "check_item", "check_item",
             "numbered_item", "numbered_item", "code", "divider", "table", "math", "image", "image",
             "html", "columns", "table", "heading"],
        )
        para = specs[1].content
        self.assertIn({"type": "link", "href": "https://x.y", "content": [{"type": "text", "text": "link"}]}, para)
        self.assertIn({"type": "docLink", "id": "01ABC"}, para)
        self.assertIn({"type": "blockRef", "id": "B1"}, para)
        self.assertIn({"type": "text", "text": "sup", "marks": {"sup": True}}, para)
        self.assertEqual(specs[8].attrs, {"language": "py"})
        self.assertEqual(specs[8].content, [{"type": "text", "text": "x = '```'"}])
        gfm = specs[10]
        self.assertEqual(gfm.attrs["header_rows"], 1)
        self.assertEqual(gfm.children[1].children[1].children[0].content,
                         [{"type": "text", "text": "2", "marks": {"bold": True}}])
        self.assertEqual(specs[11].payload, {"latex": "a^2"})
        self.assertEqual(specs[12].attrs, {"url": "/media/x.png", "alt": "pic", "width": 100})
        columns = specs[15]
        self.assertEqual([column.attrs.get("width") for column in columns.children], [0.3, None])
        html_table = specs[16]
        cell = html_table.children[1].children[0]
        self.assertEqual(cell.attrs["colspan"], 2)
        self.assertEqual([child.type for child in cell.children], ["paragraph", "bullet_item"])
        self.assertEqual(specs[17].attrs, {"level": 6})

    def test_render_then_parse_is_lossless_and_keeps_ids(self) -> None:
        nodes = _nodes(parse_markdown((FIXTURES / "every_block.md").read_text(encoding="utf-8")))
        markdown = render_markdown(nodes)
        again = _nodes(parse_markdown(markdown), prefix="new")
        self.assertEqual(_shape(again), _shape(nodes))
        self.assertEqual(render_markdown(again), markdown)

    def test_unsafe_links_stay_text(self) -> None:
        [spec] = parse_markdown('[x](javascript:alert(1)) <a href="javascript:x">y</a>')
        self.assertEqual([node["type"] for node in spec.content], ["text"])


class MarkdownMirrorTests(unittest.TestCase):
    def test_a_mirror_edit_keeps_block_ids_and_types_markdown_cannot_say(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Doc", "rough", [], "Intro\n\n> note\n")
            tree = document_tree(database, created["id"])
            intro, quote = tree["children"]
            from app.block_store import apply_transaction
            from app.models import BlockOperation

            tree = apply_transaction(database, created["id"], [
                BlockOperation(operation="update", block_id=quote["id"], type="callout",
                               attrs={"icon": "!"}),
            ], tree["revision"])
            body = render_markdown(tree["children"])
            self.assertIn('"type":"callout"', body)
            replace_document_from_markdown(
                database, created["id"], "Doc", "rough", [], body.replace("Intro", "Intro, edited"),
                created["created_at"], created["updated_at"],
            )
            after = document_tree(database, created["id"])
            self.assertEqual([node["id"] for node in after["children"]], [intro["id"], quote["id"]])
            self.assertEqual(after["children"][0]["text"], "Intro, edited")
            self.assertEqual((after["children"][1]["type"], after["children"][1]["attrs"]),
                             ("callout", {"icon": "!"}))
            self.assertEqual(after["children"][1]["children"][0]["id"], quote["children"][0]["id"])


if __name__ == "__main__":
    unittest.main()
