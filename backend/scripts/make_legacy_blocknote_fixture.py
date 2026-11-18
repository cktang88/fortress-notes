"""Write tests/fixtures/legacy_v13_blocknote.sql with the code from before the block model.

Run from a checkout of the last pre-block-model commit (schema 13, ``4f88e98^``):

    git worktree add --detach /tmp/old 4f88e98^
    cd /tmp/old/backend && uv run python /path/to/this/script.py /path/to/legacy_v13_blocknote.sql

It stores two notes the way the old code did:

* "Web editor": the transactions the BlockNote web editor sent (``toOperation`` in
  the deleted ``frontend/src/features/notes/BlockNoteEditor.tsx``): BlockNote type
  names, every BlockNote prop (defaults included), ``content: {"blocknote", "markdown"}``,
  ``text``, numeric ``position``; nested list items as child blocks.
* "OneNote import": a source page the importer wrote through ``sources`` (runs in the
  compact importer form, tables with header rows, spans, cell blocks and images,
  columns, math with MathML).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

OUT = Path(sys.argv[1]).resolve()
directory = Path(tempfile.mkdtemp())
os.environ["NOTES_DIR"] = str(directory)
os.environ["EMBEDDINGS_ENABLED"] = "false"
os.environ["VISION_ENABLED"] = "false"
sys.path.insert(0, os.getcwd())

from app import block_store, sources
from app.models import BlockOperation

DB = directory / ".fortress.sqlite3"
NOW = "2026-11-18T06:00:00.000000+00:00"

DEFAULTS = {"textColor": "default", "backgroundColor": "default", "textAlignment": "left"}
TARGET_DOC = "01M7PJWSC9QMJN96X0KHB0X88K"  # "MD" in legacy_v13.sql
TARGET_BLOCK = "01M7PJWSD9167NF1G544ZM7EY5"  # its heading


def t(text: str, **styles) -> dict:
    return {"type": "text", "text": text, "styles": styles}


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def text_of(runs: list) -> str:
    out = []
    for run in runs:
        if run.get("type") == "link":
            out.append(text_of(run["content"]))
        else:
            out.append(run.get("text", ""))
    return "".join(out)


def insert(n, position, type_, props, runs=None, markdown="", parent=None, content=None):
    if content is None:
        content = {"blocknote": runs if runs is not None else [], "markdown": markdown}
    return BlockOperation(
        operation="insert",
        block_id=uid(n),
        parent_id=parent,
        position=position,
        type=type_,
        attrs=props,
        content=content,
        text=text_of(runs) if isinstance(runs, list) else "",
    )


def web_editor_note() -> None:
    created = block_store.create_block_document(DB, "Web editor", "rough", [], "")
    document_id = created["id"]
    first = created["children"][0]["id"]
    table = {
        "type": "tableContent",
        "columnWidths": [120, None, None],
        "headerRows": 1,
        "rows": [
            {
                "cells": [
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "colspan": 2, "rowspan": 1},
                        "content": [t("Wide header", bold=True)],
                    },
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "colspan": 1, "rowspan": 1},
                        "content": [t("H3")],
                    },
                ]
            },
            {
                "cells": [
                    {
                        "type": "tableCell",
                        "props": {
                            "textColor": "red",
                            "backgroundColor": "yellow",
                            "textAlignment": "center",
                            "colspan": 1,
                            "rowspan": 2,
                        },
                        "content": [
                            t("tall "),
                            {
                                "type": "link",
                                "href": "https://example.com/t",
                                "content": [t("link")],
                            },
                        ],
                    },
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "colspan": 1, "rowspan": 1},
                        "content": [t("plain")],
                    },
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "colspan": 1, "rowspan": 1},
                        "content": [],
                    },
                ]
            },
            {
                "cells": [
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "textAlignment": "right", "colspan": 1, "rowspan": 1},
                        "content": [t("x", italic=True)],
                    },
                    {
                        "type": "tableCell",
                        "props": {**DEFAULTS, "colspan": 1, "rowspan": 1},
                        "content": [t("y")],
                    },
                ]
            },
        ],
    }
    ops = [
        BlockOperation(
            operation="update",
            block_id=first,
            type="heading",
            attrs={**DEFAULTS, "level": 2, "isToggleable": False},
            content={
                "blocknote": [t("Styled "), t("heading", italic=True)],
                "markdown": "## Styled *heading*",
            },
            text="Styled heading",
        ),
        insert(
            1,
            1,
            "paragraph",
            DEFAULTS,
            [
                t("b", bold=True),
                t(" "),
                t("i", italic=True),
                t(" "),
                t("u", underline=True),
                t(" "),
                t("s", strike=True),
                t(" "),
                t("c", code=True),
                t(" "),
                t("red", textColor="red"),
                t(" "),
                t("hl", backgroundColor="yellow"),
                t(" "),
                t(
                    "all",
                    bold=True,
                    italic=True,
                    underline=True,
                    strike=True,
                    textColor="blue",
                    backgroundColor="gray",
                ),
                t(" off", bold=False, textColor="default", backgroundColor="default"),
            ],
            "**b** *i* <u>u</u> ~~s~~ `c` red hl all off",
        ),
        insert(
            2,
            2,
            "paragraph",
            DEFAULTS,
            [
                t("See "),
                {
                    "type": "link",
                    "href": "https://example.com",
                    "content": [t("the site", bold=True)],
                },
                t(", "),
                {
                    "type": "link",
                    "href": f"[[{TARGET_DOC}|Old title]]",
                    "content": [t("Old title")],
                },
                t(", "),
                {"type": "link", "href": f'(( {TARGET_BLOCK} "Title" ))', "content": [t("Title")]},
                t(", typed "),
                t(f"[[{TARGET_DOC}|Typed]]"),
                t(" "),
                t(f"(({TARGET_BLOCK}))", italic=True),
                t(", as code "),
                t(f"[[{TARGET_DOC}]]", code=True),
                t(", bare "),
                {"type": "link", "href": f"[[{TARGET_DOC}]]", "content": [t("bare")]},
                t(" and "),
                {"type": "link", "href": "javascript:alert(1)", "content": [t("unsafe")]},
                t("."),
            ],
            "See links",
        ),
        insert(3, 3, "paragraph", DEFAULTS, [], ""),
        insert(4, 4, "paragraph", DEFAULTS, [t("   ")], ""),
        insert(
            27,
            5,
            "paragraph",
            DEFAULTS,
            [t(""), t("line one\nline two", bold=True), t("")],
            "**line one\nline two**",
        ),
        insert(
            5,
            6,
            "paragraph",
            {
                **DEFAULTS,
                "textAlignment": "center",
                "textColor": "purple",
                "backgroundColor": "blue",
            },
            [t("centered")],
            "centered",
        ),
        insert(6, 7, "bulletListItem", DEFAULTS, [t("outer bullet")], "- outer bullet"),
        insert(
            7, 0, "bulletListItem", DEFAULTS, [t("inner bullet")], "- inner bullet", parent=uid(6)
        ),
        insert(
            8,
            0,
            "numberedListItem",
            {**DEFAULTS, "start": 3},
            [t("deepest")],
            "3. deepest",
            parent=uid(7),
        ),
        insert(9, 8, "numberedListItem", DEFAULTS, [t("first")], "1. first"),
        insert(10, 9, "numberedListItem", DEFAULTS, [t("second")], "2. second"),
        insert(
            11,
            10,
            "checkListItem",
            {**DEFAULTS, "checked": True},
            [t("done", strike=True)],
            "- [x] ~~done~~",
        ),
        insert(12, 11, "checkListItem", {**DEFAULTS, "checked": False}, [t("todo")], "- [ ] todo"),
        insert(
            13,
            0,
            "checkListItem",
            {**DEFAULTS, "checked": False},
            [t("sub task")],
            "- [ ] sub task",
            parent=uid(12),
        ),
        insert(14, 12, "toggleListItem", DEFAULTS, [t("toggle")], "- toggle"),
        insert(
            15,
            13,
            "quote",
            {"textColor": "default", "backgroundColor": "default"},
            [t("quoted "), t("bold", bold=True)],
            "> quoted **bold**",
        ),
        insert(
            16,
            14,
            "callout",
            {},
            [t("Heads up: "), {"type": "link", "href": "https://x.org", "content": [t("x")]}],
            "Heads up: x",
        ),
        insert(
            17,
            15,
            "codeBlock",
            {"language": "python"},
            [t("print('[[not a link]]')\nx = 1")],
            "```python\nprint('[[not a link]]')\nx = 1\n```",
        ),
        insert(18, 16, "codeBlock", {"language": "text"}, [], "```\n```"),
        insert(
            19,
            17,
            "mathPlaceholder",
            {},
            None,
            content={"blocknote": None, "markdown": "Math placeholder"},
        ),
        insert(
            20,
            18,
            "image",
            {
                "textAlignment": "center",
                "backgroundColor": "default",
                "name": "photo.png",
                "url": "/media/01J0000000000000000000000B.png",
                "caption": "A caption",
                "showPreview": True,
                "previewWidth": 512,
            },
            None,
            content={"markdown": "![photo.png](/media/01J0000000000000000000000B.png)"},
        ),
        insert(
            21,
            19,
            "video",
            {
                "textAlignment": "left",
                "backgroundColor": "default",
                "name": "clip.mp4",
                "url": "/media/01J0000000000000000000000C.mp4",
                "caption": "",
                "showPreview": True,
                "previewWidth": 320,
            },
            None,
            content={"markdown": "[clip.mp4](/media/01J0000000000000000000000C.mp4)"},
        ),
        insert(
            22,
            20,
            "audio",
            {
                "backgroundColor": "default",
                "name": "note.m4a",
                "url": "/media/01J0000000000000000000000D.m4a",
                "caption": "",
                "showPreview": True,
            },
            None,
            content={"markdown": "[note.m4a](/media/01J0000000000000000000000D.m4a)"},
        ),
        insert(23, 21, "divider", {}, None, content={"markdown": "---"}),
        # An image inserted from the slash menu and never given a file.
        insert(
            28,
            22,
            "image",
            {
                "textAlignment": "left",
                "backgroundColor": "default",
                "name": "",
                "url": "",
                "caption": "",
                "showPreview": True,
            },
            None,
            content={"markdown": ""},
        ),
        insert(
            24,
            23,
            "table",
            {"textColor": "default"},
            None,
            content={"blocknote": table, "markdown": "| Wide header | | H3 |"},
        ),
        insert(
            25,
            24,
            "heading",
            {**DEFAULTS, "level": 3, "isToggleable": True},
            [t("Toggle heading")],
            "### Toggle heading",
        ),
        insert(
            26,
            0,
            "paragraph",
            DEFAULTS,
            [t("under the toggle heading")],
            "under the toggle heading",
            parent=uid(25),
        ),
    ]
    revision = created["revision"]
    for operation in ops:
        revision = block_store.apply_transaction(DB, document_id, [operation], revision)["revision"]


def mac_app_note() -> None:
    """What the Mac app sent before the block model (``BlockCodec.encode`` then)."""

    created = block_store.create_block_document(DB, "Mac app", "rough", [], "")
    document_id = created["id"]
    first = created["children"][0]["id"]
    ops = [
        BlockOperation(
            operation="update",
            block_id=first,
            type="heading",
            attrs={"level": 1},
            content={"blocknote": [t("From the Mac")], "markdown": "# From the Mac"},
            text="From the Mac",
        ),
        BlockOperation(
            operation="insert",
            block_id=uid(101),
            position=1,
            type="paragraph",
            attrs={},
            content={
                "blocknote": [
                    t("Note "),
                    {
                        "type": "link",
                        "href": f"[[{TARGET_DOC}|MD note]]",
                        "content": [t("MD ", bold=True), t("note", bold=True, italic=True)],
                    },
                    t(", block "),
                    {"type": "link", "href": f"(( {TARGET_BLOCK} ))", "content": [t("block")]},
                    t(", typed "),
                    t(f"(( {TARGET_BLOCK} 'Title' ))"),
                    t(", size", fontSize="12px"),
                    t(" sup", superscript=True),
                    t(" sub", subscript=True),
                ],
                "markdown": "Note [MD note]([[x]])",
            },
            text="Note MD note, block block, typed , size sup sub",
        ),
        BlockOperation(
            operation="insert",
            block_id=uid(102),
            position=2,
            type="codeBlock",
            attrs={"language": "swift"},
            content={"blocknote": [t("let x = 1")], "markdown": "```swift\nlet x = 1\n```"},
            text="let x = 1",
        ),
        BlockOperation(
            operation="insert",
            block_id=uid(103),
            position=3,
            type="quote",
            attrs={},
            content={"blocknote": [t("a line\nanother")], "markdown": "> a line\n> another"},
            text="a line\nanother",
        ),
    ]
    revision = created["revision"]
    for operation in ops:
        revision = block_store.apply_transaction(DB, document_id, [operation], revision)["revision"]


def onenote_note() -> None:
    page = [
        {
            "id": "N/1",
            "type": "paragraph",
            "inline": [
                {"text": "E = mc"},
                {"text": "2", "sup": 1},
                {"text": " and H"},
                {"text": "2", "sub": 1},
                {"text": "O "},
                {"text": "bold", "b": 1},
                {"text": " "},
                {"text": "it", "i": 1},
                {"text": " "},
                {"text": "under", "u": 1},
                {"text": " "},
                {"text": "gone", "s": 1},
                {"text": " "},
                {"text": "mono", "code": 1},
                {"text": " "},
                {"text": "teal", "color": "#008080"},
                {"text": " "},
                {"text": "marked", "highlight": "#FFFF00"},
                {"text": " "},
                {"text": "web", "link": "https://onenote.example/page"},
                {"text": " "},
                {"text": "Linked page", "link": f"[[{TARGET_DOC}|Linked page]]"},
            ],
        },
        {
            "id": "N/2",
            "type": "table",
            "column_widths": [150, None],
            "header_rows": 1,
            "header_cols": 1,
            "borders": True,
            "rows": [
                [{"inline": [{"text": "Key", "b": 1}]}, {"inline": [{"text": "Value"}]}],
                [
                    {"text": "photo", "background": "#DEEAF6", "align": "center"},
                    {
                        "text": "",
                        "blocks": [
                            {
                                "type": "image",
                                "url": "/media/01J0000000000000000000000E.png",
                                "alt": "cell pic",
                                "width": 200,
                                "height": 100,
                            },
                            {"type": "list", "inline": [{"text": "cell bullet"}]},
                            {"type": "list", "ordered": True, "inline": [{"text": "cell number"}]},
                        ],
                    },
                ],
                [{"text": "spans both", "colspan": 2}],
            ],
        },
        {
            "id": "N/3",
            "type": "columns",
            "columns": [
                {
                    "width": 0.6,
                    "blocks": [
                        {"type": "paragraph", "text": "left column"},
                        {"type": "list", "checked": True, "text": "left task"},
                    ],
                },
                {
                    "width": 0.4,
                    "blocks": [{"type": "heading", "level": 4, "text": "right heading"}],
                },
            ],
        },
        {
            "id": "N/4",
            "type": "math",
            "latex": "\\frac{a}{b}",
            "mathml": "<math><mfrac><mi>a</mi><mi>b</mi></mfrac></math>",
        },
        {
            "id": "N/5",
            "type": "file",
            "url": "/media/01J0000000000000000000000F.pdf",
            "name": "doc.pdf",
            "size": 4096,
            "mime": "application/pdf",
        },
        {
            "id": "N/6",
            "type": "list",
            "ordered": True,
            "inline": [{"text": "step one"}],
            "children": [
                {"id": "N/7", "type": "list", "inline": [{"text": "detail"}]},
            ],
        },
    ]
    page.append(
        {"id": "N/8", "markdown": "Original *source* line", "modified_at": "2026-11-01T10:00:00Z"}
    )
    result = sources.upsert_document_from_source(
        DB,
        source="onenote_import",
        external_id="NPAGE",
        title="OneNote import",
        blocks=sources.source_blocks(page),
    )
    # Edited here, then changed in OneNote too: a conflict on a Markdown-only block.
    tree = block_store.document_tree(DB, result.document_id)
    edited = tree["children"][-1]["id"]
    block_store.apply_transaction(
        DB,
        result.document_id,
        [
            BlockOperation(
                operation="update",
                block_id=edited,
                type="paragraph",
                attrs=dict(DEFAULTS),
                content={
                    "blocknote": [t("Edited "), t("here", bold=True)],
                    "markdown": "Edited **here**",
                },
                text="Edited here",
            )
        ],
        tree["revision"],
    )
    page[-1] = {
        "id": "N/8",
        "markdown": "Changed *source* line",
        "modified_at": "2026-11-02T10:00:00Z",
    }
    sources.upsert_document_from_source(
        DB,
        source="onenote_import",
        external_id="NPAGE",
        title="OneNote import",
        blocks=sources.source_blocks(page),
    )


web_editor_note()
mac_app_note()
onenote_note()

connection = sqlite3.connect(DB)
documents = [
    row[0]
    for row in connection.execute(
        "SELECT id FROM documents WHERE title IN ('Web editor', 'Mac app', 'OneNote import')"
    )
]
lines = [
    "-- Written by backend/scripts/make_legacy_blocknote_fixture.py with the schema 13 code;",
    "-- applied on top of legacy_v13.sql (its schema and the 'MD' note the links point at).",
    "BEGIN TRANSACTION;",
]
for table, column in (("documents", "id"), ("blocks", "document_id")):
    marks = ",".join("?" for _ in documents)
    cursor = connection.execute(
        f"SELECT * FROM {table} WHERE {column} IN ({marks}) "
        + (
            "ORDER BY document_id, parent_id IS NOT NULL, parent_id, position"
            if table == "blocks"
            else ""
        ),
        documents,
    )
    names = ",".join(item[0] for item in cursor.description)
    for row in cursor:
        values = ",".join(
            "NULL"
            if value is None
            else str(value)
            if isinstance(value, (int, float))
            else "'" + str(value).replace("'", "''") + "'"
            for value in row
        )
        lines.append(f'INSERT INTO "{table}"({names}) VALUES({values});')
lines.append("COMMIT;")
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
