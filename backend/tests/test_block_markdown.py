"""Markdown in and out of canonical blocks (docs/block-model.md §7, docs/open-standards.md)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from ulid import ULID

from app.block_markdown import RenderLinks, parse_markdown, render_markdown
from app.block_store import create_block_document, document_tree
from app import block_store

from support import import_markdown_files

FIXTURES = Path(__file__).parent / "fixtures"


def _nodes(specs):
    """Stored-tree-like nodes for parsed specs, each with an id (a ULID, as stored ids are)."""

    return [
        {
            "id": spec.id or str(ULID()), "type": spec.type, "attrs": spec.attrs,
            "content": spec.content, "payload": spec.payload,
            "children": _nodes(spec.children),
        }
        for spec in specs
    ]


def _shape(nodes, ids=True):
    return [((node["id"] if ids else None), node["type"], node["attrs"], node["content"],
             node["payload"], _shape(node["children"], ids)) for node in nodes]


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


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
             "numbered_item", "numbered_item", "code", "divider", "table", "code", "image", "image",
             "html", "table", "heading"],
        )
        para = specs[1].content
        self.assertIn({"type": "link", "href": "https://x.y", "content": [{"type": "text", "text": "link"}]}, para)
        self.assertIn({"type": "docLink", "id": "01ABC"}, para)
        self.assertIn({"type": "blockRef", "id": "01J00000000000000000000B10"}, para)
        self.assertIn({"type": "text", "text": "sup", "marks": {"sup": True}}, para)
        self.assertEqual(specs[8].attrs, {"language": "py"})
        self.assertEqual(specs[8].content, [{"type": "text", "text": "x = '```'"}])
        gfm = specs[10]
        self.assertEqual(gfm.attrs["header_rows"], 1)
        self.assertEqual(gfm.children[1].children[1].children[0].content,
                         [{"type": "text", "text": "2", "marks": {"bold": True}}])
        self.assertEqual((specs[11].attrs, specs[11].content), ({"language": "latex"}, [{"type": "text", "text": "a^2"}]))
        self.assertEqual(specs[12].attrs, {"url": "/media/x.png", "alt": "pic", "width": 100})
        html_table = specs[15]
        cell = html_table.children[1].children[0]
        self.assertEqual(cell.attrs["colspan"], 2)
        self.assertEqual([child.type for child in cell.children], ["paragraph", "bullet_item"])
        self.assertEqual(specs[16].attrs, {"level": 6})

    def test_only_referenced_blocks_get_an_id(self) -> None:
        nodes = _nodes(parse_markdown("Plain\n\nReferenced\n\n```\ncode\n```\n\n# Heading"))
        anchors = {nodes[1]["id"], nodes[2]["id"], nodes[3]["id"]}
        markdown = render_markdown(nodes, links=RenderLinks(anchors=anchors))
        self.assertEqual(markdown, f"Plain\n\nReferenced ^{nodes[1]['id']}\n\n```\ncode\n```\n\n"
                                   f"^{nodes[2]['id']}\n\n# Heading")  # headings are linked by text
        again = parse_markdown(markdown)
        self.assertEqual([spec.id for spec in again[1:3]], [nodes[1]["id"], nodes[2]["id"]])
        self.assertNotIn("^", render_markdown(nodes))  # no RenderLinks: nothing is referred to

    def test_the_markdown_is_plain_and_reads_back_as_the_same_blocks(self) -> None:
        nodes = _nodes(parse_markdown((FIXTURES / "every_block.md").read_text(encoding="utf-8")))
        markdown = render_markdown(nodes, links=RenderLinks(anchors=set()))
        self.assertNotIn("fortress", markdown)
        self.assertNotIn("data-", markdown)
        # (the only "^" left is the reference to a block this note doesn't have)
        self.assertEqual(markdown.replace("x^2", "").replace("a^2", "").count("^"), 1)
        self.assertIn("[[#^01J00000000000000000000B10]]", markdown)
        self.assertIn("| a | b |\n| :---: | --- |", markdown)  # a GFM table again
        self.assertIn("![pic|100](/media/x.png)", markdown)
        again = _nodes(parse_markdown(markdown))
        self.assertEqual(_shape(again, ids=False), _shape(nodes, ids=False))

    def test_unsafe_links_stay_text(self) -> None:
        [spec] = parse_markdown('[x](javascript:alert(1)) <a href="javascript:x">y</a>')
        self.assertEqual([node["type"] for node in spec.content], ["text"])


class ObsidianSyntaxTests(unittest.TestCase):
    """What Obsidian writes reads as the blocks it shows."""

    def test_callouts_highlights_tasks_dollars_and_embeds(self) -> None:
        specs = parse_markdown(
            "> [!warning]- Careful *now*\n> Body with ==marked== text.\n\n"
            "> [!NOTE]\n> plain\n\n"
            "- [ ] open ^task-1\n\n"
            "Cost $5 and $x$ stays.\n\n"
            "![[01J00000000000000000000AB1.png|320x200]]\n\n"
            "![[talk.mp3]]\n\n"
            "![chart|240](../assets/01J00000000000000000000AB1.png \"The chart\")\n"
        )
        warning, note, task, prose, embed, audio, chart = specs
        self.assertEqual((warning.type, warning.attrs),
                         ("callout", {"callout_type": "warning", "callout_fold": "-", "callout_title": True}))
        title, body = warning.children
        self.assertEqual(title.content, [{"type": "text", "text": "Careful "},
                                         {"type": "text", "text": "now", "marks": {"italic": True}}])
        self.assertIn({"type": "text", "text": "marked", "marks": {"highlight": "yellow"}}, body.content)
        self.assertEqual((note.type, note.attrs, len(note.children)), ("callout", {}, 1))
        self.assertEqual((task.type, task.attrs["checked"], task.content),
                         ("check_item", False, [{"type": "text", "text": "open"}]))
        self.assertTrue(task.id)  # ^task-1 became a block id
        self.assertEqual(prose.content, [{"type": "text", "text": "Cost $5 and $x$ stays."}])
        self.assertEqual(embed.attrs, {"url": "/media/01J00000000000000000000AB1.png",
                                       "width": 320, "height": 200})
        self.assertEqual((audio.type, audio.attrs["name"], audio.attrs["mime"]),
                         ("file", "talk.mp3", "audio/mpeg"))
        self.assertEqual(chart.attrs, {"url": "/media/01J00000000000000000000AB1.png", "alt": "chart",
                                       "width": 240, "caption": "The chart"})

    def test_block_ids_and_links_within_a_note(self) -> None:
        heading, para, ref = parse_markdown(
            "# Plans\n\nShip it. ^ship\n\nSee [[#^ship|that]] under [[#Plans]].\n"
        )
        self.assertEqual(para.content, [{"type": "text", "text": "Ship it."}])
        # Obsidian's short ids become workspace-unique ids, consistently.
        self.assertNotEqual(para.id, "ship")
        self.assertEqual(ref.content[1], {"type": "blockRef", "id": para.id})
        # Without context a heading link has nowhere to go: it stays text.
        self.assertEqual(ref.content[-1]["type"], "text")

    def test_escaped_obsidian_syntax_stays_text(self) -> None:
        text = [{"type": "text", "text": "a == b, x==y==, ^tag at end ^id, %%c%%, [[no]] [!note]"}]
        markdown = render_markdown([{"id": "p", "type": "paragraph", "attrs": {}, "content": text,
                                     "payload": None, "children": []}])
        [spec] = parse_markdown(markdown)
        self.assertEqual(spec.content, text)
        self.assertIsNone(spec.id)

    def test_blocks_nested_under_a_paragraph_follow_it(self) -> None:
        tree = _nodes(parse_markdown("Parent\n\n- item"))
        tree[0]["children"] = _nodes(parse_markdown("Child\n\n> quoted"))
        tree[0]["children"][0]["children"] = _nodes(parse_markdown("Grandchild"))
        markdown = render_markdown(tree, links=RenderLinks(anchors=set()))
        self.assertEqual(markdown, "Parent\n\nChild\n\nGrandchild\n\n> quoted\n\n- item")

    def test_columns_are_their_contents_in_order(self) -> None:
        def paragraph(text):
            return {"id": text, "type": "paragraph", "attrs": {}, "payload": None, "children": [],
                    "content": [{"type": "text", "text": text}]}

        columns = {"id": "c", "type": "columns", "attrs": {}, "content": None, "payload": None,
                   "children": [{"id": f"col{n}", "type": "column", "attrs": {"width": 0.5},
                                 "content": None, "payload": None, "children": [paragraph(text)]}
                                for n, text in enumerate(("left", "right"))]}
        self.assertEqual(render_markdown([columns]), "left\n\nright")

    def test_highlight_colours_and_html_marks(self) -> None:
        content = [
            {"type": "text", "text": "yellow", "marks": {"highlight": "yellow"}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "green", "marks": {"highlight": "#00ff00"}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "up", "marks": {"sup": True}},
        ]
        markdown = render_markdown([{"id": "p", "type": "paragraph", "attrs": {}, "content": content,
                                     "payload": None, "children": []}])
        self.assertEqual(markdown, '==yellow== <mark style="background:#00ff00">green</mark> <sup>up</sup>')
        self.assertEqual(parse_markdown(markdown)[0].content, content)


class MarkdownExportTests(unittest.TestCase):
    def test_what_markdown_cannot_say_is_not_written(self) -> None:
        from app.block_store import apply_transaction
        from app.models import BlockOperation

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "Doc", "rough", "Intro\n\n> note\n")
            intro, quote = created["children"]
            tree = apply_transaction(database, created["id"], [
                BlockOperation(operation="update", block_id=quote["id"], type="callout",
                               attrs={"icon": "!"}),
            ], created["revision"])
            # The callout's icon (an extension attribute) has no Markdown form.
            self.assertEqual(render_markdown(tree["children"]), "Intro\n\n> [!note]\n> note")

    def test_a_table_gfm_cannot_show_is_plain_html(self) -> None:
        [table] = _nodes(parse_markdown(
            '<table><tr><th>h</th><th>i</th></tr>'
            '<tr><td style="background-color:#ff0000" colspan="2"><p>p1</p><ul><li>li</li></ul></td></tr></table>'
        ))
        markdown = render_markdown([table])
        self.assertEqual(markdown, '<table>\n<tr><th><p>h</p></th><th><p>i</p></th></tr>\n'
                                   '<tr><td style="background-color:#ff0000" colspan="2"><p>p1</p>'
                                   '<ul><li>li</li></ul></td></tr>\n</table>')
        self.assertEqual(_shape(_nodes(parse_markdown(markdown)), ids=False), _shape([table], ids=False))


class ObsidianVaultImportTests(unittest.TestCase):
    """A vault written by Obsidian itself (tests/fixtures/obsidian_vault)."""

    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.vault = directory / "vault"
        shutil.copytree(FIXTURES / "obsidian_vault", self.vault)
        self.db = directory / ".fortress.sqlite3"
        self.notes = {
            document_tree(self.db, document_id)["title"]: document_tree(self.db, document_id)
            for document_id in import_markdown_files(self.db, self.vault)
        }

    def test_notes_and_front_matter(self) -> None:
        self.assertEqual(sorted(self.notes), ["People", "Project Alpha", "Roadmap"])
        alpha = self.notes["Project Alpha"]["provenance"]
        self.assertEqual((alpha["source"], alpha["source_url"], alpha["source_created_at"]),
                         ("markdown_import", "https://example.com/alpha-brief", "2024-03-01T09:30"))

    def test_wikilinks_by_name_alias_path_heading_and_block_id(self) -> None:
        alpha, roadmap, people = (self.notes[name] for name in ("Project Alpha", "Roadmap", "People"))
        by_text = {node["text"]: node for node in _walk(roadmap["children"])}
        q3_heading, q3 = by_text["Q3 goals"], by_text["Ship the beta."]
        kickoff = alpha["children"][1]["content"]
        self.assertIn({"type": "docLink", "id": roadmap["id"]}, kickoff)
        self.assertIn({"type": "blockRef", "id": q3_heading["id"]}, kickoff)  # [[Roadmap#Q3 goals]]
        self.assertIn({"type": "docLink", "id": people["id"]}, kickoff)  # [[Team/People|people]]
        cell = [node for node in _walk(alpha["children"]) if node["type"] == "table_cell"][-1]
        self.assertEqual(cell["children"][0]["content"], [{"type": "blockRef", "id": q3["id"]}])
        alpha_text = {node["text"]: node for node in _walk(alpha["children"])}
        last = alpha["children"][-1]["content"]
        self.assertIn({"type": "blockRef", "id": alpha_text["The decision is final."]["id"]}, last)
        self.assertIn({"type": "blockRef", "id": alpha_text["Project Alpha"]["id"]}, last)
        self.assertIn({"type": "text", "text": ", and [[Nowhere]] is not written yet."}, last)
        see_also = roadmap["children"][-1]["content"]
        self.assertIn({"type": "blockRef", "id": alpha_text["Draft the plan"]["id"]}, see_also)
        self.assertIn({"type": "docLink", "id": alpha["id"]}, see_also)  # by alias
        # People and Roadmap (twice) link to it, and it links to itself twice.
        self.assertEqual(len(block_store.document_backlinks(self.db, alpha["id"])), 5)

    def test_obsidian_blocks(self) -> None:
        kinds = [(node["type"], node["attrs"]) for node in self.notes["Project Alpha"]["children"]]
        self.assertIn(("callout", {"callout_type": "warning", "callout_title": True}), kinds)
        self.assertIn(("callout", {"callout_type": "tip", "callout_fold": "-", "callout_title": True}), kinds)
        self.assertIn(("image", {"url": "diagram.png", "width": 300}), kinds)
        self.assertIn(("file", {"url": "Spec.pdf", "name": "Spec.pdf", "mime": "application/pdf"}), kinds)
        # $$…$$ keeps its source as a LaTeX code block (Fortress has no math blocks).
        self.assertIn(("code", {"language": "latex"}), kinds)


if __name__ == "__main__":
    unittest.main()
