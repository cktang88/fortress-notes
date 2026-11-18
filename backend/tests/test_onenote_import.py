"""OneNote importer: manifest -> folders/notes/blocks, re-import, links, job API.

Unit tests use a hand-written manifest (tools/fortress-onenote/SCHEMA.md, format
version 1) and never need the converter binary. ``OneNoteSamplesTests`` runs the
real converter on public sample files when ``FORTRESS_ONENOTE_SAMPLES`` points
at them and the binary is built.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import block_store, documents, images, onenote_import
from app.block_store import apply_transaction, document_tree, search_blocks
from app.models import BlockOperation
from app.portability import export_document_markdown

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"
PDF = b"%PDF-1.4 fake pdf"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M0 0L9 9"/></svg>'
HTML = b"<script>alert(1)</script>"


def _asset(data: bytes, ext: str, mime: str) -> tuple[str, dict]:
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest}.{ext}"
    return name, {"path": f"assets/{name}", "sha256": digest, "size": len(data), "mime": mime, "ext": ext}


PNG_NAME, PNG_INFO = _asset(PNG, "png", "image/png")
PDF_NAME, PDF_INFO = _asset(PDF, "pdf", "application/pdf")
SVG_NAME, SVG_INFO = _asset(SVG, "svg", "image/svg+xml")
HTML_NAME, HTML_INFO = _asset(HTML, "html", "text/html")

P1 = "{11111111-1111-1111-1111-111111111111}"
P2 = "{22222222-2222-2222-2222-222222222222}"
P2B = "{2B2B2B2B-2B2B-2B2B-2B2B-2B2B2B2B2B2B}"
P3 = "{33333333-3333-3333-3333-333333333333}"
UNKNOWN = "{99999999-9999-9999-9999-999999999999}"


def _page(guid, title, items, layout=None, *, level=0, parent=None, order=0, path=("Work", "Projects", "Alpha"),
          derived=False, source_url=None):
    return {
        "id": guid, "link_target_id": guid, "title": title, "title_derived": derived,
        "level": level, "parent_id": parent, "order": order,
        "created": "2024-01-02T03:04:05.000Z", "updated": "2024-02-03T04:05:06.000Z",
        "author": "Tester", "source_path": [*path, title], "source_url": source_url,
        "height": None, "ink_text": None, "items": items,
        "layout": layout if layout is not None else [{"kind": "item", "item": item["id"]} for item in items],
        "stats": {"blocks": 0, "text_chars": 0},
    }


def _outline(item_id, blocks, x=1.0, y=1.0):
    return {"id": item_id, "kind": "outline", "x": x, "y": y, "width": 10.0, "width_set_by_user": False,
            "est_width": 10.0, "est_height": 2.0, "is_background": False, "source_url": None, "blocks": blocks}


def _onenote_link(start, end, page_id, title, object_id=None):
    href = f"onenote:#{title}&section-id={{AAAA}}&page-id={page_id}&end"
    return {"start": start, "end": end, "type": "link", "href": href,
            "onenote": {"page_id": page_id, "section_id": None, "object_id": object_id,
                        "section_file": None, "page_title": title}}


def build_manifest() -> dict:
    marks_text = "Bold italic under strike x2 H2O red marked web Standup child elsewhere"
    def span(word):
        start = marks_text.index(word)
        return start, start + len(word)

    rich_marks = [
        {"start": span("Bold")[0], "end": span("Bold")[1], "type": "bold"},
        {"start": span("italic")[0], "end": span("italic")[1], "type": "italic"},
        {"start": span("under")[0], "end": span("under")[1], "type": "underline"},
        {"start": span("strike")[0], "end": span("strike")[1], "type": "strikethrough"},
        {"start": span("x2")[0] + 1, "end": span("x2")[1], "type": "superscript"},
        {"start": span("H2O")[0] + 1, "end": span("H2O")[0] + 2, "type": "subscript"},
        {"start": span("red")[0], "end": span("red")[1], "type": "color", "color": "#C00000"},
        {"start": span("marked")[0], "end": span("marked")[1], "type": "highlight", "color": "#FFFF00"},
        {"start": span("web")[0], "end": span("web")[1], "type": "link", "href": "https://example.com/"},
        _onenote_link(*span("Standup"), P3, "Standup"),
        _onenote_link(*span("child"), P2, "Child", object_id="{ABCDEF00-0000-0000-0000-000000000001}"),
        _onenote_link(*span("elsewhere"), UNKNOWN, "Other notebook page"),
    ]
    plan_outline = _outline("i0", [
        {"type": "heading", "indent": 0, "level": 2, "text": "Plan heading"},
        {"type": "paragraph", "indent": 0, "text": marks_text, "marks": rich_marks},
        {"type": "list_item", "indent": 0, "list": {"ordered": False, "style": "bullet", "marker": "•", "restart": None},
         "text": "Parent item"},
        {"type": "list_item", "indent": 1, "list": {"ordered": True, "style": "decimal", "marker": "%.", "restart": None},
         "text": "Nested item"},
        {"type": "check_item", "indent": 0, "checked": True, "checked_at": "2024-01-05T00:00:00Z", "list": None,
         "text": "Done task",
         "tags": [{"label": "To Do", "shape": "blue_check_box", "checkable": True, "completed": True,
                   "completed_at": None, "highlight_color": None, "text_color": None},
                  {"label": "Important", "shape": "yellow_star", "checkable": False, "completed": False,
                   "completed_at": None, "highlight_color": None, "text_color": None}]},
        {"type": "paragraph", "indent": 0, "text": "Editable paragraph"},
    ])
    left = _outline("i1", [{"type": "paragraph", "indent": 0, "text": "Left column text"}], x=1, y=5)
    right = {"id": "i2", "kind": "image", "x": 12, "y": 5, "width": 5.0, "width_set_by_user": False,
             "est_width": 5.0, "est_height": 3.0, "is_background": False, "source_url": None,
             "blocks": [{"type": "image", "indent": 0, "asset": PNG_NAME, "data_status": "available",
                         "alt_text": "Quarterly revenue chart OCR", "width": 5.0, "height": 2.5, "hyperlink": None,
                         "is_printout_page": False, "printout_page_number": None, "is_background": False,
                         "original_name": "chart.png", "original_asset": None}]}
    nested_table = {"type": "table", "indent": 0, "row_count": 1, "col_count": 1, "column_widths": [1.0],
                    "borders_visible": False,
                    "rows": [{"cells": [{"background": None, "blocks": [
                        {"type": "paragraph", "indent": 0, "text": "deep cell"}]}]}]}
    table = _outline("i3", [{
        "type": "table", "indent": 0, "row_count": 2, "col_count": 2, "column_widths": [2.0, 3.0],
        "borders_visible": True,
        "rows": [
            {"cells": [
                {"background": "#DEEAF6", "blocks": [{"type": "paragraph", "indent": 0, "text": "Name",
                                                      "marks": [{"start": 0, "end": 4, "type": "bold"}]}]},
                {"background": None, "blocks": [{"type": "paragraph", "indent": 0, "text": "Qty"}]},
            ]},
            {"cells": [
                {"background": None, "blocks": [
                    {"type": "paragraph", "indent": 0, "text": "Apples"},
                    {"type": "list_item", "indent": 0, "list": {"ordered": False, "style": "bullet", "marker": "•",
                                                                "restart": None}, "text": "in cell list"},
                    nested_table,
                ]},
                {"background": "#FFF2CC", "blocks": [{"type": "paragraph", "indent": 0, "text": "3"}]},
            ]},
        ],
    }], y=9)
    plan = _page(P1, "Plan", [plan_outline, left, right, table], layout=[
        {"kind": "item", "item": "i0"},
        {"kind": "columns", "scattered": False, "columns": [
            {"width": 0.4, "x": 1, "span": 10, "rows": [{"kind": "item", "item": "i1"}]},
            {"width": 0.6, "x": 12, "span": 5, "rows": [{"kind": "item", "item": "i2"}]},
        ]},
        {"kind": "item", "item": "i3"},
    ])
    child = _page(P2, "Child", [_outline("i0", [{"type": "paragraph", "indent": 0, "text": "Child body"}])],
                  level=1, parent=P1, order=1)
    grandchild = _page(P2B, "Grandchild", [_outline("i0", [{"type": "paragraph", "indent": 0,
                                                            "text": "Grandchild body"}])],
                       level=2, parent=P2, order=2)
    standup_blocks = [
        {"type": "paragraph", "indent": 0, "text": "Standup notes"},
        {"type": "code", "indent": 0, "text": "print('hi')\nprint('there')"},
        {"type": "quote", "indent": 0, "variant": "quote", "text": "A wise quote"},
        {"type": "math", "indent": 0, "text": "a=b", "latex": "\\[a=b\\]", "mathml": "<math><mi>a</mi></math>"},
        {"type": "file", "indent": 0, "asset": PDF_NAME, "filename": "Contract.pdf", "extension": "pdf",
         "mime": "application/pdf", "size": len(PDF), "data_status": "available", "media_kind": "other",
         "icon_asset": None},
        {"type": "file", "indent": 0, "asset": HTML_NAME, "filename": "page.html", "extension": "html",
         "mime": "text/html", "size": len(HTML), "data_status": "available", "media_kind": "other",
         "icon_asset": None},
        {"type": "file", "indent": 0, "asset": None, "filename": "Lost.docx", "extension": "docx",
         "mime": "application/octet-stream", "size": 0, "data_status": "missing", "media_kind": "other",
         "icon_asset": None},
        {"type": "embed", "indent": 0, "url": "https://www.youtube.com/watch?v=xyz", "thumbnail_asset": None},
        {"type": "ink", "indent": 0, "asset": SVG_NAME, "recognized_text": "handwritten words", "width": 4.0,
         "height": 2.0, "stroke_count": 3},
        {"type": "hologram", "indent": 0, "text": "future content"},
    ]
    standup = _page(P3, "Standup notes", [_outline("i0", standup_blocks)], path=("Work", "Meetings"),
                    derived=True, source_url="https://example.org/source")
    report = {
        "counts": {}, "tags_by_label": {}, "warnings": [
            {"message": "embedded file has no data container", "page_id": P3, "page_title": "Standup notes"}],
        "failed_sections": [{"path": ["Work", "Locked"], "file": "Locked.one",
                             "reason": "The section is password protected.",
                             "hint": "Remove the password in OneNote and export again.", "error": "encrypted"}],
        "missing_data": [], "layout_notes": [],
    }
    return {
        "format": "fortress-onenote-manifest",
        "format_version": 1,
        "generator": {"name": "fortress-onenote", "version": "0.1.0", "parser": "onenote_parser 2.1"},
        "units": {"text_offsets": "code_points", "geometry": "half_inch", "px_per_geometry_unit": 48.0},
        "input": {"kind": "zip", "name": "Work.zip"},
        "notebooks": [{
            "id": "nb-1", "file_identity": "nb-1", "name": "Work", "color": None, "source_kind": "onetoc2",
            "source_path": "Work",
            "children": [
                {"kind": "section_group", "id": "g-1", "file_identity": "g-1", "name": "Projects",
                 "path": ["Work", "Projects"], "children": [
                     {"kind": "section", "id": "s-1", "file_identity": "s-1", "name": "Alpha", "color": None,
                      "path": ["Work", "Projects", "Alpha"], "in_toc": True,
                      "pages": [plan, child, grandchild], "warnings": []}]},
                {"kind": "section", "id": "s-2", "file_identity": "s-2", "name": "Meetings", "color": None,
                 "path": ["Work", "Meetings"], "in_toc": True, "pages": [standup], "warnings": []},
            ],
            "report": report,
        }],
        "assets": {PNG_NAME: PNG_INFO, PDF_NAME: PDF_INFO, SVG_NAME: SVG_INFO, HTML_NAME: HTML_INFO},
        "report": {"export_errors": [{"file": "errors.txt", "text": "Section X failed to export"}],
                   "input_warnings": [], "skipped": [], "notebooks": 1, "sections": 2, "sections_failed": 1,
                   "pages": 4, "assets": 4, "assets_bytes": 0},
    }


def write_manifest(directory: Path, manifest: dict) -> Path:
    (directory / "assets").mkdir(parents=True, exist_ok=True)
    for name, data in ((PNG_NAME, PNG), (PDF_NAME, PDF), (SVG_NAME, SVG), (HTML_NAME, HTML)):
        (directory / "assets" / name).write_bytes(data)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def _find(nodes, predicate):
    return next((node for node in _walk(nodes) if predicate(node)), None)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=self.notes / ".fortress.sqlite3",
            notes_path=self.notes,
            assets_path=self.notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            vlm_caption_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        self.settings.assets_path.mkdir()
        for item in (
            patch.object(documents, "get_settings", lambda: self.settings),
            patch.object(images, "get_settings", lambda: self.settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.db = self.settings.block_db_path
        self.manifest = build_manifest()
        self.export = write_manifest(self.root / "export", self.manifest)

    def run_import(self, manifest: dict | None = None, folder_id: str | None = None) -> dict:
        if manifest is not None:
            write_manifest(self.export, manifest)
        return onenote_import.import_manifest(
            self.export, db_path=self.db, notes_path=self.notes, assets_path=self.settings.assets_path,
            folder_id=folder_id,
        )

    def doc_id(self, guid: str) -> str:
        with block_store.connection_scope(self.db) as connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE source_kind = 'onenote_import' AND source_external_id = ?",
                (guid,),
            ).fetchone()
        return row["id"]

    def tree(self, guid: str) -> dict:
        tree = document_tree(self.db, self.doc_id(guid))
        assert tree is not None
        return tree


class ImportManifestTests(_Base):
    def test_folder_tree_follows_notebook_groups_and_sections(self) -> None:
        result = self.run_import()
        navigation = block_store.navigation(self.db)["items"]
        [work] = [item for item in navigation if item["kind"] == "folder"]
        self.assertEqual(work["name"], "Work")
        names = {child["name"]: child for child in work["children"] if child["kind"] == "folder"}
        self.assertEqual(set(names), {"Projects", "Meetings"})
        [alpha] = [child for child in names["Projects"]["children"] if child["kind"] == "folder"]
        self.assertEqual(alpha["name"], "Alpha")
        titles = [child["title"] for child in alpha["children"] if child["kind"] == "document"]
        self.assertEqual(titles, ["Plan", "Child", "Grandchild"])  # OneNote order, subpages after parent
        [notebook] = result["notebooks"]
        self.assertEqual(notebook["folder_id"], work["id"])
        self.assertEqual(notebook["sections"], 2)
        self.assertEqual(notebook["failed_sections"], [{
            "path": "Work › Locked", "reason": "The section is password protected.",
            "hint": "Remove the password in OneNote and export again."}])
        self.assertEqual(result["errors_txt"], "Section X failed to export")
        self.assertTrue(any("embedded file has no data container" in w for w in notebook["warnings"]))
        self.assertTrue(any("hologram" in w for w in notebook["warnings"]))

    def test_import_under_a_chosen_folder(self) -> None:
        parent = block_store.create_folder(self.db, "Imports", None)["id"]
        result = self.run_import(folder_id=parent)
        navigation = block_store.navigation(self.db)["items"]
        [imports] = [item for item in navigation if item["kind"] == "folder"]
        self.assertEqual([child["name"] for child in imports["children"]], ["Work"])
        self.assertEqual(result["notebooks"][0]["folder_id"], imports["children"][0]["id"])

    def test_pages_are_created_then_unchanged_on_reimport(self) -> None:
        first = self.run_import()["notebooks"][0]
        self.assertEqual((first["pages_created"], first["pages_updated"], first["pages_unchanged"]), (4, 0, 0))
        self.assertEqual((first["images"], first["files"], first["tables"], first["columns_pages"]), (1, 3, 2, 1))
        ids = {guid: self.doc_id(guid) for guid in (P1, P2, P2B, P3)}
        second = self.run_import()["notebooks"][0]
        self.assertEqual((second["pages_created"], second["pages_updated"], second["pages_unchanged"]), (0, 0, 4))
        self.assertEqual({guid: self.doc_id(guid) for guid in ids}, ids)
        plan = self.tree(P1)
        self.assertEqual(plan["created_at"], "2024-01-02T03:04:05.000Z")
        provenance = plan["provenance"]
        self.assertEqual(provenance["source"], "onenote_import")
        self.assertEqual(provenance["source_path"], "Work › Projects › Alpha › Plan")
        self.assertEqual(provenance["source_modified_at"], "2024-02-03T04:05:06.000Z")
        self.assertEqual(provenance["importer"]["name"], "fortress-onenote-import")
        standup = self.tree(P3)["provenance"]
        self.assertEqual(standup["source_url"], "https://example.org/source")
        self.assertTrue(standup["extra"]["onenote"]["title_derived"])
        self.assertTrue((self.notes / f"{ids[P1]}.md").is_file())  # Markdown mirror

    def test_local_edit_survives_reimport_and_source_change_is_a_conflict(self) -> None:
        self.run_import()
        tree = self.tree(P1)
        block = _find(tree["children"], lambda node: node["text"] == "Editable paragraph")
        apply_transaction(self.db, tree["id"], [BlockOperation(
            operation="update", block_id=block["id"], text="Edited in Fortress",
            content={"markdown": "Edited in Fortress"})], tree["revision"])
        changed = copy.deepcopy(self.manifest)
        page = changed["notebooks"][0]["children"][0]["children"][0]["pages"][0]
        page["items"][0]["blocks"][0]["text"] = "Plan heading v2"
        result = self.run_import(changed)["notebooks"][0]
        self.assertEqual(result["pages_updated"], 1)
        self.assertEqual(result["pages_unchanged"], 3)
        tree = self.tree(P1)
        texts = [node["text"] for node in _walk(tree["children"])]
        self.assertIn("Edited in Fortress", texts)
        self.assertNotIn("Editable paragraph", texts)
        self.assertIn("Plan heading v2", texts)
        self.assertNotIn("Plan heading", texts)
        # Unchanged source again: still unchanged, the local edit stays.
        again = self.run_import(changed)["notebooks"][0]
        self.assertEqual(again["pages_unchanged"], 4)
        self.assertIn("Edited in Fortress", [node["text"] for node in _walk(self.tree(P1)["children"])])

    def test_columns_tables_and_marks(self) -> None:
        self.run_import()
        tree = self.tree(P1)
        top_types = [node["type"] for node in tree["children"]]
        self.assertEqual(top_types[0], "heading")
        columns = _find(tree["children"], lambda node: node["type"] == "columns")
        self.assertEqual([column["type"] for column in columns["children"]], ["column", "column"])
        self.assertEqual([column["attrs"]["width"] for column in columns["children"]], [0.4, 0.6])
        self.assertEqual(columns["children"][0]["children"][0]["text"], "Left column text")
        image = columns["children"][1]["children"][0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["attrs"]["alt"], "Quarterly revenue chart OCR")
        self.assertEqual(image["attrs"]["width"], 240)  # 5 half-inch units * 48 px

        table = _find(tree["children"], lambda node: node["type"] == "table")
        content = table["content"]["blocknote"]
        self.assertEqual(content["columnWidths"], [96, 144])
        self.assertTrue(table["attrs"]["borders"])
        first, second = content["rows"]
        self.assertEqual(first["cells"][0]["props"]["backgroundColor"], "#DEEAF6")
        self.assertEqual(first["cells"][0]["content"][0]["styles"], {"bold": True})
        self.assertEqual(second["cells"][1]["props"]["backgroundColor"], "#FFF2CC")
        nested = second["cells"][0]["blocks"]
        self.assertEqual(second["cells"][0]["content"][0]["text"], "Apples")
        self.assertEqual([block["type"] for block in nested], ["list", "table"])
        self.assertFalse(nested[1]["attrs"]["borders"])
        self.assertIn("deep cell", table["text"])

        paragraph = _find(tree["children"], lambda node: node["text"].startswith("Bold italic"))
        runs = paragraph["content"]["blocknote"]
        styles = {}
        for run in runs:
            if run["type"] == "text":
                styles.setdefault(run["text"], run["styles"])
        self.assertEqual(styles["Bold"], {"bold": True})
        self.assertEqual(styles["italic"], {"italic": True})
        self.assertEqual(styles["under"], {"underline": True})
        self.assertEqual(styles["strike"], {"strike": True})
        self.assertEqual(styles["2"], {"superscript": True})  # x², then H₂O
        self.assertIn({"type": "text", "text": "2", "styles": {"subscript": True}}, runs)
        self.assertEqual(styles["red"], {"textColor": "#C00000"})
        self.assertEqual(styles["marked"], {"backgroundColor": "#FFFF00"})
        links = {run["content"][0]["text"]: run["href"] for run in runs if run["type"] == "link"}
        self.assertEqual(links["web"], "https://example.com/")
        markdown = export_document_markdown(self.db, tree["id"])
        self.assertIn("<u>under</u>", markdown)

        lists = [node for node in _walk(tree["children"]) if node["type"] == "list"]
        parent_item = next(node for node in lists if node["text"] == "Parent item")
        self.assertEqual([child["text"] for child in parent_item["children"]], ["Nested item"])
        self.assertTrue(parent_item["children"][0]["attrs"]["ordered"])
        done = next(node for node in lists if node["text"] == "Done task")
        self.assertIs(done["attrs"]["checked"], True)
        self.assertEqual(done["attrs"]["onenote_tags"], ["Important"])

    def test_assets_are_copied_once_by_hash_and_searchable(self) -> None:
        self.run_import()
        assets = self.settings.assets_path
        self.assertEqual((assets / PNG_NAME).read_bytes(), PNG)
        self.assertEqual((assets / PDF_NAME).read_bytes(), PDF)
        self.assertEqual((assets / SVG_NAME).read_bytes(), SVG)
        html_digest = HTML_NAME.split(".")[0]
        self.assertTrue((assets / f"{html_digest}.bin").is_file())  # active content as a download
        self.assertFalse((assets / HTML_NAME).exists())
        before = sorted(path.name for path in assets.iterdir())
        self.run_import()
        self.assertEqual(sorted(path.name for path in assets.iterdir()), before)

        tree = self.tree(P3)
        nodes = list(_walk(tree["children"]))
        files = [node for node in nodes if node["type"] == "file"]
        self.assertEqual(files[0]["attrs"]["url"], f"/media/{PDF_NAME}")
        self.assertEqual(files[0]["attrs"]["name"], "Contract.pdf")
        self.assertEqual(files[1]["attrs"]["url"], f"/media/{html_digest}.bin")
        self.assertNotIn("url", files[2]["attrs"])
        ink = next(node for node in nodes if node["type"] == "image")
        self.assertEqual(ink["attrs"]["url"], f"/media/{SVG_NAME}")
        self.assertEqual(ink["attrs"]["alt"], "handwritten words")
        math = next(node for node in nodes if node["type"] == "math")
        self.assertEqual(math["content"]["latex"], "a=b")
        code = next(node for node in nodes if node["type"] == "code")
        self.assertEqual(code["text"], "print('hi')\nprint('there')")
        self.assertTrue(any(node["type"] == "quote" for node in nodes))
        embed = next(node for node in nodes if "youtube" in node["text"])
        self.assertEqual(embed["content"]["blocknote"][0]["type"], "link")

        hits = search_blocks(self.db, "Quarterly revenue")
        self.assertEqual([hit["document_id"] for hit in hits], [self.doc_id(P1)])
        self.assertTrue(search_blocks(self.db, "handwritten"))
        with block_store.connection_scope(self.db) as connection:
            registered = {row["media_name"] for row in connection.execute("SELECT media_name FROM assets")}
        self.assertIn(PNG_NAME, registered)

    def test_onenote_links_become_document_links(self) -> None:
        result = self.run_import()
        links = result["links"]
        self.assertEqual(links["rewritten"], 2)
        self.assertEqual(links["block_links_as_page_links"], 1)
        self.assertEqual(len(links["unresolved"]), 1)
        unresolved = links["unresolved"][0]
        self.assertEqual(unresolved["page"], "Work › Projects › Alpha › Plan")
        self.assertIn(UNKNOWN, unresolved["target"])
        tree = self.tree(P1)
        paragraph = _find(tree["children"], lambda node: node["text"].startswith("Bold italic"))
        hrefs = {run["content"][0]["text"]: run["href"]
                 for run in paragraph["content"]["blocknote"] if run["type"] == "link"}
        self.assertEqual(hrefs["Standup"], f"[[{self.doc_id(P3)}|Standup notes]]")
        self.assertEqual(hrefs["child"], f"[[{self.doc_id(P2)}|Child]]")
        self.assertNotIn("elsewhere", hrefs)  # unresolved: kept as text
        self.assertIn("elsewhere", paragraph["text"])
        self.assertIn(UNKNOWN, paragraph["provenance"]["extra"]["unresolved_links"][0])
        backlinks = block_store.document_backlinks(self.db, self.doc_id(P3))
        self.assertTrue(backlinks)
        # The second run resolves everything in one pass: nothing changes.
        again = self.run_import()
        self.assertEqual(again["notebooks"][0]["pages_unchanged"], 4)
        self.assertEqual(again["links"]["rewritten"], 2)

    def test_links_resolve_to_pages_from_earlier_imports(self) -> None:
        first = copy.deepcopy(self.manifest)
        first["notebooks"][0]["children"] = first["notebooks"][0]["children"][1:]  # only Meetings
        self.run_import(first)
        result = self.run_import(self.manifest)
        self.assertEqual(result["links"]["rewritten"], 2)
        self.assertEqual(result["notebooks"][0]["pages_unchanged"], 1)

    def test_subpages_listed_on_their_parent(self) -> None:
        self.run_import()
        plan = self.tree(P1)
        heading = plan["children"][-1]
        self.assertEqual(heading["text"], "Subpages")
        [item] = heading["children"]
        self.assertEqual(item["content"]["blocknote"][0]["href"], f"[[{self.doc_id(P2)}|Child]]")
        child = self.tree(P2)
        self.assertEqual(child["provenance"]["extra"]["onenote"]["parent_page_id"], P1)
        self.assertEqual(child["provenance"]["extra"]["onenote"]["level"], 1)
        [grand] = child["children"][-1]["children"]
        self.assertEqual(grand["content"]["blocknote"][0]["href"], f"[[{self.doc_id(P2B)}|Grandchild]]")
        self.assertFalse(any(node["text"] == "Subpages" for node in self.tree(P2B)["children"]))

    def test_trashed_page_is_skipped(self) -> None:
        self.run_import()
        block_store.delete_document(self.db, self.doc_id(P3))
        result = self.run_import()["notebooks"][0]
        self.assertEqual(result["pages_skipped"], 1)
        self.assertEqual(result["pages_unchanged"], 2)
        self.assertEqual(result["pages_updated"], 1)  # Plan's link to it becomes text

    def test_unknown_manifest_version_is_refused(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["format_version"] = 2
        with self.assertRaisesRegex(onenote_import.OneNoteImportError, "format version"):
            self.run_import(manifest)


class ConverterTests(unittest.TestCase):
    def test_missing_binary_gives_a_clear_error(self) -> None:
        with patch.dict(os.environ, {"FORTRESS_ONENOTE_BIN": "/nonexistent/fortress-onenote"}):
            self.assertIsNone(onenote_import.converter_path())
            with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
                onenote_import.OneNoteImportError, "FORTRESS_ONENOTE_BIN"
            ):
                onenote_import.run_converter(Path(directory) / "in.zip", Path(directory) / "out")

    def test_failing_converter_reports_its_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-converter"
            script.write_text("#!/bin/sh\necho 'nothing convertible: zip holds only errors.txt' >&2\nexit 1\n")
            script.chmod(0o755)
            with patch.dict(os.environ, {"FORTRESS_ONENOTE_BIN": str(script)}), self.assertRaisesRegex(
                onenote_import.OneNoteImportError, "only errors.txt"
            ):
                onenote_import.run_converter(Path(directory) / "in.zip", Path(directory) / "out")


class ImportJobApiTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        from app import main

        self.jobs = onenote_import.ImportJobs()
        self.statuses: list[str] = []
        original_update = self.jobs._update

        def record(job_id, **changes):
            if "status" in changes and (not self.statuses or self.statuses[-1] != changes["status"]):
                self.statuses.append(changes["status"])
            original_update(job_id, **changes)

        self.jobs._update = record
        self.converted_inputs: list[Path] = []

        def fake_converter(input_path: Path, out_dir: Path, **_):
            self.converted_inputs.append(input_path)
            if input_path.name.startswith("broken"):
                raise onenote_import.OneNoteImportError("The OneNote export could not be converted: boom")
            write_manifest(out_dir, self.manifest)
            return {"pages": 4}

        for item in (
            patch.object(main, "settings", self.settings),
            patch.object(onenote_import, "jobs", self.jobs),
            patch.object(onenote_import, "run_converter", fake_converter),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_path_import_runs_through_all_states(self) -> None:
        source = self.root / "Work.zip"
        source.write_bytes(b"zip")
        response = self.client.post("/api/import/onenote", json={"path": str(source)})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.jobs.wait(30)
        job = self.client.get(f"/api/import/onenote/{body['job_id']}").json()
        self.assertEqual(job["status"], "done", job)
        self.assertIsNone(job["error"])
        self.assertEqual(self.statuses, ["converting", "importing", "linking", "done"])
        self.assertEqual(job["progress"]["pages_total"], 4)
        self.assertEqual(job["progress"]["pages_done"], 4)
        [notebook] = job["result"]["notebooks"]
        self.assertEqual(notebook["pages_created"], 4)
        self.assertIn("rewritten", job["result"]["links"])
        self.assertIsInstance(job["result"]["duration_ms"], int)
        self.assertEqual(self.converted_inputs, [source])
        stored = self.notes / ".fortress-imports" / f"{body['job_id']}.json"
        self.assertEqual(json.loads(stored.read_text())["status"], "done")
        self.assertTrue(source.exists())  # a local path is never deleted

        second = self.client.post("/api/import/onenote", json={"path": str(source)}).json()
        self.jobs.wait(30)
        listed = self.client.get("/api/import/onenote").json()
        self.assertEqual([job["job_id"] for job in listed], [second["job_id"], body["job_id"]])
        self.assertEqual(listed[0]["result"]["notebooks"][0]["pages_unchanged"], 4)

    def test_upload_import_and_failures(self) -> None:
        response = self.client.post(
            "/api/import/onenote", files={"file": ("My Notebook.zip", b"PK" + b"x" * (3 * 1024 * 1024), "application/zip")}
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.jobs.wait(30)
        job = self.client.get(f"/api/import/onenote/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["input_name"], "My Notebook.zip")
        self.assertEqual(self.converted_inputs[-1].name, "My Notebook.zip")
        self.assertFalse(self.converted_inputs[-1].exists())  # the upload is cleaned up

        bad_type = self.client.post("/api/import/onenote", files={"file": ("notes.txt", b"x", "text/plain")})
        self.assertEqual(bad_type.status_code, 415)
        no_folder = self.client.post(
            "/api/import/onenote", files={"file": ("a.zip", b"x", "application/zip")}, data={"folder_id": "nope"}
        )
        self.assertEqual(no_folder.status_code, 404)
        missing = self.client.post("/api/import/onenote", json={"path": str(self.root / "absent.zip")})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.client.get("/api/import/onenote/UNKNOWNJOB").status_code, 404)

        broken = self.root / "broken.zip"
        broken.write_bytes(b"x")
        failed = self.client.post("/api/import/onenote", json={"path": str(broken)}).json()
        self.jobs.wait(30)
        job = self.client.get(f"/api/import/onenote/{failed['job_id']}").json()
        self.assertEqual(job["status"], "failed")
        self.assertIn("boom", job["error"])
        self.assertIsNone(job["result"])

    def test_import_into_folder_from_json(self) -> None:
        folder = block_store.create_folder(self.db, "Inbox", None)["id"]
        source = self.root / "Work"
        source.mkdir()
        response = self.client.post("/api/import/onenote", json={"path": str(source), "folder_id": folder})
        self.assertEqual(response.status_code, 202)
        self.jobs.wait(30)
        job = self.client.get(f"/api/import/onenote/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "done", job)
        navigation = block_store.navigation(self.db)["items"]
        [inbox] = [item for item in navigation if item["kind"] == "folder"]
        self.assertEqual([child["name"] for child in inbox["children"]], ["Work"])


G = "{0A0B0C0D-1111-2222-3333-444455556666}"


def oid(n: int) -> str:
    """A manifest ExGUID (`{GUID}&N`, N hex)."""

    return f"{G}&{n:X}"


def _pages(manifest: dict) -> dict:
    found = {}

    def walk(entries):
        for entry in entries:
            if entry["kind"] == "section":
                for page in entry["pages"]:
                    found[page["id"]] = page
            else:
                walk(entry["children"])

    for notebook in manifest["notebooks"]:
        walk(notebook["children"])
    return found


def with_object_ids(manifest: dict) -> dict:
    """The fixture as a converter with object ids writes it: every top-level
    block and table-cell block gets object/element ids; the Plan page links
    to the Child page's paragraph by its element id."""

    manifest = copy.deepcopy(manifest)
    counter = iter(range(0x10, 0x1000, 2))

    def tag(blocks):
        for block in blocks:
            n = next(counter)
            block["object_id"], block["element_id"] = oid(n + 1), oid(n)
            for row in block.get("rows") or []:
                for cell in row["cells"]:
                    tag(cell["blocks"])

    pages = _pages(manifest)
    for guid in sorted(pages):
        for item in pages[guid]["items"]:
            tag(item["blocks"])
    child_body = pages[P2]["items"][0]["blocks"][0]
    for block in pages[P1]["items"][0]["blocks"]:
        for mark in block.get("marks") or []:
            if mark.get("type") == "link" and (mark.get("onenote") or {}).get("object_id"):
                mark["onenote"]["object_ref"] = child_body["element_id"]
    return manifest


class LivingDocumentTests(_Base):
    """Object ids, paragraph links, pages removed in OneNote, coverage, OCR."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest = with_object_ids(self.manifest)
        write_manifest(self.export, self.manifest)

    def block(self, guid: str, text: str) -> dict:
        found = _find(self.tree(guid)["children"], lambda node: node["text"] == text)
        assert found is not None, text
        return found

    def test_paragraph_edited_in_source_updates_the_same_block(self) -> None:
        self.run_import()
        before = self.block(P1, "Editable paragraph")
        self.assertEqual(before["provenance"]["external_id"], f"{P1}/obj:{oid(0x1B)}")
        self.assertEqual(before["provenance"]["extra"]["onenote"],
                         {"object_id": oid(0x1B), "element_id": oid(0x1A)})
        # Another note references the paragraph.
        other = block_store.create_block_document(self.db, "Elsewhere", "rough", [], f"See (( {before['id']} ))")

        def references() -> list:
            return [(link["source_document_id"], link["target_block_id"])
                    for link in block_store.document_backlinks(self.db, self.doc_id(P1))]

        self.assertEqual(references(), [(other["id"], before["id"])])

        changed = copy.deepcopy(self.manifest)
        _pages(changed)[P1]["items"][0]["blocks"][5]["text"] = "Edited in OneNote"
        result = self.run_import(changed)["notebooks"][0]
        self.assertEqual((result["pages_updated"], result["pages_unchanged"]), (1, 3))
        after = self.block(P1, "Edited in OneNote")
        self.assertEqual(after["id"], before["id"])  # same Fortress block, updated in place
        self.assertNotIn("Editable paragraph", [node["text"] for node in _walk(self.tree(P1)["children"])])
        self.assertEqual(references(), [(other["id"], before["id"])])

    def test_local_edit_wins_and_source_edit_becomes_a_conflict(self) -> None:
        self.run_import()
        tree = self.tree(P1)
        block = self.block(P1, "Editable paragraph")
        apply_transaction(self.db, tree["id"], [BlockOperation(
            operation="update", block_id=block["id"], text="Edited in Fortress",
            content={"markdown": "Edited in Fortress"})], tree["revision"])
        changed = copy.deepcopy(self.manifest)
        _pages(changed)[P1]["items"][0]["blocks"][5]["text"] = "Edited in OneNote"
        result = self.run_import(changed)["notebooks"][0]
        self.assertEqual(result["conflicts"], 1)
        kept = self.block(P1, "Edited in Fortress")
        self.assertEqual(kept["id"], block["id"])
        self.assertEqual(kept["provenance"]["conflict"]["source_markdown"], "Edited in OneNote")

    def test_notes_imported_with_hash_ids_migrate_in_place(self) -> None:
        self.run_import(build_manifest())  # an old, format-1 style manifest
        before = {node["text"]: node["id"] for node in _walk(self.tree(P1)["children"]) if node["text"]}
        columns_before = [node["id"] for node in self.tree(P1)["children"] if node["type"] == "columns"]
        result = self.run_import(self.manifest)["notebooks"][0]
        self.assertEqual(result["pages_created"], 0)
        tree = self.tree(P1)
        after = {node["text"]: node["id"] for node in _walk(tree["children"]) if node["text"]}
        self.assertEqual(after, before)
        self.assertEqual([node["id"] for node in tree["children"] if node["type"] == "columns"], columns_before)
        migrated = self.block(P1, "Editable paragraph")
        self.assertEqual(migrated["provenance"]["external_id"], f"{P1}/obj:{oid(0x1B)}")
        # Now keyed by object id: an edit in OneNote keeps the block.
        changed = copy.deepcopy(self.manifest)
        _pages(changed)[P1]["items"][0]["blocks"][5]["text"] = "Edited later"
        self.run_import(changed)
        self.assertEqual(self.block(P1, "Edited later")["id"], migrated["id"])

    def test_paragraph_link_becomes_a_block_reference(self) -> None:
        result = self.run_import()
        self.assertEqual(result["links"]["block_references"], 1)
        self.assertEqual(result["links"]["block_links_as_page_links"], 0)
        target = self.block(P2, "Child body")
        paragraph = _find(self.tree(P1)["children"], lambda node: node["text"].startswith("Bold italic"))
        hrefs = {run["content"][0]["text"]: run["href"]
                 for run in paragraph["content"]["blocknote"] if run["type"] == "link"}
        self.assertEqual(hrefs["child"], f"(( {target['id']} ))")
        with block_store.connection_scope(self.db) as connection:
            refs = connection.execute(
                "SELECT source_block_id FROM block_refs WHERE target_block_id = ?", (target["id"],)
            ).fetchall()
        self.assertEqual([row["source_block_id"] for row in refs], [paragraph["id"]])
        # Re-import: unchanged. Edit the target paragraph in OneNote: the reference
        # still points at the same (updated) block.
        self.assertEqual(self.run_import()["notebooks"][0]["pages_unchanged"], 4)
        changed = copy.deepcopy(self.manifest)
        _pages(changed)[P2]["items"][0]["blocks"][0]["text"] = "Child body, revised"
        self.run_import(changed)
        self.assertEqual(self.block(P2, "Child body, revised")["id"], target["id"])
        paragraph = _find(self.tree(P1)["children"], lambda node: node["text"].startswith("Bold italic"))
        self.assertIn(f"(( {target['id']} ))", json.dumps(paragraph["content"]))

    def test_paragraph_link_to_a_table_cell_points_at_the_table(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        pages = _pages(manifest)
        table = pages[P1]["items"][3]["blocks"][0]
        cell_block = table["rows"][1]["cells"][1]["blocks"][0]
        child = pages[P2]["items"][0]["blocks"][0]
        child["marks"] = [{"start": 0, "end": 5, "type": "link", "href": "onenote:#x",
                           "onenote": {"page_id": P1, "section_id": None, "object_id": G,
                                       "object_ref": cell_block["element_id"], "section_file": None,
                                       "page_title": "Plan"}}]
        self.run_import(manifest)
        table_block = _find(self.tree(P1)["children"], lambda node: node["type"] == "table")
        body = self.block(P2, "Child body")
        self.assertIn(f"(( {table_block['id']} ))", json.dumps(body["content"]))

    def test_pages_and_sections_removed_in_onenote_are_marked_not_deleted(self) -> None:
        self.run_import()
        ids = {guid: self.doc_id(guid) for guid in (P1, P2, P2B, P3)}
        changed = copy.deepcopy(self.manifest)
        notebook = changed["notebooks"][0]
        notebook["children"] = notebook["children"][:1]  # the Meetings section is gone
        alpha = notebook["children"][0]["children"][0]
        alpha["pages"] = [page for page in alpha["pages"] if page["id"] != P2B]  # and a page
        result = self.run_import(changed)["notebooks"][0]
        removed = {item["document_id"]: item for item in result["pages_removed_in_source"]}
        self.assertEqual(set(removed), {ids[P2B], ids[P3]})
        self.assertTrue(all(item["newly_removed"] for item in removed.values()))
        self.assertEqual(removed[ids[P3]]["source_path"], "Work › Meetings › Standup notes")
        self.assertEqual([item["name"] for item in result["sections_removed_in_source"]], ["Meetings"])
        self.assertEqual(result["sections_removed_in_source"][0]["path"], "Work › Meetings")
        for guid in (P2B, P3):
            provenance = self.tree(guid)["provenance"]  # still there
            self.assertTrue(provenance["source_removed"])
            self.assertTrue(provenance["source_removed_at"])
        self.assertNotIn("source_removed", self.tree(P1)["provenance"])
        # Still listed next time, with the first date.
        again = self.run_import(changed)["notebooks"][0]
        self.assertEqual({item["document_id"] for item in again["pages_removed_in_source"]}, {ids[P2B], ids[P3]})
        self.assertFalse(any(item["newly_removed"] for item in again["pages_removed_in_source"]))
        # The page comes back: the mark goes away.
        restored = self.run_import(self.manifest)["notebooks"][0]
        self.assertEqual(restored["pages_removed_in_source"], [])
        self.assertEqual(restored["sections_removed_in_source"], [])
        self.assertNotIn("source_removed", self.tree(P3)["provenance"])
        self.assertNotIn("source_removed_at", self.tree(P3)["provenance"])

    def test_unreadable_sections_and_other_notebooks_are_not_removed(self) -> None:
        self.run_import()
        changed = copy.deepcopy(self.manifest)
        notebook = changed["notebooks"][0]
        notebook["children"] = notebook["children"][:1]
        notebook["report"]["failed_sections"].append({
            "path": ["Work", "Meetings"], "file": "Meetings.one", "reason": "damaged", "hint": "", "error": "x"})
        result = self.run_import(changed)["notebooks"][0]
        self.assertEqual(result["pages_removed_in_source"], [])
        self.assertEqual(result["sections_removed_in_source"], [])
        other = copy.deepcopy(self.manifest)
        other["notebooks"][0].update(id="nb-2", file_identity="nb-2", name="Home", children=[])
        result = self.run_import(other)["notebooks"][0]
        self.assertEqual(result["pages_removed_in_source"], [])
        self.assertNotIn("source_removed", self.tree(P3)["provenance"])

    def test_text_coverage(self) -> None:
        result = self.run_import()
        self.assertEqual(result["notebooks"][0]["low_coverage_pages"], [])
        self.assertEqual(onenote_import.text_coverage("Hello, World", "**hello** [[x|world]]")[0], 1.0)
        self.assertAlmostEqual(onenote_import.text_coverage("ab cd", "ab")[0], 0.5)
        self.assertEqual(onenote_import.text_coverage("", "anything")[0], 1.0)

        original = onenote_import._convert_block

        def lossy(block, ctx, nested=False):
            if block.get("text") == "Editable paragraph":
                return None
            return original(block, ctx, nested)

        changed = copy.deepcopy(self.manifest)
        _pages(changed)[P1]["items"][0]["blocks"][5]["text"] = "Editable paragraph"
        with patch.object(onenote_import, "_convert_block", lossy):
            result = self.run_import(changed)
        [low] = result["notebooks"][0]["low_coverage_pages"]
        self.assertEqual(low["page"], "Work › Projects › Alpha › Plan")
        self.assertEqual(low["document_id"], self.doc_id(P1))
        self.assertLess(low["coverage"], 0.98)
        self.assertGreater(low["manifest_chars"], low["imported_chars"] - 1000)

    def test_images_without_onenote_text_are_queued_for_ocr(self) -> None:
        changed = copy.deepcopy(self.manifest)
        image = _pages(changed)[P1]["items"][2]["blocks"][0]
        image["alt_text"] = None
        queued: list[list[Path]] = []
        result = onenote_import.import_manifest(
            write_manifest(self.export, changed), db_path=self.db, notes_path=self.notes,
            assets_path=self.settings.assets_path, ocr=queued.append)
        self.assertEqual(result["notebooks"][0]["images_ocr_queued"], 1)
        self.assertEqual([[path.name for path in batch] for batch in queued], [[PNG_NAME]])
        # Once the OCR stored text, a re-import does not queue it again.
        block_store.store_asset_text(self.db, PNG_NAME.split(".")[0], "Text in image: revenue")
        self.assertTrue(search_blocks(self.db, "revenue"))
        queued.clear()
        onenote_import.import_manifest(self.export, db_path=self.db, notes_path=self.notes,
                                       assets_path=self.settings.assets_path, ocr=queued.append)
        self.assertEqual(queued, [])


class ImportJobLifecycleTests(_Base):
    """Job records on disk while running, restarts, cancelling."""

    def setUp(self) -> None:
        super().setUp()
        from app import main

        self.jobs = onenote_import.ImportJobs()
        self.jobs.ocr = None
        self.gate = threading.Event()
        self.converting = threading.Event()

        def fake_converter(input_path: Path, out_dir: Path, *, cancelled=None, **_):
            self.converting.set()
            while not self.gate.wait(0.02):
                if cancelled is not None and cancelled():
                    raise onenote_import.ImportCancelled()
            write_manifest(out_dir, self.manifest)
            return {"pages": 4}

        for item in (
            patch.object(main, "settings", self.settings),
            patch.object(onenote_import, "jobs", self.jobs),
            patch.object(onenote_import, "run_converter", fake_converter),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.gate.set)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.source = self.root / "Work.zip"
        self.source.write_bytes(b"zip")

    def stored(self, job_id: str) -> dict:
        return json.loads((self.notes / ".fortress-imports" / f"{job_id}.json").read_text())

    def test_record_is_saved_while_running_and_a_restart_reads_as_failed(self) -> None:
        job_id = self.client.post("/api/import/onenote", json={"path": str(self.source)}).json()["job_id"]
        self.assertTrue(self.converting.wait(10))
        self.assertEqual(self.stored(job_id)["status"], "converting")
        # A new process (restart) knows nothing of the running job.
        restarted = onenote_import.ImportJobs()
        with patch.object(onenote_import, "jobs", restarted):
            job = self.client.get(f"/api/import/onenote/{job_id}")
            self.assertEqual(job.status_code, 200)
            self.assertEqual(job.json()["status"], "failed")
            self.assertIn("interrupted by a restart", job.json()["error"])
            self.assertIn("already imported are kept", job.json()["error"])
            [listed] = self.client.get("/api/import/onenote").json()
            self.assertEqual(listed["status"], "failed")
            self.assertEqual(self.client.post(f"/api/import/onenote/{job_id}/cancel").status_code, 409)
        self.assertEqual(self.stored(job_id)["status"], "failed")
        self.gate.set()
        self.jobs.wait(30)

    def test_cancel_running_and_queued_jobs(self) -> None:
        first = self.client.post("/api/import/onenote", json={"path": str(self.source)}).json()["job_id"]
        second = self.client.post("/api/import/onenote", json={"path": str(self.source)}).json()["job_id"]
        self.assertTrue(self.converting.wait(10))
        queued = self.client.post(f"/api/import/onenote/{second}/cancel")
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.json()["status"], "cancelled")
        running = self.client.post(f"/api/import/onenote/{first}/cancel")
        self.assertEqual(running.status_code, 202)
        self.assertTrue(running.json()["cancel_requested"])
        self.jobs.wait(30)
        for job_id in (first, second):
            job = self.client.get(f"/api/import/onenote/{job_id}").json()
            self.assertEqual(job["status"], "cancelled", job)
            self.assertIn("kept", job["error"])
            self.assertEqual(self.stored(job_id)["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/import/onenote/{first}/cancel").status_code, 409)
        self.assertEqual(self.client.post("/api/import/onenote/NOSUCHJOB/cancel").status_code, 404)

    def test_cancel_between_pages_keeps_imported_pages(self) -> None:
        self.gate.set()
        calls = []
        original = onenote_import._Importer._import_page

        def import_page(importer, *args, **kwargs):
            calls.append(args[0]["id"])
            state = original(importer, *args, **kwargs)
            if len(calls) == 2:
                self.jobs.cancel(job_id, self.notes)
            return state

        with patch.object(onenote_import._Importer, "_import_page", import_page):
            job_id = self.client.post("/api/import/onenote", json={"path": str(self.source)}).json()["job_id"]
            self.jobs.wait(30)
        job = self.client.get(f"/api/import/onenote/{job_id}").json()
        self.assertEqual(job["status"], "cancelled", job)
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.doc_id(P1))  # the pages imported before cancelling stay
        # Importing again is idempotent and completes.
        again = self.client.post("/api/import/onenote", json={"path": str(self.source)}).json()["job_id"]
        self.jobs.wait(30)
        result = self.client.get(f"/api/import/onenote/{again}").json()["result"]["notebooks"][0]
        self.assertEqual(result["pages_created"], 2)
        self.assertEqual(result["pages_created"] + result["pages_updated"] + result["pages_unchanged"], 4)


SAMPLES = os.environ.get("FORTRESS_ONENOTE_SAMPLES", "")


@unittest.skipUnless(SAMPLES and onenote_import.converter_path(), "set FORTRESS_ONENOTE_SAMPLES and build the converter")
class OneNoteSamplesTests(_Base):
    """End to end on real OneNote files (third-party samples; never copied into the repo)."""

    def test_samples_import_end_to_end(self) -> None:
        out = self.root / "converted"
        summary = onenote_import.run_converter(Path(SAMPLES), out)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.export = out
        first = onenote_import.import_manifest(
            out, db_path=self.db, notes_path=self.notes, assets_path=self.settings.assets_path)
        notebooks = first["notebooks"]
        self.assertEqual(len(notebooks), len(manifest["notebooks"]))
        pages = sum(nb["pages_created"] + nb["pages_updated"] + nb["pages_unchanged"] for nb in notebooks)
        self.assertEqual(pages, summary["pages"])
        self.assertEqual(sum(nb["pages_skipped"] for nb in notebooks), 0)
        for nb, source in zip(notebooks, manifest["notebooks"]):
            counts = source["report"]["counts"]
            self.assertEqual(nb["tables"], counts["tables"], nb["name"])
            self.assertEqual(nb["images"], counts["images"], nb["name"])
            self.assertEqual(nb["files"], counts["attachments"], nb["name"])
            self.assertEqual(nb["ink"], counts["ink"], nb["name"])
            self.assertEqual(len(nb["failed_sections"]), counts["sections_failed"], nb["name"])
        self.assertEqual(sum(len(nb["failed_sections"]) for nb in notebooks), summary["sections_failed"])
        # Some text from the samples is searchable.
        texts = [block["text"] for item in manifest["notebooks"] for block in _manifest_blocks(item)
                 if block.get("type") == "paragraph" and len(str(block.get("text") or "").split()) >= 3]
        self.assertTrue(texts)
        word = max(texts[0].split(), key=len)
        self.assertTrue(search_blocks(self.db, word), word)

        second = onenote_import.import_manifest(
            out, db_path=self.db, notes_path=self.notes, assets_path=self.settings.assets_path)
        for nb in second["notebooks"]:
            self.assertEqual((nb["pages_created"], nb["pages_updated"]), (0, 0), nb["name"])
        self.assertEqual(sum(nb["pages_unchanged"] for nb in second["notebooks"]), summary["pages"])
        # Nothing lost silently.
        self.assertEqual([page for nb in notebooks for page in nb["low_coverage_pages"]], [])

        # Simulate an edit in OneNote: the paragraph keeps its object id, so the
        # same Fortress block is updated.
        edited = next((
            (page, block) for page in _pages(manifest).values() for item in page["items"]
            for block in item["blocks"]
            if block.get("type") == "paragraph" and block.get("object_id") and block.get("text")
        ), None)
        self.assertIsNotNone(edited, "the converter found no object ids")
        page, block = edited
        with block_store.connection_scope(self.db) as connection:
            [before] = [row["id"] for row in connection.execute(
                "SELECT blocks.id, blocks.provenance_json FROM blocks JOIN documents ON documents.id = blocks.document_id"
                " WHERE documents.source_external_id = ?", (page["id"],))
                if block["object_id"] in (row["provenance_json"] or "")]
        block["text"] += " (edited)"
        block.pop("marks", None)
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        third = onenote_import.import_manifest(
            out, db_path=self.db, notes_path=self.notes, assets_path=self.settings.assets_path)
        self.assertEqual(sum(nb["pages_updated"] for nb in third["notebooks"]), 1)
        with block_store.connection_scope(self.db) as connection:
            row = connection.execute("SELECT text FROM blocks WHERE id = ?", (before,)).fetchone()
        self.assertTrue(row["text"].endswith("(edited)"))


def _manifest_blocks(notebook):
    def entries(items):
        for entry in items:
            if entry["kind"] == "section":
                for page in entry["pages"]:
                    for item in page["items"]:
                        yield from item["blocks"]
            else:
                yield from entries(entry["children"])

    yield from entries(notebook["children"])


class CompareFindingsTests(_Base):
    """Regressions found by tools/onenote-compare (rendered OneNote vs imported note)."""

    def import_blocks(self, blocks: list[dict], guid: str = P3) -> dict:
        page = _pages(self.manifest)[guid]
        page["items"] = [_outline("i0", blocks)]
        page["layout"] = [{"kind": "item", "item": "i0"}]
        self.run_import(self.manifest)
        return self.tree(guid)

    def test_markdown_export_numbers_ordered_list_items(self) -> None:
        info = {"ordered": True, "style": "decimal", "marker": "%.", "restart": None}
        tree = self.import_blocks([
            {"type": "list_item", "indent": 0, "text": f"Step {n}", "list": info,
             "object_id": f"{{{n}AAAAAAA-0000-0000-0000-000000000000}}&1"}
            for n in range(1, 4)
        ])
        markdown = export_document_markdown(self.db, tree["id"])
        # Every OneNote block carries a provenance comment, which ends a Markdown list:
        # without real numbers each item would read "1." in any Markdown viewer.
        self.assertIn("1. Step 1", markdown)
        self.assertIn("2. Step 2", markdown)
        self.assertIn("3. Step 3", markdown)

    def test_markdown_export_keeps_nesting_and_never_makes_code_blocks(self) -> None:
        from markdown_it import MarkdownIt

        info = {"ordered": True, "style": "decimal", "marker": "%.", "restart": None}
        tree = self.import_blocks([
            {"type": "paragraph", "indent": 0, "text": "Intro"},
            {"type": "paragraph", "indent": 1, "text": "Indented once"},
            {"type": "paragraph", "indent": 2, "text": "Indented twice"},
            {"type": "list_item", "indent": 0, "text": "Outer", "list": info},
            {"type": "list_item", "indent": 1, "text": "Inner", "list": info},
            {"type": "list_item", "indent": 2, "text": "Innermost", "list": info},
        ])
        html = MarkdownIt("commonmark", {"html": True}).render(export_document_markdown(self.db, tree["id"]))
        # Indented OneNote paragraphs used to be written 4+ spaces deep: a code block
        # showing the provenance comments.
        self.assertNotIn("<pre>", html)
        self.assertNotIn("&lt;!-- fortress:block", html)
        self.assertIn("<p>Indented twice</p>", html)
        # Children of "1. " items need 3 spaces, or they fall out of the item.
        self.assertRegex(html, r"(?s)<ol>\s*<li>.*Outer.*<ol>\s*<li>.*Inner.*<ol>\s*<li>.*Innermost")

    def test_list_numbering_style_and_start_are_kept(self) -> None:
        tree = self.import_blocks([
            {"type": "list_item", "indent": 0, "text": "Roman",
             "list": {"ordered": True, "style": "lower_roman", "marker": "%.", "restart": 3}},
        ])
        attrs = tree["children"][0]["attrs"]
        self.assertEqual((attrs.get("list_style"), attrs.get("list_start")), ("lower_roman", 3))

    def test_hostile_strings_from_the_converter_are_sanitised(self) -> None:
        bad = ["javascript:alert(1)", "java\nscript:alert(1)", " JAVASCRIPT:alert(1)", "data:text/html,x",
               "file:///etc/passwd", "\\\\server\\share\\x.doc", "ms-word:ofe|u|https://x/y.docx",
               "vbscript:x", "https://example.com/" + "a" * 6000]
        text = " ".join(f"L{i}" for i in range(len(bad))) + " ok"
        marks = []
        for i, href in enumerate(bad):
            start = text.index(f"L{i}")
            marks.append({"start": start, "end": start + len(f"L{i}"), "type": "link", "href": href})
        start = text.index("ok")
        marks.append({"start": start, "end": start + 2, "type": "link", "href": "https://example.com/ok"})
        page = _pages(self.manifest)[P3]
        page["title"] = "Bell\x07 title\x1b[31m"
        tree = self.import_blocks([
            {"type": "paragraph", "indent": 0, "text": text, "marks": marks},
            {"type": "paragraph", "indent": 0, "text": "nul\x00 and esc\x1b here", "marks": [
                {"start": 0, "end": 3, "type": "bold"}]},
            {"type": "image", "indent": 0, "asset": PNG_NAME, "data_status": "available", "alt_text": "a\x0bb",
             "hyperlink": "javascript:alert(1)", "width": None, "height": None, "is_printout_page": False,
             "printout_page_number": None, "is_background": False, "original_name": "x\x01.png", "original_asset": None},
            {"type": "embed", "indent": 0, "url": "file:///C:/secret.mp4", "thumbnail_asset": None},
        ])
        links, first = tree["children"][0], tree["children"][0]["content"]["blocknote"]
        hrefs = [run["href"] for run in first if run["type"] == "link"]
        self.assertEqual(hrefs, ["https://example.com/ok"])
        self.assertEqual(links["text"], text)  # the words stay
        self.assertEqual(len(links["provenance"]["extra"]["dropped_links"]), len(bad))
        self.assertEqual(tree["children"][1]["text"], "nul and esc here")
        image = tree["children"][2]
        self.assertNotIn("link", image["attrs"])
        self.assertEqual(image["attrs"]["alt"], "ab")
        self.assertEqual(image["attrs"]["name"], "x.png")
        self.assertEqual(tree["children"][3]["text"], "file:///C:/secret.mp4")
        self.assertEqual(tree["title"], "Bell title[31m")
        markdown = export_document_markdown(self.db, tree["id"])
        self.assertNotRegex(markdown, r"[\x00-\x08\x0b-\x1f]")
        visible = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
        self.assertNotIn("](javascript", visible.lower())
        self.assertNotIn("](file:", visible.lower())
        self.assertNotIn("](data:", visible.lower())

    def test_odd_tag_labels(self) -> None:
        def tag(label, shape):
            return {"label": label, "shape": shape, "checkable": False, "completed": False, "completed_at": None,
                    "highlight_color": None, "text_color": None}

        tree = self.import_blocks([{
            "type": "paragraph", "indent": 0, "text": "Tagged",
            "tags": [tag("   ", "yellow_star"), tag("Question ", "question_mark"), tag("", "no_icon"),
                     tag("x" * 600, "flag")],
        }])
        self.assertEqual(tree["children"][0]["attrs"]["onenote_tags"], ["Yellow star", "Question", "x" * 100])

    def test_monospace_runs_become_inline_code(self) -> None:
        tree = self.import_blocks([{
            "type": "paragraph", "indent": 0, "text": "run ls -la now",
            "marks": [{"start": 4, "end": 10, "type": "font", "family": "Consolas"}],
        }])
        runs = tree["children"][0]["content"]["blocknote"]
        self.assertIn({"type": "text", "text": "ls -la", "styles": {"code": True}}, runs)

    def test_inline_equations_keep_their_latex(self) -> None:
        text = "Pythagoras: 𝑎2+𝑏2=𝑐2 holds"
        start = text.index("𝑎")
        end = start + len("𝑎2+𝑏2=𝑐2")
        tree = self.import_blocks([
            {"type": "paragraph", "indent": 0, "text": text,
             "marks": [{"start": start, "end": end, "type": "math", "latex": "\\(a^{2}+b^{2}=c^{2}\\)"}]},
            # An equation alone in a list item (converter >= 0.2.1 keeps it a list item).
            {"type": "list_item", "indent": 0, "text": "∫1𝑥𝑑𝑥",
             "list": {"ordered": False, "style": "bullet", "marker": "•", "restart": None},
             "marks": [{"start": 0, "end": 6, "type": "math", "latex": "\\[\\int \\frac{1}{x}dx\\]"}]},
        ])
        paragraph, item = tree["children"]
        self.assertEqual(paragraph["content"]["blocknote"], [
            {"type": "text", "text": "Pythagoras: ", "styles": {}},
            {"type": "text", "text": "$a^{2}+b^{2}=c^{2}$", "styles": {"code": True}},
            {"type": "text", "text": " holds", "styles": {}},
        ])
        self.assertEqual(item["type"], "list")
        self.assertEqual(item["text"], "$\\int \\frac{1}{x}dx$")
        report = self.run_import(self.manifest)
        self.assertEqual([page for nb in report["notebooks"] for page in nb["low_coverage_pages"]], [])

    def test_alignment_is_kept(self) -> None:
        cell = {"background": None, "blocks": [{"type": "paragraph", "indent": 0, "text": "Total", "align": "right"}]}
        tree = self.import_blocks([
            {"type": "paragraph", "indent": 0, "text": "Centred title", "align": "center"},
            {"type": "table", "indent": 0, "row_count": 1, "col_count": 1, "column_widths": [3.0],
             "borders_visible": True, "rows": [{"cells": [cell]}]},
        ])
        paragraph, table = tree["children"]
        self.assertEqual(paragraph["attrs"].get("textAlignment"), "center")
        cell_props = table["content"]["blocknote"]["rows"][0]["cells"][0]["props"]
        self.assertEqual(cell_props.get("textAlignment"), "right")

    def test_video_embed_keeps_its_poster_image(self) -> None:
        tree = self.import_blocks([{
            "type": "embed", "indent": 0, "url": "https://www.youtube.com/watch?v=abc",
            "thumbnail_asset": PNG_NAME, "alt_text": "Talent Connect",
        }])
        [image] = tree["children"]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["attrs"]["link"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(image["attrs"]["caption"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(image["attrs"]["alt"], "Talent Connect")
        self.assertTrue(image["attrs"]["url"].startswith("/media/"))


if __name__ == "__main__":
    unittest.main()
