"""Fortress side: import a converted export with the real importer, then turn each
imported document into a ``Model`` (from the block tree) and two HTML renderings:

* ``tree_html``: the block tree drawn directly (what the app shows: columns as
  flex rows, tables with cell colours, check boxes, images, files, math);
* ``markdown_html``: the note's Markdown export rendered with markdown-it
  (HTML enabled) plus a stylesheet that turns ``<!-- fortress:columns -->``
  markers into flex columns.

Runs inside the backend's environment (``cd backend && uv run``): it imports
``app.*``.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model import Model, Token, norm_color, norm_text, sha256_file, tokens_from_pieces, words


# --- import ---------------------------------------------------------------------------------


class FortressNotes:
    """A fresh, throwaway NOTES_DIR holding one import."""

    def __init__(self, root: Path):
        from app import documents, images  # noqa: PLC0415 (backend env only)

        self.notes = root / "notes"
        self.notes.mkdir(parents=True, exist_ok=True)
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
        self.settings.assets_path.mkdir(exist_ok=True)
        self._stack = ExitStack()
        for module in (documents, images):
            self._stack.enter_context(patch.object(module, "get_settings", lambda: self.settings))
        self.db = self.settings.block_db_path

    def close(self) -> None:
        self._stack.close()

    def import_manifest(self, manifest_dir: Path) -> dict:
        from app import onenote_import  # noqa: PLC0415

        return onenote_import.import_manifest(
            manifest_dir, db_path=self.db, notes_path=self.notes, assets_path=self.settings.assets_path,
            ocr=lambda paths: None,  # no OCR / AI calls
        )

    def documents_by_page(self) -> dict[str, str]:
        from app import block_store  # noqa: PLC0415

        with block_store.connection_scope(self.db) as connection:
            rows = connection.execute(
                "SELECT id, source_external_id FROM documents"
                " WHERE source_kind = 'onenote_import' AND deleted_at IS NULL"
            ).fetchall()
        return {str(row["source_external_id"]).upper(): row["id"] for row in rows if row["source_external_id"]}

    def tree(self, document_id: str) -> dict:
        from app import block_store  # noqa: PLC0415

        return block_store.document_tree(self.db, document_id)

    def markdown(self, document_id: str) -> str:
        from app.portability import export_document_markdown  # noqa: PLC0415

        return export_document_markdown(self.db, document_id)

    def media_path(self, url: str | None) -> Path | None:
        if not url or not url.startswith("/media/"):
            return None
        name = url[len("/media/"):]
        if "/" in name or name.startswith("."):
            return None
        return self.settings.assets_path / name


# --- tree -> model ----------------------------------------------------------------------------

_GENERATED_SUFFIXES = ("/subpages", "/ink_text")
# Placeholders the importer writes for content whose data is not in the export.
_GENERATED_LABELS = ("Image missing from the OneNote export", "Empty image (no data) in the OneNote export",
                     "Image OneNote marked as damaged")


def _external_id(block: dict) -> str:
    return str(((block.get("provenance") or {}).get("external_id")) or "")


def _runs(block: dict) -> list[dict]:
    content = block.get("content") or {}
    runs = content.get("blocknote")
    if isinstance(runs, list):
        return runs
    text = block.get("text") or ""
    return [{"type": "text", "text": text, "styles": {}}] if text else []


def _run_marks(styles: dict) -> frozenset[str]:
    marks = set()
    for name, mark in (("bold", "bold"), ("italic", "italic"), ("underline", "underline"), ("strike", "strike"),
                       ("superscript", "superscript"), ("subscript", "subscript"), ("code", "code")):
        if styles.get(name):
            marks.add(mark)
    color = norm_color(styles.get("textColor"))
    if color:
        marks.add(f"color:{color}")
    background = norm_color(styles.get("backgroundColor"))
    if background:
        marks.add(f"highlight:{background}")
    return frozenset(marks)


TEXT_KINDS = {"paragraph", "heading", "list", "quote", "code", "bulletListItem", "numberedListItem", "checkListItem"}


def _is_equation(run: dict) -> bool:
    text = run.get("text", "")
    return bool((run.get("styles") or {}).get("code")) and len(text) > 2 and text.startswith("$") and text.endswith("$")


class _TreeWalker:
    def __init__(self, notes: FortressNotes, model: Model):
        self.notes, self.model = notes, model
        self.readable: list[str] = []
        self.table_depth = 0
        self.container = -1

    def runs_text(self, runs: list[dict], record: bool = True) -> str:
        """Tokenise a block's runs together (a word may span runs); returns its plain text."""

        pieces: list[tuple[str, frozenset[str]]] = []
        for run in runs or []:
            if run.get("type") == "link":
                inner = run.get("content") or []
                text = "".join(r.get("text", "") for r in inner)
                if record:
                    self.model.links.append((norm_text(text), str(run.get("href") or "")))
                pieces += [(r.get("text", ""), _run_marks(r.get("styles") or {})) for r in inner]
                continue
            text = run.get("text", "")
            if text.startswith(_GENERATED_LABELS):
                self.model.notes.append(f"generated label skipped: {text[:60]}")
                continue
            styles = run.get("styles") or {}
            if styles.get("code") and len(text) > 2 and text.startswith("$") and text.endswith("$"):
                self.model.math.append(text.strip("$"))  # inline equation (LaTeX)
                pieces.append((" ", frozenset()))
                continue
            pieces.append((text, _run_marks(styles)))
        self.model.tokens.extend(tokens_from_pieces(pieces, self.container))
        text = "".join(t for t, _ in pieces)
        self.readable.append(text)
        return norm_text(text)

    def add_words(self, text: str, marks: frozenset[str]) -> None:
        for word in words(text):
            self.model.tokens.append(Token(word, marks, self.container))

    def blocks(self, blocks: list[dict], list_depth: int = 0) -> None:
        for block in blocks or []:
            if _external_id(block).endswith(_GENERATED_SUFFIXES):
                self.model.notes.append(f"generated block skipped: {_external_id(block).rsplit('/', 1)[-1]}")
                continue
            self.block(block, list_depth)

    def block(self, block: dict, list_depth: int) -> None:
        kind = block.get("type")
        attrs = block.get("attrs") or {}
        if attrs.get("onenote_tags"):
            # One entry per tagged block (whatever the number of labels), as on the reference side.
            label = str(attrs["onenote_tags"][0])
            text = "" if kind not in TEXT_KINDS else norm_text("".join(
                r.get("text", "") if r.get("type") != "link" else "".join(x.get("text", "") for x in r.get("content") or [])
                for r in _runs(block) if not _is_equation(r)))
            self.model.tags.append((label, text))
        content = block.get("content") or {}
        child_depth = list_depth
        if kind in {"paragraph", "heading", "list", "quote", "code", "bulletListItem", "numberedListItem",
                    "checkListItem"}:
            if kind == "code":
                text = block.get("text") or ""
                self.add_words(text, frozenset({"code"}))
                self.readable.append(text)
                text = norm_text(text)
            else:
                text = self.runs_text(_runs(block))
            if kind == "heading":
                self.model.headings.append((int(attrs.get("level") or 1), text))
            if kind == "list":
                if "checked" in attrs:
                    self.model.checks.append((text, bool(attrs.get("checked"))))
                if "checked" not in attrs or attrs.get("ordered") or attrs.get("list_format"):
                    # (a to-do that is also in a numbered / bulleted list counts as a list item too)
                    fmt = str(attrs.get("list_style") or "decimal") if attrs.get("ordered") else "bullet"
                    self.model.list_items.append((text, bool(attrs.get("ordered")), list_depth, fmt))
                child_depth = list_depth + 1
            align = attrs.get("align") or attrs.get("textAlignment")
            if align in {"center", "right"}:
                self.model.aligned.append((align, text))
        elif kind == "table":
            self.table(block)
        elif kind == "image":
            path = self.notes.media_path(attrs.get("url"))
            entry = {"sha": sha256_file(path), "alt": norm_text(str(attrs.get("alt") or "")),
                     "name": str(attrs.get("name") or ""), "size": path.stat().st_size if path and path.is_file() else None,
                     "original_sha": sha256_file(self.notes.media_path(attrs.get("original_url")))}
            if attrs.get("ink"):
                self.model.ink.append({"text": entry["alt"]})
            else:
                self.model.images.append(entry)
        elif kind == "file":
            path = self.notes.media_path(attrs.get("url"))
            self.model.files.append({"sha": sha256_file(path), "name": str(attrs.get("name") or ""),
                                     "size": attrs.get("size")})
        elif kind == "math":
            self.model.math.append(norm_text(str(content.get("latex") or block.get("text") or "")))
        elif kind in {"columns", "column"}:
            pass
        else:
            text = block.get("text") or ""
            self.add_words(text, frozenset())
            self.model.notes.append(f"unknown block type {kind}")
        self.blocks(block.get("children") or [], child_depth)

    def table(self, block: dict) -> None:
        table = (block.get("content") or {}).get("blocknote") or {}
        rows = table.get("rows") or []
        entry = {"rows": len(rows), "cols": max((len(r.get("cells") or []) for r in rows), default=0),
                 "cells": [], "bg": [], "depth": self.table_depth}
        self.model.tables.append(entry)
        self.table_depth += 1
        for row in rows:
            texts, colors = [], []
            for cell in row.get("cells") or []:
                if isinstance(cell, str):
                    cell = {"content": [{"type": "text", "text": cell, "styles": {}}]}
                start = len(self.model.tokens)
                self.runs_text(cell.get("content") or [])
                self.nested(cell.get("blocks") or [])
                texts.append(" ".join(t.word for t in self.model.tokens[start:]))
                align = (cell.get("props") or {}).get("textAlignment")
                if align in {"center", "right"}:
                    first = " ".join(words("".join(r.get("text", "") if r.get("type") != "link" else
                                                   "".join(x.get("text", "") for x in r.get("content") or [])
                                                   for r in cell.get("content") or [])))
                    self.model.aligned.append((align, first))
                colors.append(norm_color((cell.get("props") or {}).get("backgroundColor")))
            entry["cells"].append(texts)
            entry["bg"].append(colors)
        self.table_depth -= 1

    def nested(self, blocks: list[dict], list_depth: int = 0) -> None:
        """In-cell blocks (``{"type", "attrs", "content", "children"}``)."""

        for block in blocks or []:
            self.block(block, list_depth)


def tree_model(notes: FortressNotes, tree: dict) -> Model:
    model = Model(title=norm_text(tree.get("title") or ""))
    walker = _TreeWalker(notes, model)
    for index, block in enumerate(tree.get("children") or []):
        _containers(block, model, walker, [index])
    model.text = norm_text(" ".join(walker.readable))
    return model


def _containers(block: dict, model: Model, walker: _TreeWalker, path: list[int]) -> None:
    """Top-level blocks; a columns block contributes one container per column."""

    if block.get("type") == "columns":
        for c_index, column in enumerate(block.get("children") or []):
            for b_index, child in enumerate(column.get("children") or []):
                _containers(child, model, walker, [*path, c_index, b_index])
        return
    model.containers.append({"index": len(model.containers), "path": path, "type": block.get("type")})
    walker.container = len(model.containers) - 1
    walker.blocks([block])


def columns_of(tree: dict) -> list[dict]:
    """Every columns block: its path and the first words of each column."""

    found = []

    def visit(blocks, trail):
        for index, block in enumerate(blocks or []):
            if block.get("type") == "columns":
                cols = []
                for column in block.get("children") or []:
                    sample = []
                    _collect_words(column.get("children") or [], sample, 60)
                    cols.append({"width": (column.get("attrs") or {}).get("width"), "words": sample})
                found.append({"path": [*trail, index], "columns": cols})
                for column in block.get("children") or []:
                    visit(column.get("children") or [], [*trail, index])
            else:
                visit(block.get("children") or [], [*trail, index])

    visit(tree.get("children") or [], [])
    return found


def _collect_words(blocks: list[dict], out: list[str], limit: int) -> None:
    for block in blocks:
        if len(out) >= limit:
            return
        out.extend(words(block.get("text") or ""))
        _collect_words(block.get("children") or [], out, limit)


# --- tree -> HTML ------------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light; }
body { font: 11pt/1.45 Calibri, Carlito, "Segoe UI", Arial, sans-serif; margin: 24px 32px; color: #111; background: #fff; max-width: 1100px; }
h1.doc-title { font-size: 20pt; font-weight: 400; border-bottom: 1px solid #ddd; margin: 0 0 16px; padding-bottom: 4px; }
.blk { margin: 2px 0; }
.children { margin-left: 24px; }
ul, ol { margin: 2px 0; padding-left: 24px; }
li.check { list-style: none; margin-left: -20px; }
li.check input { margin-right: 6px; }
table.f-table { border-collapse: collapse; margin: 6px 0; }
table.f-table td { border: 1px solid #a3a3a3; padding: 2px 6px; vertical-align: top; min-width: 24px; }
table.f-table.noborders td { border: 1px dashed #e4e4e4; }
.f-columns { display: flex; gap: 16px; align-items: flex-start; margin: 6px 0; }
.f-column { min-width: 0; border-left: 2px solid #e8eef8; padding-left: 8px; }
img { max-width: 100%; height: auto; }
.f-image { margin: 4px 0; }
.f-image .cap { font-size: 8pt; color: #777; }
.f-file { display: inline-block; border: 1px solid #ccd; border-radius: 6px; padding: 3px 8px; background: #f6f7fb; font-size: 10pt; margin: 3px 0; }
.f-math { margin: 6px 0; font-size: 13pt; }
.f-math code { font-size: 9pt; color: #666; }
pre { background: #f5f5f5; padding: 6px; white-space: pre-wrap; }
blockquote { border-left: 3px solid #ccc; margin: 4px 0; padding-left: 10px; color: #444; }
.f-tag { display: inline-block; font-size: 8pt; border-radius: 4px; padding: 0 4px; margin-right: 4px; background: #fff2b3; color: #6a5300; }
a.wiki { color: #6a1b9a; }
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def runs_html(runs: list[dict]) -> str:
    out = []
    for run in runs or []:
        if run.get("type") == "link":
            href = str(run.get("href") or "")
            inner = runs_html(run.get("content") or [])
            if href.startswith("[[") or href.startswith("(("):
                out.append(f'<a class="wiki" title="{_esc(href)}">{inner}</a>')
            else:
                out.append(f'<a href="{_esc(href)}">{inner}</a>')
            continue
        text = _esc(run.get("text", "")).replace("\n", "<br>")
        styles = run.get("styles") or {}
        css = []
        if styles.get("textColor"):
            css.append(f"color:{styles['textColor']}")
        if styles.get("backgroundColor"):
            css.append(f"background-color:{styles['backgroundColor']}")
        if css:
            text = f'<span style="{";".join(css)}">{text}</span>'
        for name, tag in (("code", "code"), ("bold", "strong"), ("italic", "em"), ("underline", "u"),
                          ("strike", "s"), ("superscript", "sup"), ("subscript", "sub")):
            if styles.get(name):
                text = f"<{tag}>{text}</{tag}>"
        out.append(text)
    return "".join(out)


class HtmlRenderer:
    def __init__(self, notes: FortressNotes, media_prefix: str):
        self.notes = notes
        self.media_prefix = media_prefix  # file:// URL of the assets folder

    def media(self, url: str | None) -> str:
        if url and url.startswith("/media/"):
            return self.media_prefix + url[len("/media/"):]
        return url or ""

    def document(self, tree: dict) -> str:
        body = self.blocks(tree.get("children") or [])
        return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(tree.get('title') or '')}</title>"
                f"<style>{STYLE}</style></head><body><h1 class='doc-title'>{_esc(tree.get('title') or '')}</h1>"
                f"{body}</body></html>")

    def blocks(self, blocks: list[dict]) -> str:
        out: list[str] = []
        index = 0
        blocks = blocks or []
        while index < len(blocks):
            block = blocks[index]
            if block.get("type") == "list":
                group = []
                ordered = bool((block.get("attrs") or {}).get("ordered"))
                while index < len(blocks) and blocks[index].get("type") == "list" and \
                        bool((blocks[index].get("attrs") or {}).get("ordered")) == ordered:
                    group.append(blocks[index])
                    index += 1
                tag = "ol" if ordered else "ul"
                out.append(f"<{tag}>" + "".join(self.list_item(item) for item in group) + f"</{tag}>")
                continue
            out.append(self.block(block))
            index += 1
        return "".join(out)

    def tags(self, attrs: dict) -> str:
        return "".join(f'<span class="f-tag">{_esc(str(tag))}</span>' for tag in attrs.get("onenote_tags") or [])

    def list_item(self, block: dict) -> str:
        attrs = block.get("attrs") or {}
        inner = self.tags(attrs) + runs_html(_runs(block))
        children = self.blocks(block.get("children") or [])
        if "checked" in attrs:
            box = f'<input type="checkbox" disabled {"checked" if attrs.get("checked") else ""}>'
            return f'<li class="check">{box}{inner}{children}</li>'
        return f"<li>{inner}{children}</li>"

    def block(self, block: dict) -> str:
        kind = block.get("type")
        attrs = block.get("attrs") or {}
        content = block.get("content") or {}
        children = self.blocks(block.get("children") or [])
        children_html = f'<div class="children">{children}</div>' if children else ""
        align = attrs.get("textAlignment") or attrs.get("align")
        style = f' style="text-align:{align}"' if align in {"center", "right"} else ""
        if kind == "heading":
            level = min(max(int(attrs.get("level") or 1), 1), 6)
            return f"<h{level}{style}>{self.tags(attrs)}{runs_html(_runs(block))}</h{level}>{children_html}"
        if kind in {"paragraph", "bulletListItem", "numberedListItem", "checkListItem"}:
            return f'<p class="blk"{style}>{self.tags(attrs)}{runs_html(_runs(block))}</p>{children_html}'
        if kind == "quote":
            return f"<blockquote>{runs_html(_runs(block))}{children}</blockquote>"
        if kind == "code":
            return f"<pre><code>{_esc(block.get('text') or '')}</code></pre>{children_html}"
        if kind == "table":
            return self.table(attrs, content.get("blocknote") or {}) + children_html
        if kind == "image":
            alt = attrs.get("alt") or ""
            width = f' width="{attrs["width"]}"' if attrs.get("width") else ""
            cap = f'<div class="cap">{_esc(alt[:200])}</div>' if alt else ""
            return (f'<div class="f-image"><img src="{_esc(self.media(attrs.get("url")))}" alt="{_esc(alt)}"{width}>'
                    f"{cap}</div>{children_html}")
        if kind == "file":
            size = f" ({attrs['size']} bytes)" if attrs.get("size") else ""
            return (f'<div><a class="f-file" href="{_esc(self.media(attrs.get("url")))}">&#128206; '
                    f'{_esc(str(attrs.get("name") or "file"))}{size}</a></div>{children_html}')
        if kind == "math":
            mathml = content.get("mathml")
            latex = content.get("latex") or ""
            if mathml:
                return f'<div class="f-math">{mathml}</div>{children_html}'
            return f'<div class="f-math"><code>{_esc(latex)}</code></div>{children_html}'
        if kind == "columns":
            cols = []
            for column in block.get("children") or []:
                width = (column.get("attrs") or {}).get("width") or 1
                cols.append(f'<div class="f-column" style="flex:{width} 1 0">{self.blocks(column.get("children") or [])}</div>')
            return f'<div class="f-columns">{"".join(cols)}</div>'
        return f'<p class="blk">{_esc(block.get("text") or "")}</p>{children_html}'

    def table(self, attrs: dict, table: dict) -> str:
        widths = table.get("columnWidths") or []
        cols = "".join(f'<col style="width:{w}px">' if w else "<col>" for w in widths)
        rows = []
        for row in table.get("rows") or []:
            cells = []
            for cell in row.get("cells") or []:
                if isinstance(cell, str):
                    cell = {"content": [{"type": "text", "text": cell, "styles": {}}]}
                props = cell.get("props") or {}
                css = f' style="background-color:{props["backgroundColor"]}"' if props.get("backgroundColor") else ""
                cells.append(f"<td{css}>{runs_html(cell.get('content') or [])}{self.blocks(cell.get('blocks') or [])}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        border = "" if attrs.get("borders", True) else " noborders"
        return f'<table class="f-table{border}"><colgroup>{cols}</colgroup>{"".join(rows)}</table>'


# --- Markdown export -> HTML ---------------------------------------------------------------------

_MARKER_RE = re.compile(r"<!--\s*(/?)fortress:(columns|column)\s*(\{.*?\})?\s*-->", re.DOTALL)


def markdown_html(markdown: str, title: str, media_prefix: str) -> str:
    from markdown_it import MarkdownIt  # noqa: PLC0415

    body = markdown
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        if end != -1:
            body = body[end + 5:]
    md = MarkdownIt("commonmark", {"html": True}).enable("table").enable("strikethrough")
    rendered = md.render(body)
    rendered = rendered.replace('src="/media/', f'src="{media_prefix}').replace('href="/media/', f'href="{media_prefix}')
    # $$ math $$ blocks: show them as display math source.
    rendered = re.sub(r"<p>\$\$\n?(.*?)\n?\$\$</p>", lambda m: f'<div class="f-math"><code>{m.group(1)}</code></div>',
                      rendered, flags=re.DOTALL)
    out: list[str] = []
    stack: list[bool] = []  # per open columns: is a column open?
    position = 0
    for match in _MARKER_RE.finditer(rendered):
        out.append(rendered[position:match.start()])
        position = match.end()
        closing, kind = match.group(1), match.group(2)
        value = {}
        if match.group(3):
            try:
                value = json.loads(match.group(3))
            except ValueError:
                value = {}
        if kind == "columns" and not closing:
            out.append('<div class="f-columns">')
            stack.append(False)
        elif kind == "column" and stack:
            if stack[-1]:
                out.append("</div>")
            width = value.get("width") or 1
            out.append(f'<div class="f-column" style="flex:{width} 1 0">')
            stack[-1] = True
        elif kind == "columns" and closing and stack:
            if stack.pop():
                out.append("</div>")
            out.append("</div>")
    out.append(rendered[position:])
    while stack:
        if stack.pop():
            out.append("</div>")
        out.append("</div>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)}</title><style>{STYLE}"
            "table { border-collapse: collapse; } td, th { border: 1px solid #a3a3a3; padding: 2px 6px; vertical-align: top; }"
            f"</style></head><body><h1 class='doc-title'>{_esc(title)}</h1>{''.join(out)}</body></html>")


def make_notes(root: Path | None = None) -> FortressNotes:
    return FortressNotes(Path(root or tempfile.mkdtemp(prefix="fortress-compare-")))


# --- Markdown export, as rendered ---------------------------------------------------------------


_INLINE = {"a", "b", "i", "u", "s", "em", "strong", "code", "sup", "sub", "span", "mark", "del", "small"}


def export_summary(md_html: str) -> dict:
    """What a Markdown viewer shows of the export: visible words (no comments, no
    title, no inline ``$LaTeX$`` / display math), code blocks and list nesting."""

    from html.parser import HTMLParser  # noqa: PLC0415

    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.pieces: list[str] = []
            self.skip = 0
            self.stack: list[str] = []
            self.pre = 0
            self.max_list_depth = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            classes = (attrs.get("class") or "").split()
            if tag in {"style", "script", "title"} or "doc-title" in classes or "f-math" in classes:
                self.skip += 1
                self.stack.append("skip:" + tag)
                return
            if tag in {"br", "img", "hr", "input", "col", "meta"}:
                self.pieces.append(" ")
                return
            if tag == "pre":
                self.pre += 1
            self.stack.append(tag)
            depth = sum(1 for t in self.stack if t in {"ul", "ol"})
            self.max_list_depth = max(self.max_list_depth, depth)
            if tag not in _INLINE:
                self.pieces.append(" ")

        def handle_endtag(self, tag):
            if not self.stack:
                return
            top = self.stack.pop()
            if top.startswith("skip:"):
                self.skip -= 1
            if top not in _INLINE:
                self.pieces.append(" ")

        def handle_data(self, data):
            if not self.skip:
                self.pieces.append(data)

    parser = _Parser()
    # Inline equations are written as `$LaTeX$` code spans.
    parser.feed(re.sub(r"<code>\$[^<]*\$</code>", " ", md_html))
    text = "".join(parser.pieces)
    return {"words": words(text), "pre": parser.pre, "list_depth": parser.max_list_depth, "text": norm_text(text)}


def tree_summary(tree: dict) -> dict:
    """Code blocks and deepest list nesting of the block tree (what the export should show)."""

    found = {"code": 0, "list_depth": 0}

    def visit(blocks, depth):
        for block in blocks or []:
            kind = block.get("type")
            if kind == "code":
                found["code"] += 1
            inner = depth + 1 if kind == "list" else depth
            found["list_depth"] = max(found["list_depth"], inner)
            visit(block.get("children") or [], inner if kind == "list" else 0)

    visit(tree.get("children") or [], 0)
    return found
