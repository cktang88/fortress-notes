"""Reference side: one2html's page HTML -> ``Model``.

one2html (MIT, https://github.com/msiemens/one2html) renders a page as
absolutely positioned outline containers (``body > div.container-outline``,
``img``, ink ``svg``), paragraphs as ``.outline-element`` (``div`` or ``li``),
text runs as ``span``/``a`` with inline CSS for their formatting, note tags as
``span.note-tag-icon`` (checkboxes carry ``role=checkbox aria-checked``),
tables as ``table/tr/td``, attachments as ``embed``/``audio``/``video`` and
equations as MathML. It never shares code with our converter, which makes it
an independent reference (both read the file with onenote_parser).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from model import MARK_NAMES, Model, norm_color, norm_text, sha256_file, tokens_from_pieces, words

VOID = {"img", "br", "embed", "meta", "link", "input", "hr", "col", "source", "wbr", "area", "base", "param", "track"}
FIELD_CODE_RE = re.compile(r'\ufddfHYPERLINK "[^"]*"?')
BLOCK_STYLE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "cite", "div", "li", "td", "ul", "ol", "table", "blockquote", "code", "pre"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def style(self) -> dict[str, str]:
        result = {}
        for part in (self.attrs.get("style") or "").split(";"):
            if ":" in part:
                key, value = part.split(":", 1)
                result[key.strip().lower()] = value.strip()
        return result

    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def text_content(self) -> str:
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text_content())
        return "".join(parts)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


def _find(root: Node, tag: str) -> Node | None:
    for node in root.iter():
        if node.tag == tag:
            return node
    return None


def page_id(html: str) -> str | None:
    match = re.search(r'name="X-Original-Page-Id" content="([^"]*)"', html)
    return match.group(1).upper() if match else None


def _px(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"(-?[\d.]+)px", value)
    return float(match.group(1)) if match else None


def _ancestors(node: Node):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _marks_of(text_parent: Node) -> frozenset[str]:
    marks: set[str] = set()
    for node in [text_parent, *_ancestors(text_parent)]:
        if node.tag in {"body", "#root"} or "container-outline" in node.classes:
            break
        if node.tag in BLOCK_STYLE_TAGS:
            # Paragraph-style formatting (heading colours, ...) is not a run mark, but a
            # paragraph background is what a highlighting note tag looks like.
            if node.tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "cite"}:
                background = norm_color(node.style().get("background-color"))
                if background and not any(m.startswith("highlight:") for m in marks):
                    marks.add(f"highlight:{background}")
            continue
        style = node.style()
        if style.get("font-weight") == "bold":
            marks.add("bold")
        if style.get("font-style") == "italic":
            marks.add("italic")
        decoration = style.get("text-decoration", "")
        if "underline" in decoration:
            marks.add("underline")
        if "line-through" in decoration:
            marks.add("strike")
        align = style.get("vertical-align")
        if align == "super":
            marks.add("superscript")
        elif align == "sub":
            marks.add("subscript")
        family = style.get("font-family", "").lower()
        if any(name in family for name in ("consolas", "courier", "lucida console", "monaco", "menlo")):
            marks.add("code")
        color = norm_color(style.get("color"))
        if color and "color" not in {m.split(":")[0] for m in marks}:
            marks.add(f"color:{color}")
        background = norm_color(style.get("background-color"))
        if background and "highlight" not in {m.split(":")[0] for m in marks}:
            marks.add(f"highlight:{background}")
    assert all(m.split(":")[0] in (*MARK_NAMES, "color", "highlight") for m in marks)
    return frozenset(marks)


def _paragraph(node: Node) -> Node | None:
    for ancestor in [node, *_ancestors(node)]:
        if "outline-element" in ancestor.classes:
            return ancestor
    return None


def _own_text(element: Node) -> str:
    """Text of a paragraph without its nested paragraphs / tables / svg."""

    parts = []
    for child in element.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag in {"svg", "math", "table", "ul", "ol", "style", "script"} or "outline-element" in child.classes:
            continue
        else:
            parts.append(_own_text(child))
    return "".join(parts)


def load(path: Path) -> tuple[str | None, Model, list[str]]:
    """(page id, model, ids of top-level container elements in DOM order)."""

    html = path.read_text(encoding="utf-8", errors="replace")
    pid = page_id(html)
    root = parse(html)
    body = _find(root, "body") or root
    model = Model()

    containers: list[Node] = []
    for child in body.children:
        if not isinstance(child, Node) or child.tag in {"script", "style"}:
            continue
        if "title" in child.classes:
            outlines = [c for c in child.children if isinstance(c, Node)]
            if outlines:
                model.title = norm_text(_own_text_deep(outlines[0]))
            continue
        containers.append(child)
    if not model.title:
        # one2html omits the title block when the title outline holds only a link marker etc.
        head_title = _find(root, "title")
        model.title = norm_text(head_title.text_content()) if head_title is not None else ""
    for index, container in enumerate(containers):
        style = container.style()
        model.containers.append({
            "index": index, "tag": container.tag, "left": _px(style.get("left")), "top": _px(style.get("top")),
            "text": "",
        })

    readable: list[str] = []
    for index, container in enumerate(containers):
        cwords: list[str] = []
        holder = Node("#holder", {}, None)
        holder.children = [container]  # the container itself may be an img / ink svg / embed
        _walk(holder, model, index, path.parent, cwords, readable, depth_state={"tables": 0})
        model.containers[index]["text"] = " ".join(cwords[:12])
        model.containers[index]["words"] = len(cwords)
    model.text = norm_text(" ".join(readable))
    return pid, model, [c.tag for c in containers]


def _own_text_deep(node: Node) -> str:
    return " ".join(norm_text(_own_text(n)) for n in node.iter() if "outline-element" in n.classes)


INLINE_TAGS = {"span", "a", "b", "i", "u", "s", "strong", "em", "sup", "sub", "font", "mark", "small", "big"}


class _State:
    """Walk state: inline text pieces are buffered and tokenised together per paragraph."""

    def __init__(self, model: Model, container: int, base: Path, cwords: list[str], readable: list[str]):
        self.model, self.container, self.base, self.cwords, self.readable = model, container, base, cwords, readable
        self.pieces: list[tuple[str, frozenset[str]]] = []
        self.tables = 0
        self.checked_paragraphs: set[int] = set()
        self.tagged_paragraphs: set[int] = set()
        self.aligned_paragraphs: set[int] = set()

    def flush(self) -> None:
        if self.pieces:
            tokens = tokens_from_pieces(self.pieces, self.container)
            self.model.tokens.extend(tokens)
            self.cwords.extend(t.word for t in tokens)
            self.readable.append("".join(t for t, _ in self.pieces))
            self.pieces = []


def _walk(node: Node, model: Model, container: int, base: Path, cwords: list[str], readable: list[str],
          depth_state: dict | None = None) -> None:
    state = _State(model, container, base, cwords, readable)
    _visit(node, state)
    state.flush()


def _visit(node: Node, st: _State) -> None:
    for child in node.children:
        if isinstance(child, str):
            # OneNote hides hyperlink field codes; one2html prints them when a link's
            # marker and display runs are split oddly.
            child = FIELD_CODE_RE.sub(" ", child)
            if child:
                st.pieces.append((child, _marks_of(node)))
            continue
        tag = child.tag
        classes = child.classes
        if tag in {"style", "script", "title"}:
            continue
        inline = tag in INLINE_TAGS and "note-tag-icon" not in classes
        if not inline:
            st.flush()
        _element(child, tag, classes, st)
        if not inline:
            st.flush()


def _file_entry(child: Node, st: _State, alt: bool) -> dict:
    src = child.attrs.get("src", "")
    file = (st.base / src) if src and "/" not in src.replace("\\", "/") else None
    empty = file is None or not file.is_file() or file.stat().st_size == 0
    entry = {"sha": None if empty else sha256_file(file), "name": src,
             "size": file.stat().st_size if file and file.is_file() else None}
    if alt:
        entry["alt"] = norm_text(child.attrs.get("alt", ""))
    return entry


def _element(child: Node, tag: str, classes: set[str], st: _State) -> None:
    model = st.model
    if tag == "svg":
        if any("note-tag-icon" in a.classes for a in _ancestors(child)):
            return
        title = _find(child, "title")
        model.ink.append({"text": norm_text(title.text_content()) if title else ""})
        return
    if tag == "math":
        model.math.append(norm_text(child.text_content()))
        return
    if "note-tag-icon" in classes:
        # One check / one "tagged" entry per paragraph: OneNote allows several tags on a
        # paragraph, Fortress keeps one checkbox and a list of labels.
        para = _paragraph(child)
        text = norm_text(_own_text(para)) if para is not None else ""
        key = id(para) if para is not None else id(child)
        if child.attrs.get("role") == "checkbox":
            if key not in st.checked_paragraphs:
                st.checked_paragraphs.add(key)
                model.checks.append((text, child.attrs.get("aria-checked") == "true"))
        elif key not in st.tagged_paragraphs:
            st.tagged_paragraphs.add(key)
            title = _find(child, "title")
            model.tags.append((norm_text(title.text_content()) if title else "tag", text))
        return
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        model.headings.append((int(tag[1]), norm_text(_own_text(child))))
    if tag == "li" and child.parent is not None and child.parent.tag in {"ul", "ol"} \
            and "tagged-list" not in child.parent.classes:
        depth = sum(1 for a in _ancestors(child) if a.tag in {"ul", "ol"} and "tagged-list" not in a.classes) - 1
        parent_style = child.parent.style().get("list-style-type", "decimal" if child.parent.tag == "ol" else "bullet")
        model.list_items.append((norm_text(_own_text(child)), child.parent.tag == "ol", depth, parent_style))
    align = child.style().get("text-align")
    if align in {"center", "right"} and tag != "td":
        # On the paragraph element, or (inside lists / tagged paragraphs) on its wrapper span.
        para = _paragraph(child) or child
        if id(para) not in st.aligned_paragraphs:
            st.aligned_paragraphs.add(id(para))
            model.aligned.append((align, norm_text(_own_text(para))))
    if tag == "a" and child.attrs.get("href"):
        model.links.append((norm_text(child.text_content()), child.attrs["href"]))
    if tag == "table":
        rows = [r for r in child.children if isinstance(r, Node) and r.tag == "tr"]
        for tbody in (c for c in child.children if isinstance(c, Node) and c.tag == "tbody"):
            rows += [r for r in tbody.children if isinstance(r, Node) and r.tag == "tr"]
        cells = [[c for c in r.children if isinstance(c, Node) and c.tag == "td"] for r in rows]
        entry = {
            "rows": len(rows), "cols": max((len(r) for r in cells), default=0), "cells": [],
            "bg": [[norm_color(c.style().get("background") or c.style().get("background-color")) for c in r]
                   for r in cells],
            "depth": st.tables,
        }
        model.tables.append(entry)
        st.tables += 1
        for row in cells:
            texts = []
            for cell in row:
                start = len(model.tokens)
                _visit(cell, st)
                st.flush()
                texts.append(" ".join(t.word for t in model.tokens[start:]))
            entry["cells"].append(texts)
        st.tables -= 1
        return
    if tag == "img":
        model.images.append(_file_entry(child, st, alt=True))
        return
    if tag in {"embed", "audio", "video"}:
        model.files.append(_file_entry(child, st, alt=False))
        return
    if tag == "br":
        st.pieces.append(("\n", frozenset()))
        return
    _visit(child, st)
