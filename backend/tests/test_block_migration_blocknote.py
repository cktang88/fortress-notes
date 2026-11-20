"""Schema 14 on notes the BlockNote web editor, the Mac app and the importer wrote.

``fixtures/legacy_v13_blocknote.sql`` was written by the schema 13 code itself
(``scripts/make_legacy_blocknote_fixture.py``, run on the last commit before the
block model): the web editor's transactions with every BlockNote prop and style,
``[[id|Title]]`` / ``(( id "label" ))`` link hrefs and links typed as text, nested
list items, tables with header rows, spans and cell colours, media blocks; the Mac
app's requests of that time; and an imported page with super/subscript, a table with
cell images and lists, columns and math with MathML. It goes on top of
``legacy_v13.sql`` (its schema, and the "MD" note the links point at). The whole
migrated tree is asserted.

``LegacyShapeTests`` covers what the old code's own validation kept out of the
fixture but a hand-edited or older database may still hold.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import block_legacy, block_store, sources

FIXTURES = Path(__file__).parent / "fixtures"
MD = "01M7PJWSC9QMJN96X0KHB0X88K"
MD_HEADING = "01M7PJWSD9167NF1G544ZM7EY5"

WEB_EDITOR = [
    {
        "type": "heading",
        "attrs": {"level": 2},
        "content": [
            {"type": "text", "text": "Styled "},
            {"type": "text", "text": "heading", "marks": {"italic": True}},
        ],
    },
    {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "b", "marks": {"bold": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "i", "marks": {"italic": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "u", "marks": {"underline": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "s", "marks": {"strike": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "c", "marks": {"code": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "red", "marks": {"color": "red"}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "hl", "marks": {"highlight": "yellow"}},
            {"type": "text", "text": " "},
            {
                "type": "text",
                "text": "all",
                "marks": {
                    "bold": True,
                    "italic": True,
                    "underline": True,
                    "strike": True,
                    "color": "blue",
                    "highlight": "gray",
                },
            },
            {"type": "text", "text": " off"},
        ],
    },
    {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "See "},
            {
                "type": "link",
                "href": "https://example.com",
                "content": [{"type": "text", "text": "the site", "marks": {"bold": True}}],
            },
            {"type": "text", "text": ", "},
            {"type": "docLink", "id": MD},
            {"type": "text", "text": ", "},
            {"type": "blockRef", "id": MD_HEADING},
            {"type": "text", "text": ", typed "},
            {"type": "docLink", "id": MD},
            {"type": "text", "text": " "},
            {"type": "blockRef", "id": MD_HEADING},
            {"type": "text", "text": ", as code "},
            {"type": "text", "text": "[[01M7PJWSC9QMJN96X0KHB0X88K]]", "marks": {"code": True}},
            {"type": "text", "text": ", bare "},
            {"type": "docLink", "id": MD},
            {"type": "text", "text": " and unsafe."},
        ],
    },
    {"type": "paragraph"},
    {"type": "paragraph", "content": [{"type": "text", "text": "   "}]},
    {
        "type": "paragraph",
        "content": [{"type": "text", "text": "line one\nline two", "marks": {"bold": True}}],
    },
    {
        "type": "paragraph",
        "attrs": {"text_color": "purple", "background_color": "blue", "text_alignment": "center"},
        "content": [{"type": "text", "text": "centered"}],
    },
    {
        "type": "bullet_item",
        "content": [{"type": "text", "text": "outer bullet"}],
        "children": [
            {
                "type": "bullet_item",
                "content": [{"type": "text", "text": "inner bullet"}],
                "children": [
                    {
                        "type": "numbered_item",
                        "attrs": {"start": 3},
                        "content": [{"type": "text", "text": "deepest"}],
                    }
                ],
            }
        ],
    },
    {"type": "numbered_item", "content": [{"type": "text", "text": "first"}]},
    {"type": "numbered_item", "content": [{"type": "text", "text": "second"}]},
    {
        "type": "check_item",
        "attrs": {"checked": True},
        "content": [{"type": "text", "text": "done", "marks": {"strike": True}}],
    },
    {
        "type": "check_item",
        "attrs": {"checked": False},
        "content": [{"type": "text", "text": "todo"}],
        "children": [
            {
                "type": "check_item",
                "attrs": {"checked": False},
                "content": [{"type": "text", "text": "sub task"}],
            }
        ],
    },
    {"type": "bullet_item", "content": [{"type": "text", "text": "toggle"}]},
    {
        "type": "quote",
        "children": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "quoted "},
                    {"type": "text", "text": "bold", "marks": {"bold": True}},
                ],
            }
        ],
    },
    {
        "type": "callout",
        "children": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Heads up: "},
                    {
                        "type": "link",
                        "href": "https://x.org",
                        "content": [{"type": "text", "text": "x"}],
                    },
                ],
            }
        ],
    },
    {
        "type": "code",
        "attrs": {"language": "python"},
        "content": [{"type": "text", "text": "print('[[not a link]]')\nx = 1"}],
    },
    {"type": "code"},
    {"type": "code", "attrs": {"language": "latex"}},
    {
        "type": "image",
        "attrs": {
            "text_alignment": "center",
            "name": "photo.png",
            "url": "/media/01J0000000000000000000000B.png",
            "caption": "A caption",
            "width": 512,
        },
    },
    {"type": "file", "attrs": {"name": "clip.mp4", "url": "/media/01J0000000000000000000000C.mp4"}},
    {"type": "file", "attrs": {"name": "note.m4a", "url": "/media/01J0000000000000000000000D.m4a"}},
    {"type": "divider"},
    {"type": "image"},
    {
        "type": "table",
        "attrs": {"column_widths": [120, None, None], "header_rows": 1},
        "children": [
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "attrs": {"colspan": 2},
                        "children": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Wide header", "marks": {"bold": True}}
                                ],
                            }
                        ],
                    },
                    {
                        "type": "table_cell",
                        "children": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "H3"}]}
                        ],
                    },
                ],
            },
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "attrs": {
                            "color": "red",
                            "background": "yellow",
                            "align": "center",
                            "rowspan": 2,
                        },
                        "children": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "tall "},
                                    {
                                        "type": "link",
                                        "href": "https://example.com/t",
                                        "content": [{"type": "text", "text": "link"}],
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "type": "table_cell",
                        "children": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "plain"}]}
                        ],
                    },
                    {"type": "table_cell"},
                ],
            },
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "attrs": {"align": "right"},
                        "children": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "x", "marks": {"italic": True}}
                                ],
                            }
                        ],
                    },
                    {
                        "type": "table_cell",
                        "children": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "y"}]}
                        ],
                    },
                ],
            },
        ],
    },
    {
        "type": "heading",
        "attrs": {"level": 3, "is_toggleable": True},
        "content": [{"type": "text", "text": "Toggle heading"}],
        "children": [
            {"type": "paragraph", "content": [{"type": "text", "text": "under the toggle heading"}]}
        ],
    },
]

MAC_APP = [
    {
        "type": "heading",
        "attrs": {"level": 1},
        "content": [{"type": "text", "text": "From the Mac"}],
    },
    {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Note "},
            {"type": "docLink", "id": MD},
            {"type": "text", "text": ", block "},
            {"type": "blockRef", "id": MD_HEADING},
            {"type": "text", "text": ", typed "},
            {"type": "blockRef", "id": MD_HEADING},
            {"type": "text", "text": ", size"},
            {"type": "text", "text": " sup", "marks": {"sup": True}},
            {"type": "text", "text": " sub", "marks": {"sub": True}},
        ],
    },
    {
        "type": "code",
        "attrs": {"language": "swift"},
        "content": [{"type": "text", "text": "let x = 1"}],
    },
    {
        "type": "quote",
        "children": [
            {"type": "paragraph", "content": [{"type": "text", "text": "a line\nanother"}]}
        ],
    },
]

ONENOTE = [
    {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "E = mc"},
            {"type": "text", "text": "2", "marks": {"sup": True}},
            {"type": "text", "text": " and H"},
            {"type": "text", "text": "2", "marks": {"sub": True}},
            {"type": "text", "text": "O "},
            {"type": "text", "text": "bold", "marks": {"bold": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "it", "marks": {"italic": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "under", "marks": {"underline": True}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "gone", "marks": {"strike": True}},
            {"type": "text", "text": " mono "},
            {"type": "text", "text": "teal", "marks": {"color": "#008080"}},
            {"type": "text", "text": " "},
            {"type": "text", "text": "marked", "marks": {"highlight": "#FFFF00"}},
            {"type": "text", "text": " "},
            {
                "type": "link",
                "href": "https://onenote.example/page",
                "content": [{"type": "text", "text": "web"}],
            },
            {"type": "text", "text": " "},
            {"type": "docLink", "id": MD},
        ],
    },
    {
        "type": "table",
        "attrs": {
            "borders": True,
            "column_widths": [150, None],
            "header_rows": 1,
            "header_cols": 1,
        },
        "children": [
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "children": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Key", "marks": {"bold": True}}
                                ],
                            }
                        ],
                    },
                    {
                        "type": "table_cell",
                        "children": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Value"}]}
                        ],
                    },
                ],
            },
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "attrs": {"background": "#DEEAF6", "align": "center"},
                        "children": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "photo"}]}
                        ],
                    },
                    {
                        "type": "table_cell",
                        "children": [
                            {
                                "type": "image",
                                "attrs": {
                                    "url": "/media/01J0000000000000000000000E.png",
                                    "alt": "cell pic",
                                    "width": 200,
                                    "height": 100,
                                },
                            },
                            {
                                "type": "bullet_item",
                                "content": [{"type": "text", "text": "cell bullet"}],
                            },
                            {
                                "type": "numbered_item",
                                "content": [{"type": "text", "text": "cell number"}],
                            },
                        ],
                    },
                ],
            },
            {
                "type": "table_row",
                "children": [
                    {
                        "type": "table_cell",
                        "attrs": {"colspan": 2},
                        "children": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "spans both"}],
                            }
                        ],
                    }
                ],
            },
        ],
    },
    {
        "type": "columns",
        "children": [
            {
                "type": "column",
                "attrs": {"width": 0.6},
                "children": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "left column"}]},
                    {
                        "type": "check_item",
                        "attrs": {"checked": True},
                        "content": [{"type": "text", "text": "left task"}],
                    },
                ],
            },
            {
                "type": "column",
                "attrs": {"width": 0.4},
                "children": [
                    {
                        "type": "heading",
                        "attrs": {"level": 4},
                        "content": [{"type": "text", "text": "right heading"}],
                    }
                ],
            },
        ],
    },
    {
        "type": "code",
        "attrs": {"language": "latex"},
        "content": [{"type": "text", "text": "\\frac{a}{b}"}],
    },
    {
        "type": "file",
        "attrs": {
            "url": "/media/01J0000000000000000000000F.pdf",
            "name": "doc.pdf",
            "size": 4096,
            "mime": "application/pdf",
        },
    },
    {
        "type": "numbered_item",
        "content": [{"type": "text", "text": "step one"}],
        "children": [{"type": "bullet_item", "content": [{"type": "text", "text": "detail"}]}],
    },
    {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Edited "},
            {"type": "text", "text": "here", "marks": {"bold": True}},
        ],
    },
]


def _compact(nodes: list[dict]) -> list[dict]:
    result = []
    for node in nodes:
        item: dict = {"type": node["type"]}
        if node["attrs"]:
            item["attrs"] = node["attrs"]
        if node.get("content"):
            item["content"] = node["content"]
        if node.get("payload"):
            item["payload"] = node["payload"]
        if node["children"]:
            item["children"] = _compact(node["children"])
        result.append(item)
    return result


class BlockNoteMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / ".fortress.sqlite3"
        connection = sqlite3.connect(self.db)
        for name in ("legacy_v13.sql", "legacy_v13_blocknote.sql"):
            connection.executescript((FIXTURES / name).read_text(encoding="utf-8"))
        connection.close()
        block_store.initialize(self.db)
        self.ids = {item["title"]: item["id"] for item in block_store.list_documents(self.db)}

    def tree(self, title: str) -> dict:
        tree = block_store.document_tree(self.db, self.ids[title])
        assert tree is not None
        return tree

    def test_web_editor_note(self) -> None:
        tree = self.tree("Web editor")
        self.assertEqual(_compact(tree["children"]), WEB_EDITOR)
        self.assertEqual(
            [node["text"] for node in tree["children"]],
            [
                "Styled heading",
                "b i u s c red hl all off",
                f"See the site, MD, Title, typed MD Title, as code [[{MD}]], bare MD and unsafe.",
                "",
                "   ",
                "line one\nline two",
                "centered",
                "outer bullet",
                "first",
                "second",
                "done",
                "todo",
                "toggle",
                "",
                "",
                "print('[[not a link]]')\nx = 1",
                "",
                "",
                "A caption",
                "clip.mp4",
                "note.m4a",
                "",
                "",
                "",
                "Toggle heading",
            ],
        )
        # The editor's own block ids (UUIDs) are kept.
        self.assertEqual(tree["children"][1]["id"], "00000000-0000-4000-8000-000000000001")

    def test_mac_app_note(self) -> None:
        tree = self.tree("Mac app")
        self.assertEqual(_compact(tree["children"]), MAC_APP)
        self.assertEqual(
            [node["text"] for node in tree["children"]],
            ["From the Mac", "Note MD, block Title, typed Title, size sup sub", "let x = 1", ""],
        )

    def test_onenote_import(self) -> None:
        tree = self.tree("OneNote import")
        self.assertEqual(_compact(tree["children"]), ONENOTE)
        # ("mono" lost its code mark when the old importer stored it.)
        self.assertEqual(
            [node["text"] for node in tree["children"]],
            [
                "E = mc2 and H2O bold it under gone mono teal marked web MD",
                "",
                "",
                "\\frac{a}{b}",
                "doc.pdf",
                "step one",
                "Edited here",
            ],
        )

    def test_a_page_with_a_conflict_counts_as_edited_here(self) -> None:
        document_id = self.ids["OneNote import"]
        state = sources.sync_state(self.db, document_id)
        # Never refreshed over the local edit; its blocks keep their source ids.
        self.assertEqual(state["revision"], -1)
        self.assertIn(self.tree("OneNote import")["children"][-1]["id"], state["blocks"].values())

    def test_links_and_references_are_indexed(self) -> None:
        links_block = "00000000-0000-4000-8000-000000000002"
        mac_block = "00000000-0000-4000-8000-000000000101"
        onenote_first = self.tree("OneNote import")["children"][0]["id"]
        connection = sqlite3.connect(self.db)
        document_refs = connection.execute(
            "SELECT source_block_id, target_document_id FROM document_refs WHERE source_block_id IN (?, ?, ?)",
            (links_block, mac_block, onenote_first),
        ).fetchall()
        block_refs = connection.execute(
            "SELECT source_block_id, target_block_id FROM block_refs WHERE source_block_id IN (?, ?)",
            (links_block, mac_block),
        ).fetchall()
        connection.close()
        self.assertEqual(
            sorted(document_refs), sorted([(links_block, MD), (mac_block, MD), (onenote_first, MD)])
        )
        self.assertEqual(
            sorted(block_refs), sorted([(links_block, MD_HEADING), (mac_block, MD_HEADING)])
        )
        self.assertTrue(block_store.fts_is_consistent(self.db))
        self.assertEqual(
            [hit["text"] for hit in block_store.search_blocks(self.db, "deepest")], ["deepest"]
        )


def _convert(
    type_name: str,
    attrs: object = None,
    content: object = None,
    text: str = "",
    nested: list | None = None,
) -> list[dict]:
    return _spec_dicts(
        block_legacy.convert(type_name, attrs, content, text, nested_children=nested)
    )


def _spec_dicts(specs) -> list[dict]:
    result = []
    for spec in specs:
        item: dict = {"type": spec.type}
        if spec.attrs:
            item["attrs"] = spec.attrs
        if spec.content:
            item["content"] = spec.content
        if spec.payload:
            item["payload"] = spec.payload
        if spec.children:
            item["children"] = _spec_dicts(spec.children)
        result.append(item)
    return result


def _text(text: str, **marks) -> dict:
    return {"type": "text", "text": text, **({"marks": marks} if marks else {})}


class CorruptRowTests(unittest.TestCase):
    def test_a_row_with_unreadable_json_keeps_its_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / ".fortress.sqlite3"
            connection = sqlite3.connect(db)
            connection.executescript((FIXTURES / "legacy_v13.sql").read_text(encoding="utf-8"))
            connection.execute(
                """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
                       content_json, text, created_at, updated_at, provenance_json, revision)
                   VALUES ('01M7PJWSD9167NF1G544ZM7EZZ', ?, NULL, 99, 'paragraph', '{bad',
                           'not json', 'kept *text*', '2026-01-01', '2026-01-01', ?, NULL),
                          ('01M7PJWSD9167NF1G544ZM7EZY', ?, NULL, 100, 'paragraph', '[1]',
                           '[]', 'list attrs', '2026-01-01', '2026-01-01', NULL, NULL)""",
                (MD, '{"source": "onenote_import", "external_id": "X/1", "source_hash": "h"}', MD),
            )
            connection.commit()
            connection.close()
            block_store.initialize(db)
            unreadable, list_attrs = block_store.document_tree(db, MD)["children"][-2:]
            self.assertEqual(
                (unreadable["id"], unreadable["type"], unreadable["attrs"], unreadable["content"]),
                (
                    "01M7PJWSD9167NF1G544ZM7EZZ",
                    "paragraph",
                    {},
                    [_text("kept "), _text("text", italic=True)],
                ),
            )
            self.assertEqual(
                (list_attrs["attrs"], list_attrs["content"]), ({}, [_text("list attrs")])
            )


class LegacyShapeTests(unittest.TestCase):
    """Stored shapes the schema 13 code's validation would not write, read leniently."""

    def test_inline_values_of_any_shape(self) -> None:
        inline = block_legacy.inline
        self.assertEqual(inline(None), [])
        self.assertEqual(inline(42), [])
        self.assertEqual(inline(f"see [[{MD}|x]]"), [_text("see "), {"type": "docLink", "id": MD}])
        self.assertEqual(inline({"type": "text", "text": "one run"}), [_text("one run")])
        self.assertEqual(
            inline(
                [
                    "plain ",
                    7,
                    {"type": "docLink", "id": MD},
                    {"type": "blockRef"},
                    {"type": "blockRef", "id": MD_HEADING},
                ]
            ),
            [
                _text("plain "),
                {"type": "docLink", "id": MD},
                {"type": "blockRef", "id": MD_HEADING},
            ],
        )
        # A node with no text of its own: its content is read.
        self.assertEqual(
            inline([{"type": "mention", "content": [{"text": "inner"}]}, {"type": "x"}]),
            [_text("inner")],
        )
        # A link whose content is a string; one with no text; a bad colour is dropped.
        self.assertEqual(
            inline(
                [
                    {"type": "link", "href": "https://a.example", "content": "a"},
                    {"type": "link", "href": "https://b.example", "content": []},
                    {"text": "c", "styles": {"textColor": "not a colour", "bold": True}},
                ]
            ),
            [
                {"type": "link", "href": "https://a.example", "content": [_text("a")]},
                _text("c", bold=True),
            ],
        )

    def test_compact_runs_and_conflicting_marks(self) -> None:
        self.assertEqual(
            block_legacy.inline(
                [
                    {"text": "both", "sup": 1, "sub": 1},
                    {"text": "site", "link": "https://example.com", "b": 1},
                    {"text": "note", "link": f"[[{MD}|Note]]"},
                    {"text": "ref", "href": f"(( {MD_HEADING} ))"},
                    {"text": "unsafe", "link": "javascript:alert(1)"},
                ]
            ),
            [
                _text("both", sup=True),
                {
                    "type": "link",
                    "href": "https://example.com",
                    "content": [_text("site", bold=True)],
                },
                {"type": "docLink", "id": MD},
                {"type": "blockRef", "id": MD_HEADING},
                _text("unsafe"),
            ],
        )

    def test_attrs_are_cleaned_up(self) -> None:
        self.assertEqual(
            _convert("heading", {"level": 2.0}, {"blocknote": ["h"]}),
            [{"type": "heading", "attrs": {"level": 2}, "content": [_text("h")]}],
        )
        self.assertEqual(
            _convert("heading", {"level": 9}, {"blocknote": ["h"]})[0]["attrs"], {"level": 1}
        )
        self.assertEqual(
            _convert("heading", {"level": 2.5}, {"blocknote": ["h"]})[0]["attrs"], {"level": 1}
        )
        self.assertEqual(
            _convert("paragraph", ["not", "attrs"], {"blocknote": ["p"]}),
            [{"type": "paragraph", "content": [_text("p")]}],
        )
        self.assertEqual(
            _convert("callout", {"emoji": "!"}, {"blocknote": ["c"]})[0]["attrs"], {"icon": "!"}
        )
        # Extension keys become snake_case; one that isn't a name gets a prefix; bad values go.
        self.assertEqual(
            _convert(
                "paragraph", {"1st": "a", "dataX": 1, "textAlignment": "left"}, {"blocknote": []}
            ),
            [{"type": "paragraph", "attrs": {"x_1st": "a", "data_x": 1}}],
        )
        self.assertEqual(
            _convert("image", {"url": "/media/a.png", "width": -5})[0]["attrs"],
            {"url": "/media/a.png"},
        )

    def test_table_json_variants(self) -> None:
        table = {
            "headerRows": True,
            "columnWidths": [None, None],
            "rows": [
                "not a row",
                [
                    "bare cell",
                    {
                        "type": "tableCell",
                        "content": [_text("x")],
                        "props": {
                            "textAlignment": "left",
                            "colspan": 1,
                            "rowspan": 1,
                            "background": "#FF0000",
                        },
                    },
                ],
                {
                    "cells": [
                        {
                            "type": "tableCell",
                            "content": [],
                            "blocks": [
                                "skipped",
                                {
                                    "type": "list",
                                    "attrs": {"ordered": True},
                                    "content": {"blocknote": ["n"]},
                                    "children": [
                                        {"type": "list", "content": {"blocknote": ["deeper"]}},
                                        "skipped",
                                    ],
                                },
                            ],
                        }
                    ]
                },
            ],
        }
        self.assertEqual(
            _convert("table", {}, {"blocknote": table}),
            [
                {
                    "type": "table",
                    "attrs": {"header_rows": 1},
                    "children": [
                        {
                            "type": "table_row",
                            "children": [
                                {
                                    "type": "table_cell",
                                    "children": [
                                        {"type": "paragraph", "content": [_text("bare cell")]}
                                    ],
                                },
                                {
                                    "type": "table_cell",
                                    "attrs": {"background": "#FF0000"},
                                    "children": [{"type": "paragraph", "content": [_text("x")]}],
                                },
                            ],
                        },
                        {
                            "type": "table_row",
                            "children": [
                                {
                                    "type": "table_cell",
                                    "children": [
                                        {
                                            "type": "numbered_item",
                                            "content": [_text("n")],
                                            "children": [
                                                {
                                                    "type": "bullet_item",
                                                    "content": [_text("deeper")],
                                                }
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        )
        # A table stored as Markdown only (an import whose table didn't parse as JSON).
        self.assertEqual(
            _convert("table", {}, {"markdown": "| a |\n|---|\n| 1 |"}),
            [
                {
                    "type": "table",
                    "attrs": {"header_rows": 1},
                    "children": [
                        {
                            "type": "table_row",
                            "children": [
                                {
                                    "type": "table_cell",
                                    "children": [{"type": "paragraph", "content": [_text("a")]}],
                                }
                            ],
                        },
                        {
                            "type": "table_row",
                            "children": [
                                {
                                    "type": "table_cell",
                                    "children": [{"type": "paragraph", "content": [_text("1")]}],
                                }
                            ],
                        },
                    ],
                }
            ],
        )

    def test_markdown_only_blocks(self) -> None:
        # Raw HTML with no Markdown; an empty block; an unknown type keeps its name.
        self.assertEqual(
            _convert("raw", {}, {}, "<b>x</b>"), [{"type": "html", "payload": {"html": "<b>x</b>"}}]
        )
        self.assertEqual(
            _convert("heading", {"level": 3}, {"markdown": "  "}),
            [{"type": "heading", "attrs": {"level": 3}}],
        )
        self.assertEqual(_convert("divider", {}, {"markdown": ""}), [{"type": "divider"}])
        self.assertEqual(
            _convert("pageBreak", {"textColor": "red"}, {"markdown": "text"}),
            [
                {
                    "type": "paragraph",
                    "attrs": {"text_color": "red", "legacy_type": "page_break"},
                    "content": [_text("text")],
                }
            ],
        )
        # The stored type wins over what its text looks like (text edited without its marker).
        self.assertEqual(
            _convert("heading", {"level": 2}, {"markdown": "Renamed"}),
            [{"type": "heading", "attrs": {"level": 2}, "content": [_text("Renamed")]}],
        )
        self.assertEqual(
            _convert("list", {"ordered": True}, {"markdown": "edited"}),
            [{"type": "numbered_item", "content": [_text("edited")]}],
        )
        self.assertEqual(
            _convert("heading", {}, {"markdown": "## Kept level"}),
            [{"type": "heading", "attrs": {"level": 2}, "content": [_text("Kept level")]}],
        )
        # A check item keeps its stored state when its marker is gone.
        self.assertEqual(
            _convert("checkListItem", {"checked": True}, {"markdown": "done"}),
            [{"type": "check_item", "attrs": {"checked": True}, "content": [_text("done")]}],
        )
        self.assertEqual(
            _convert("list", {"checked": False}, {"markdown": "# not a heading"}),
            [
                {
                    "type": "check_item",
                    "attrs": {"checked": False},
                    "content": [_text("not a heading")],
                }
            ],
        )
        self.assertEqual(
            _convert("bulletListItem", {}, {"markdown": "```\ncode\n```"}),
            [{"type": "bullet_item", "content": [_text("```\ncode\n```")]}],
        )
        # Code without its fence is the code itself, not Markdown.
        self.assertEqual(
            _convert("codeBlock", {"language": "py"}, {"markdown": "# x = *a* * b"}),
            [{"type": "code", "attrs": {"language": "py"}, "content": [_text("# x = *a* * b")]}],
        )
        self.assertEqual(
            _convert("code", {"language": "js"}, {"markdown": "```js\nlet a\n```"}),
            [{"type": "code", "attrs": {"language": "js"}, "content": [_text("let a")]}],
        )
        self.assertEqual(
            _convert("codeBlock", {"language": "text"}, {"markdown": ""}), [{"type": "code"}]
        )
        # A quote or callout without ">" is still one.
        self.assertEqual(
            _convert("quote", {}, {"markdown": "plain line"}),
            [
                {
                    "type": "quote",
                    "children": [{"type": "paragraph", "content": [_text("plain line")]}],
                }
            ],
        )
        self.assertEqual(
            _convert("callout", {"emoji": "!"}, {"markdown": "note\n\n- b"}),
            [
                {
                    "type": "callout",
                    "attrs": {"icon": "!"},
                    "children": [
                        {"type": "paragraph", "content": [_text("note")]},
                        {"type": "bullet_item", "content": [_text("b")]},
                    ],
                }
            ],
        )
        # Math (Fortress has none now) keeps its formula as a LaTeX code block: stored
        # as Markdown only, the web editor's placeholder, and MathML only.
        self.assertEqual(
            _convert("math", {}, {"markdown": "$$\nx^2\n$$"}),
            [{"type": "code", "attrs": {"language": "latex"}, "content": [_text("x^2")]}],
        )
        self.assertEqual(
            _convert("mathPlaceholder", {}, {"markdown": "Math placeholder"}),
            [{"type": "code", "attrs": {"language": "latex"}}],
        )
        self.assertEqual(
            _convert("math", {}, {"mathml": "<math><mi>x</mi></math>"}),
            [{"type": "code", "attrs": {"language": "xml"},
              "content": [_text("<math><mi>x</mi></math>")]}],
        )


if __name__ == "__main__":
    unittest.main()
