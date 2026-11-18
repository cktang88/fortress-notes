"""Markdown in and out of canonical blocks (docs/block-model.md §7).

Markdown is never stored: :func:`render_markdown` writes it from canonical
blocks (exports, the mirror files, the notes API), and :func:`parse_markdown`
reads it straight into canonical :class:`BlockSpec` trees (imports, capture, the
notes API). Every block carries its id (and whatever Markdown can't say) in a
``<!-- fortress:block {...} -->`` comment, so text written here and read back
keeps its block ids.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape as _html_escape
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import block_schema as schema
from . import provenance as _provenance
from .block_schema import BlockShapeError, text_node


@dataclass
class BlockSpec:
    """A canonical block with its children, before it is stored."""

    type: str
    attrs: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None
    children: list["BlockSpec"] = field(default_factory=list)
    id: str | None = None
    provenance: dict[str, Any] | None = None
    # Parsing only: the id of the block a Markdown comment says this one belongs under.
    parent_ref: str | None = field(default=None, repr=False, compare=False)

    def block(self) -> schema.Block:
        return schema.Block(self.type, self.attrs, self.content, self.payload)


def spec(type_name: str, attrs: dict | None = None, content: list | None = None,
         payload: dict | None = None, children: list[BlockSpec] | None = None) -> BlockSpec:
    """A validated BlockSpec (raises BlockShapeError)."""

    block = schema.normalize_block(type_name, attrs, content, payload)
    return BlockSpec(block.type, block.attrs, block.content, block.payload, list(children or []))


# Containers that may wrap a misplaced block to make it fit its parent.
_WRAPPERS = {"columns": "column", "table": "table_row", "table_row": "table_cell"}


def legalize(blocks: list[BlockSpec], parent_type: str | None = None) -> list[BlockSpec]:
    """``blocks`` rearranged so every block may sit where it is (nothing dropped).

    A block its parent can't hold is wrapped (a paragraph inside a table row gets
    a cell) or, for a structural container, replaced by its children; children
    of a block that holds none (code, images, ...) follow it as siblings.
    """

    allowed = schema.allowed_children(parent_type)
    result: list[BlockSpec] = []
    for block in blocks:
        if block.type not in allowed:
            wrapper = _WRAPPERS.get(parent_type) if parent_type else None
            if wrapper is not None:
                result.extend(legalize([BlockSpec(wrapper, children=[block])], parent_type))
            else:
                result.extend(legalize(block.children, parent_type))
            continue
        if block.children and not schema.block_type(block.type).children:
            hoisted, block.children = block.children, []
            result.append(block)
            result.extend(legalize(hoisted, parent_type))
        else:
            block.children = legalize(block.children, block.type)
            result.append(block)
    return result


# --- The fortress comment ------------------------------------------------------------------

_COMMENT_RE = re.compile(r"^<!--\s*fortress:block\s+(\{.*\})\s*-->$", re.DOTALL)
_MARKER_RE = re.compile(r"^<!--\s*(/?)fortress:(columns|column|file)\s*(\{.*\})?\s*-->$", re.DOTALL)


def _encode(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    # '>' and '--' can't occur inside an HTML comment; outside strings JSON has neither.
    return encoded.replace(">", "\\u003e").replace("--", "-\\u002d")


def comment(meta: dict[str, Any]) -> str:
    return f"<!-- fortress:block {_encode(meta)} -->"


def marker(kind: str, meta: dict[str, Any] | None = None) -> str:
    return f"<!-- fortress:{kind} {_encode(meta)} -->" if meta else f"<!-- fortress:{kind} -->"


def _parse_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_comment(source: str) -> dict[str, Any] | None:
    """The metadata of a ``fortress:block`` comment (None: not one).

    Older versions wrote the block's provenance itself (no ``id`` key); that is
    read as ``{"provenance": ...}``.
    """

    match = _COMMENT_RE.match(source.strip())
    if not match:
        return None
    value = _parse_json(match.group(1))
    if value is None:
        return None
    if "id" not in value and "source" in value:
        value = {"provenance": value}
    meta: dict[str, Any] = {}
    if isinstance(value.get("id"), str):
        meta["id"] = value["id"]
    if isinstance(value.get("provenance"), dict):
        try:
            restored = _provenance.normalize(value["provenance"], trusted=True)
        except _provenance.ProvenanceError:
            restored = {}
        if restored:
            meta["provenance"] = restored
    for key in ("attrs", "rows"):
        if isinstance(value.get(key), (dict, list)):
            meta[key] = value[key]
    for key in ("type", "parent"):
        if isinstance(value.get(key), str):
            meta[key] = value[key]
    return meta


# --- Inline content to Markdown ---------------------------------------------------------------

# Outermost first: the order marks open in (and close in reverse).
_MARK_ORDER = ("highlight", "color", "underline", "sup", "sub", "bold", "italic", "strike")
_MD_MARKS = {"bold": ("**", "**"), "italic": ("*", "*"), "strike": ("~~", "~~")}
_HTML_MARKS = {
    "bold": ("<strong>", "</strong>"), "italic": ("<em>", "</em>"), "strike": ("<s>", "</s>"),
    "underline": ("<u>", "</u>"), "sup": ("<sup>", "</sup>"), "sub": ("<sub>", "</sub>"),
}
_ENTITY_RE = re.compile(r"&#?[A-Za-z0-9]+;")
_ORDERED_START_RE = re.compile(r"\d{1,9}(?=[.)])")


def _mark_tags(name: str, value: Any, html: bool) -> tuple[str, str]:
    if name == "color":
        return f'<span style="color:{value}">', "</span>"
    if name == "highlight":
        return f'<mark style="background:{value}">', "</mark>"
    if not html and name in _MD_MARKS:
        return _MD_MARKS[name]
    return _HTML_MARKS[name]


def _escape_markdown(text: str, at_line_start: bool, table: bool) -> str:
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if at_line_start and char not in " \t":
            at_line_start = False
            digits = _ORDERED_START_RE.match(text, index)
            if digits:
                # "1. x" would start a list: escape the dot.
                end = digits.end()
                out.append(text[index:end] + "\\" + text[end])
                index = end + 1
                continue
            if char in "#>-+=" or (char == "$" and text[index + 1:index + 2] == "$"):
                out.append("\\" + char)
                index += 1
                continue
        if char == "\n":
            out.append(char)
            at_line_start = True
        elif char in "\\`*_~<[]":
            out.append("\\" + char)
        elif char == "(" and text[index + 1:index + 2] == "(":
            out.append("\\(")
        elif char == "|" and table:
            out.append("\\|")
        elif char == "&" and _ENTITY_RE.match(text, index):
            out.append("\\&")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _code_span(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _split_whitespace(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move edge spaces out of emphasis (``**a **`` is not bold in Markdown)."""

    result: list[dict[str, Any]] = []
    for node in nodes:
        marks = node.get("marks") or {}
        if node["type"] != "text" or not any(marks.get(m) for m in _MD_MARKS):
            result.append(node)
            continue
        text = node["text"]
        core = text.strip(" ")
        if not core:
            result.append(node)
            continue
        plain = {k: v for k, v in marks.items() if k not in _MD_MARKS and k != "code"}
        lead = text[: len(text) - len(text.lstrip(" "))]
        trail = text[len(text.rstrip(" ")):]
        if lead:
            result.append(text_node(lead, plain))
        result.append(text_node(core, marks))
        if trail:
            result.append(text_node(trail, plain))
    return schema.merge_inline(result)


def _ref_text(node: dict[str, Any]) -> str:
    return f"[[{node['id']}]]" if node["type"] == "docLink" else f"(({node['id']}))"


def _render_inline(nodes: list[dict[str, Any]], *, html: bool, newline: str, table: bool) -> str:
    """Inline nodes as Markdown (``html``: every mark as an HTML tag) or as pure HTML."""

    out: list[str] = []
    stack: list[tuple[str, Any]] = []
    state = {"line_start": True}

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            name, value = stack.pop()
            out.append(_mark_tags(name, value, html)[1])

    def text_of(text: str, code: bool) -> str:
        if code and not html:
            return _code_span(text)
        pieces = text.split("\n")
        rendered: list[str] = []
        for number, piece in enumerate(pieces):
            if number:
                rendered.append(newline)
                state["line_start"] = True
            if piece:
                if html:
                    rendered.append(_html_escape(piece, quote=False))
                else:
                    rendered.append(_escape_markdown(piece, state["line_start"], table))
                state["line_start"] = False
        return "".join(rendered)

    def emit(items: list[dict[str, Any]]) -> None:
        for node in items:
            kind = node["type"]
            if kind in ("docLink", "blockRef"):
                close_to(0)
                out.append(_ref_text(node))
                state["line_start"] = False
                continue
            if kind == "link":
                close_to(0)
                if html:
                    out.append(f'<a href="{_attr(node["href"])}">')
                    emit(node["content"])
                    close_to(0)
                    out.append("</a>")
                else:
                    out.append("[")
                    emit(node["content"])
                    close_to(0)
                    out.append("](" + _link_destination(node["href"]) + ")")
                state["line_start"] = False
                continue
            marks = node.get("marks") or {}
            wanted = [(name, marks[name]) for name in _MARK_ORDER if marks.get(name)]
            keep = 0
            while keep < len(stack) and keep < len(wanted) and stack[keep] == wanted[keep]:
                keep += 1
            close_to(keep)
            for name, value in wanted[keep:]:
                stack.append((name, value))
                out.append(_mark_tags(name, value, html)[0])
            if marks.get("code") and html:
                out.append("<code>" + text_of(node["text"], True) + "</code>")
            else:
                out.append(text_of(node["text"], bool(marks.get("code"))))

    emit(nodes)
    close_to(0)
    return "".join(out)


def _link_destination(href: str) -> str:
    if re.search(r"[\s()<>]", href):
        return "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
    return href


def _comparable(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat = [
        {**node, "href": _INLINE_MD.normalizeLink(node["href"])} if node["type"] == "link" else node
        for node in nodes
    ]
    return schema.merge_inline(flat)


def inline_to_markdown(nodes: list[dict[str, Any]], *, newline: str = "\\\n",
                       table: bool = False) -> str:
    """Markdown for inline nodes: native marks where CommonMark has them, HTML otherwise.

    If the native form would not read back as the same nodes (emphasis next to
    punctuation, ...), every mark is written as an HTML tag instead.
    """

    nodes = _split_whitespace(nodes)
    check_newline = "<br>" if table else "\\\n"
    native = _render_inline(nodes, html=False, newline=check_newline, table=table)
    parsed, images = inline_from_markdown(native)
    if not images and _comparable(parsed) == _comparable(nodes):
        if newline == check_newline:
            return native
        return _render_inline(nodes, html=False, newline=newline, table=table)
    # Marks as HTML tags, text still Markdown-escaped.
    return _render_markdown_html_marks(nodes, newline="<br>" if table else newline, table=table)


def _render_markdown_html_marks(nodes: list[dict[str, Any]], *, newline: str, table: bool) -> str:
    out: list[str] = []
    for node in nodes:
        if node["type"] in ("docLink", "blockRef"):
            out.append(_ref_text(node))
        elif node["type"] == "link":
            inner = _render_markdown_html_marks(node["content"], newline=newline, table=table)
            out.append(f"[{inner}]({_link_destination(node['href'])})")
        else:
            marks = node.get("marks") or {}
            body = _escape_markdown(node["text"], False, table).replace("\n", newline)
            if marks.get("code"):
                body = "<code>" + _html_escape(node["text"], quote=False).replace("\n", "<br>") + "</code>"
            for name in reversed(_MARK_ORDER):
                if marks.get(name):
                    opening, closing = _mark_tags(name, marks[name], True)
                    body = opening + body + closing
            out.append(body)
    return "".join(out)


def inline_to_html(nodes: list[dict[str, Any]]) -> str:
    """HTML for inline nodes (inside HTML tables)."""

    return _render_inline(nodes, html=True, newline="<br>", table=False)


# --- Markdown/HTML to inline content ------------------------------------------------------------

_REF_RE = re.compile(
    r"\[\[([A-Za-z0-9_-]{1,128})(?:\|[^\]\n]*)?\]\]"
    r"|\(\(\s*([A-Za-z0-9_-]{1,128})(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)\)"
)


def _ref_tokens(state) -> None:
    """Core rule: ``[[id]]`` and ``((id))`` in unescaped text become reference tokens."""

    for block_token in state.tokens:
        if block_token.type != "inline" or not block_token.children:
            continue
        children: list[Token] = []
        for token in block_token.children:
            if token.type != "text" or ("[[" not in token.content and "((" not in token.content):
                children.append(token)
                continue
            last = 0
            for match in _REF_RE.finditer(token.content):
                if match.start() > last:
                    piece = Token("text", "", 0)
                    piece.content = token.content[last:match.start()]
                    children.append(piece)
                ref = Token("fortress_ref", "", 0)
                ref.meta = {"kind": "docLink" if match.group(1) else "blockRef",
                            "id": match.group(1) or match.group(2)}
                children.append(ref)
                last = match.end()
            if last < len(token.content):
                piece = Token("text", "", 0)
                piece.content = token.content[last:]
                children.append(piece)
        block_token.children = children


def _render_ref(self, tokens, index, options, env) -> str:
    meta = tokens[index].meta
    return f'<fortress-ref data-kind="{meta["kind"]}" data-id="{meta["id"]}"></fortress-ref>'


def _render_softbreak(self, tokens, index, options, env) -> str:
    return " "


def _markdown(*plugins: str) -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(list(plugins)) if plugins else MarkdownIt("commonmark")
    md.core.ruler.before("text_join", "fortress_refs", _ref_tokens)
    md.add_render_rule("fortress_ref", _render_ref)
    md.add_render_rule("softbreak", _render_softbreak)
    return md


_INLINE_MD = _markdown("strikethrough")
_BLOCK_MD = _markdown("table", "strikethrough")


def inline_from_markdown(markdown: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(inline nodes, image attrs)`` for one block's inline Markdown.

    Images can't be inline content: they are returned separately (the caller
    makes them image blocks after the text).
    """

    tokens = _INLINE_MD.parseInline(markdown)
    return _nodes_from_inline_token(tokens[0] if tokens else None)


def _nodes_from_inline_token(token: Token | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if token is None or not token.children:
        return [], []
    html = _BLOCK_MD.renderer.render(token.children, _BLOCK_MD.options, {})
    return _nodes_from_html(_parse_html(html), collapse=False)


def inline_from_html(html: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _nodes_from_html(_parse_html(html), collapse=True)


# --- A tiny HTML tree --------------------------------------------------------------------------

_VOID = {"br", "img", "hr", "col", "input", "meta", "wbr", "source", "link"}


class _Element:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Element | str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(element)
        if tag not in _VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Element(tag, {key: value or "" for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _parse_html(html: str) -> list[_Element | str]:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root.children


def _style_map(element: _Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in element.attrs.get("style", "").split(";"):
        if ":" in part:
            key, _, value = part.partition(":")
            result[key.strip().lower()] = value.strip()
    return result


_TAG_MARKS = {
    "strong": "bold", "b": "bold", "em": "italic", "i": "italic", "s": "strike",
    "del": "strike", "strike": "strike", "u": "underline", "ins": "underline",
    "sup": "sup", "sub": "sub", "code": "code",
}


def _safe_color(value: str | None) -> str | None:
    if not value:
        return None
    try:
        color = schema.color_value(value, "colour")
    except BlockShapeError:
        return None
    return None if color == "default" else color


def _element_marks(element: _Element) -> dict[str, Any]:
    tag = element.tag
    if tag in _TAG_MARKS:
        return {_TAG_MARKS[tag]: True}
    css = _style_map(element)
    marks: dict[str, Any] = {}
    color = _safe_color(css.get("color") or (element.attrs.get("color") if tag == "font" else None))
    background = _safe_color(css.get("background-color") or css.get("background"))
    if tag == "mark":
        background = background or _safe_color(element.attrs.get("data-color")) or "yellow"
    if color:
        marks["color"] = color
    if background:
        marks["highlight"] = background
    return marks


def _with_marks(marks: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = {**marks, **extra}
    if extra.get("sup"):
        merged.pop("sub", None)
    if extra.get("sub"):
        merged.pop("sup", None)
    return merged


def _nodes_from_html(
    elements: list[_Element | str], *, collapse: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    state = {"skip_newline": False}

    def text(value: str, marks: dict[str, Any], target: list[dict[str, Any]]) -> None:
        if state["skip_newline"] and value.startswith("\n"):
            value = value[1:]
        state["skip_newline"] = False
        if collapse:
            value = re.sub(r"\s+", " ", value)
        if value:
            target.append(text_node(value, marks))

    def walk(items: list[_Element | str], marks: dict[str, Any], target: list, in_link: bool) -> None:
        for item in items:
            if isinstance(item, str):
                text(item, marks, target)
                continue
            tag = item.tag
            if tag == "br":
                target.append(text_node("\n", marks))
                state["skip_newline"] = True
            elif tag == "img":
                images.append(image_attrs_from_element(item))
            elif tag == "fortress-ref" and not in_link:
                kind = item.attrs.get("data-kind")
                if kind in ("docLink", "blockRef") and item.attrs.get("data-id"):
                    target.append({"type": kind, "id": item.attrs["data-id"]})
            elif tag == "a" and not in_link:
                href = item.attrs.get("href", "").strip()
                inner: list[dict[str, Any]] = []
                walk(item.children, marks, inner, True)
                inner = schema.merge_inline(inner)
                if href and inner:
                    target.append({"type": "link", "href": href, "content": inner})
                else:
                    target.extend(inner)
            else:
                # Marks; any other inline tag keeps its text.
                walk(item.children, _with_marks(marks, _element_marks(item)), target, in_link)

    walk(elements, {}, nodes, False)
    nodes = schema.merge_inline(nodes)
    if collapse and nodes:
        if nodes[0]["type"] == "text":
            nodes[0] = {**nodes[0], "text": nodes[0]["text"].lstrip(" ")}
        if nodes[-1]["type"] == "text":
            nodes[-1] = {**nodes[-1], "text": nodes[-1]["text"].rstrip(" ")}
        nodes = schema.merge_inline(nodes)
    return nodes, images


def _attr(value: object) -> str:
    return _html_escape(str(value), quote=True).replace("\n", "&#10;").replace("\r", "")


def _json_attr(value: dict[str, Any]) -> str:
    return _attr(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _number(value: str | None) -> int | float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


# --- Images --------------------------------------------------------------------------------------

_IMAGE_NATIVE = ("url", "alt", "caption", "width", "height", "name")


def image_html(attrs: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    parts = [f'<img src="{_attr(attrs.get("url") or "")}"']
    if attrs.get("alt"):
        parts.append(f'alt="{_attr(attrs["alt"])}"')
    if attrs.get("caption"):
        parts.append(f'title="{_attr(attrs["caption"])}"')
    for key in ("width", "height"):
        if attrs.get(key) is not None:
            parts.append(f'{key}="{attrs[key]}"')
    if attrs.get("name"):
        parts.append(f'data-name="{_attr(attrs["name"])}"')
    if meta:
        parts.append(f'data-block="{_json_attr(meta)}"')
    return " ".join(parts) + ">"


def image_attrs_from_element(element: _Element) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    legacy = _parse_json(element.attrs.get("data-attrs"))
    if legacy:
        attrs.update(legacy)
    if "previewWidth" in attrs:
        attrs.setdefault("width", attrs.pop("previewWidth"))
    attrs["url"] = element.attrs.get("src", "")
    for html_name, key in (("alt", "alt"), ("title", "caption"), ("data-name", "name")):
        if element.attrs.get(html_name):
            attrs[key] = element.attrs[html_name]
    for key in ("width", "height"):
        number = _number(element.attrs.get(key))
        if number is not None:
            attrs[key] = number
    return attrs


# --- Rendering --------------------------------------------------------------------------------------

# Attributes the Markdown syntax of a type already says (the rest go in its comment).
_NATIVE_ATTRS = {
    "heading": {"level"}, "check_item": {"checked"}, "code": {"language"},
    "image": set(_IMAGE_NATIVE),
}
_LIST_TYPES = ("bullet_item", "numbered_item", "check_item")
_INLINE_COMMENT_TYPES = frozenset({"paragraph", "heading", *_LIST_TYPES})


class _Renderer:
    def __init__(self, document_provenance: dict[str, Any] | None, include_ids: bool):
        self.document_source = (document_provenance or {}).get("source", _provenance.DEFAULT_SOURCE)
        self.include_ids = include_ids

    def meta(self, node: dict[str, Any], *, parent: str | None = None,
             skip_attrs: bool = False) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if self.include_ids and node.get("id"):
            meta["id"] = node["id"]
        own = node.get("provenance")
        if isinstance(own, dict) and own and not own.get("inherited"):
            if own != {"source": self.document_source}:
                meta["provenance"] = {k: v for k, v in own.items() if k != "inherited"}
        if not skip_attrs:
            native = _NATIVE_ATTRS.get(node["type"], set())
            extra = {k: v for k, v in (node.get("attrs") or {}).items() if k not in native}
            if extra:
                meta["attrs"] = extra
        if parent and self.include_ids:
            meta["parent"] = parent
        return meta

    def blocks(self, nodes: list[dict[str, Any]], parent: str | None = None) -> list[str]:
        fragments: list[str] = []
        ordinal = 0
        previous: str | None = None
        for node in nodes:
            kind = node["type"]
            ordinal = ordinal + 1 if kind == "numbered_item" else 0
            rendered = [part for part in self.block(node, ordinal, parent) if part]
            if kind in _LIST_TYPES and previous == kind and fragments and rendered:
                # Items of one list stay together (a tight list).
                fragments[-1] += "\n" + rendered.pop(0)
            fragments.extend(rendered)
            previous = kind
        return fragments

    def block(self, node: dict[str, Any], ordinal: int, parent: str | None) -> list[str]:
        kind = node["type"]
        children = node.get("children") or []
        if kind == "columns":
            fragments = [marker("columns", self.meta(node, parent=parent))]
            for column in children:
                fragments.append(marker("column", self.meta(column)))
                fragments.extend(self.blocks(column.get("children") or []))
            fragments.append("<!-- /fortress:columns -->")
            return fragments
        if kind in _INLINE_COMMENT_TYPES:
            meta = self.meta(node, parent=parent)
            text = self.text_block(node, ordinal, meta)
            if kind in _LIST_TYPES:
                indent = " " * len(self.list_marker(node, ordinal))
                body = "\n\n".join(self.blocks(children))
                if body:
                    gap = "\n" if children[0]["type"] in _LIST_TYPES else "\n\n"
                    text += gap + _indent(body, indent)
                return [text]
            # Markdown can't nest under a paragraph: children follow, naming their parent.
            return [text, *self.blocks(children, parent=node.get("id"))]
        meta = self.meta(node, parent=parent, skip_attrs=kind in ("table", "table_row", "table_cell"))
        if kind == "code":
            source = self.code(node)
        elif kind in ("quote", "callout"):
            if kind == "callout":
                meta["type"] = "callout"
            body = "\n\n".join(self.blocks(children))
            source = "\n".join(f"> {line}" if line else ">" for line in (body or "").split("\n"))
        elif kind == "divider":
            source = "---"
        elif kind == "image":
            return [image_html(node.get("attrs") or {}, meta or None)]
        elif kind == "file":
            meta["type"] = "file"
            meta["attrs"] = node.get("attrs") or {}
            source = file_link(node.get("attrs") or {})
        elif kind == "math":
            latex = str((node.get("payload") or {}).get("latex", ""))
            body = "\n".join(line for line in latex.split("\n") if line.strip())
            source = f"$$\n{body}\n$$"
        elif kind == "html":
            source = str((node.get("payload") or {}).get("html", "")).strip() or "<div></div>"
        elif kind == "table":
            return [self.table(node, meta)]
        else:  # pragma: no cover - every flow type is handled above
            source = ""
        return [f"{comment(meta)}\n{source}" if meta else source]

    def list_marker(self, node: dict[str, Any], ordinal: int) -> str:
        if node["type"] == "numbered_item":
            return f"{ordinal}. "
        if node["type"] == "check_item":
            return "- [x] " if (node.get("attrs") or {}).get("checked") else "- [ ] "
        return "- "

    def text_block(self, node: dict[str, Any], ordinal: int, meta: dict[str, Any]) -> str:
        kind = node["type"]
        nodes = node.get("content") or []
        tail = f" {comment(meta)}" if meta else ""
        if kind == "heading":
            level = (node.get("attrs") or {}).get("level", 1)
            body = inline_to_markdown(nodes, newline="<br>")
            return "#" * level + " " + body + tail
        if kind in _LIST_TYPES:
            prefix = self.list_marker(node, ordinal)
            body = inline_to_markdown(nodes, newline="\\\n" + " " * len(prefix))
            if not body and not tail:
                return prefix.rstrip()
            return prefix + body + tail
        body = inline_to_markdown(nodes)
        if not body:
            # An empty paragraph is written only to keep its id.
            return comment(meta) if meta else ""
        return body + tail

    def code(self, node: dict[str, Any]) -> str:
        code = schema.inline_text(node.get("content"))
        longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
        fence = "`" * (longest + 1)
        language = (node.get("attrs") or {}).get("language") or ""
        return f"{fence}{language}\n{code}\n{fence}"

    def table(self, node: dict[str, Any], meta: dict[str, Any]) -> str:
        gfm = self.gfm_table(node, meta)
        if gfm is not None:
            return gfm
        return table_html(node, self)

    def gfm_table(self, node: dict[str, Any], meta: dict[str, Any]) -> str | None:
        attrs = node.get("attrs") or {}
        rows = node.get("children") or []
        if not rows or attrs.get("header_rows") != 1 or attrs.get("header_cols"):
            return None
        if any(key not in ("header_rows", "borders") for key in attrs) or attrs.get("borders") is False:
            return None
        width = len(rows[0].get("children") or [])
        if not width or any(len(row.get("children") or []) != width for row in rows):
            return None
        aligns: list[str | None] = []
        row_meta: list[dict[str, Any]] = []
        for number, row in enumerate(rows):
            if row.get("attrs") or self.meta(row, skip_attrs=True).keys() - {"id"}:
                return None
            cells_meta = []
            for column, cell in enumerate(row["children"]):
                cell_attrs = cell.get("attrs") or {}
                if cell_attrs.keys() - {"align"} or cell_attrs.get("align") == "justify":
                    return None
                if self.meta(cell, skip_attrs=True).keys() - {"id"}:
                    return None
                if number == 0:
                    aligns.append(cell_attrs.get("align"))
                elif cell_attrs.get("align") != aligns[column]:
                    return None
                inner = cell.get("children") or []
                if len(inner) > 1:
                    return None
                block_meta = None
                if inner:
                    paragraph = inner[0]
                    if paragraph["type"] != "paragraph" or paragraph.get("children"):
                        return None
                    block_meta = self.meta(paragraph)
                    if block_meta.keys() - {"id", "provenance"}:
                        return None
                cells_meta.append({"id": cell.get("id"), "block": block_meta})
            row_meta.append({"id": row.get("id"), "cells": cells_meta})
        lines = []
        for number, row in enumerate(rows):
            cells = []
            for cell in row["children"]:
                inner = cell.get("children") or []
                content = inner[0].get("content") or [] if inner else []
                cells.append(inline_to_markdown(content, newline="<br>", table=True))
            lines.append("| " + " | ".join(cells) + " |")
            if number == 0:
                separators = [{"center": ":---:", "right": "---:", "left": ":---"}.get(a or "", "---")
                              for a in aligns]
                lines.append("| " + " | ".join(separators) + " |")
        if self.include_ids:
            meta = {**meta, "rows": row_meta}
        return (comment(meta) + "\n" if meta else "") + "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def file_link(attrs: dict[str, Any]) -> str:
    name = str(attrs.get("name") or attrs.get("url") or "file")
    url = attrs.get("url") or ""
    label = _escape_markdown(name, True, False)
    link = f"[{label}]({_link_destination(url)})" if url else label
    if isinstance(attrs.get("size"), (int, float)):
        link += f" ({_human_size(attrs['size'])})"
    return link


def _human_size(size: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover


def render_markdown(nodes: list[dict[str, Any]], *, document_provenance: dict | None = None,
                    include_ids: bool = True) -> str:
    """A block tree (``document_tree`` nodes) as Markdown.

    With ``include_ids`` every block's id is written in its fortress comment;
    provenance that differs from ``document_provenance`` and attributes the
    Markdown syntax can't say are always written.
    """

    return "\n\n".join(
        fragment for fragment in _Renderer(document_provenance, include_ids).blocks(nodes) if fragment
    )


# --- HTML blocks (tables) ---------------------------------------------------------------------------


def table_html(node: dict[str, Any], renderer: _Renderer) -> str:
    """A table as HTML: rows, cells and cell blocks carry their metadata in data-block."""

    attrs = node.get("attrs") or {}
    header_rows = attrs.get("header_rows") or 0
    header_cols = attrs.get("header_cols") or 0
    lines = [f"<table{_block_attr(renderer, node, attrs)}>"]
    widths = attrs.get("column_widths") or []
    if any(width is not None for width in widths):
        cols = "".join(
            f'<col style="width:{width}px">' if width is not None else "<col>" for width in widths
        )
        lines.append(f"<colgroup>{cols}</colgroup>")
    for row_number, row in enumerate(node.get("children") or []):
        cells = []
        for column, cell in enumerate(row.get("children") or []):
            tag = "th" if row_number < header_rows or column < header_cols else "td"
            cell_attrs = cell.get("attrs") or {}
            cells.append(
                f"<{tag}{_cell_attributes(cell_attrs)}{_block_attr(renderer, cell, cell_attrs)}>"
                f"{blocks_html(cell.get('children') or [], renderer)}</{tag}>"
            )
        lines.append(f"<tr{_block_attr(renderer, row, row.get('attrs') or {})}>" + "".join(cells) + "</tr>")
    lines.append("</table>")
    return "\n".join(lines)


_TABLE_NATIVE = {
    "table": {"column_widths", "header_rows", "header_cols"},
    "table_cell": {"colspan", "rowspan", "background", "color", "align"},
}


def _block_attr(renderer: _Renderer, node: dict[str, Any], attrs: dict[str, Any]) -> str:
    meta = renderer.meta(node, skip_attrs=True)
    native = _TABLE_NATIVE.get(node["type"], _NATIVE_ATTRS.get(node["type"], set()))
    extra = {k: v for k, v in attrs.items() if k not in native}
    if extra:
        meta["attrs"] = extra
    return f' data-block="{_json_attr(meta)}"' if meta else ""


def _cell_attributes(attrs: dict[str, Any]) -> str:
    css = []
    if attrs.get("background"):
        css.append(f"background-color:{attrs['background']}")
    if attrs.get("color"):
        css.append(f"color:{attrs['color']}")
    if attrs.get("align"):
        css.append(f"text-align:{attrs['align']}")
    result = f' style="{_attr(";".join(css))}"' if css else ""
    for key in ("colspan", "rowspan"):
        if attrs.get(key):
            result += f' {key}="{int(attrs[key])}"'
    return result


def blocks_html(nodes: list[dict[str, Any]], renderer: _Renderer) -> str:
    """Blocks (inside a table cell) as one line of HTML."""

    out: list[str] = []
    index = 0
    while index < len(nodes):
        kind = nodes[index]["type"]
        if kind in _LIST_TYPES:
            tag = "ol" if kind == "numbered_item" else "ul"
            items = []
            while index < len(nodes) and nodes[index]["type"] == kind:
                items.append(_list_item_html(nodes[index], renderer))
                index += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        out.append(_block_html(nodes[index], renderer))
        index += 1
    return "".join(out)


def _list_item_html(node: dict[str, Any], renderer: _Renderer) -> str:
    attrs = node.get("attrs") or {}
    checked = ""
    if node["type"] == "check_item":
        checked = f' data-checked="{"true" if attrs.get("checked") else "false"}"'
    inner = inline_to_html(node.get("content") or [])
    children = node.get("children") or []
    return (f"<li{checked}{_block_attr(renderer, node, attrs)}>{inner}"
            f"{blocks_html(children, renderer) if children else ''}</li>")


def _block_html(node: dict[str, Any], renderer: _Renderer) -> str:
    kind = node["type"]
    attrs = node.get("attrs") or {}
    data = _block_attr(renderer, node, attrs)
    children = node.get("children") or []
    tail = f'<div data-children="">{blocks_html(children, renderer)}</div>' if children else ""
    content = node.get("content") or []
    if kind == "heading":
        level = attrs.get("level", 1)
        return f"<h{level}{data}>{inline_to_html(content)}</h{level}>{tail}"
    if kind in ("quote", "callout"):
        callout = ' data-type="callout"' if kind == "callout" else ""
        return f"<blockquote{callout}{data}>{blocks_html(children, renderer)}</blockquote>"
    if kind == "code":
        language = attrs.get("language")
        css = f' class="language-{_attr(language)}"' if language else ""
        code = _html_escape(schema.inline_text(content), quote=False).replace("\n", "&#10;")
        return f"<pre{data}><code{css}>{code}</code></pre>"
    if kind == "divider":
        return f"<hr{data}>"
    if kind == "table":
        return table_html(node, renderer).replace("\n", "")
    if kind == "image":
        return image_html(attrs, renderer.meta(node, skip_attrs=False) or None)
    if kind == "file":
        meta = {**renderer.meta(node, skip_attrs=True), "attrs": attrs}
        name = _html_escape(str(attrs.get("name") or attrs.get("url") or "file"), quote=False)
        return (f'<div data-type="file" data-block="{_json_attr(meta)}">'
                f'<a href="{_attr(attrs.get("url", ""))}">{name}</a></div>')
    if kind == "math":
        latex = _html_escape(str((node.get("payload") or {}).get("latex", "")), quote=False)
        return f'<div data-type="math"{data}>{latex.replace(chr(10), "&#10;")}</div>'
    if kind == "html":
        raw = str((node.get("payload") or {}).get("html", ""))
        return f'<div data-type="html" data-html="{_attr(raw)}"{data}></div>'
    return f"<p{data}>{inline_to_html(content)}</p>{tail}"


# --- Parsing ---------------------------------------------------------------------------------------


def parse_markdown(body: str) -> list[BlockSpec]:
    """Canonical blocks for a Markdown body (ids and metadata from fortress comments)."""

    body = body.replace("\r\n", "\n").replace("\r", "\n")
    tokens = _BLOCK_MD.parse(body)
    blocks, _ = _Parser(tokens).sequence(0)
    return legalize(blocks)


def _apply_meta(block: BlockSpec, meta: dict[str, Any] | None) -> BlockSpec:
    if not meta:
        return block
    if meta.get("id"):
        block.id = meta["id"]
    if meta.get("provenance"):
        block.provenance = meta["provenance"]
    wanted = meta.get("type")
    if isinstance(wanted, str) and wanted in schema.TYPES and wanted != block.type:
        try:
            schema.check_type_change(block.type, wanted)
            block.attrs = schema.retype_attrs(block.type, wanted, block.attrs)
            block.type = wanted
        except BlockShapeError:
            pass
    if isinstance(meta.get("attrs"), dict):
        try:
            block.attrs = schema.normalize_attrs(block.type, {**block.attrs, **meta["attrs"]})
        except BlockShapeError:
            pass
    if isinstance(meta.get("parent"), str):
        block.parent_ref = meta["parent"]
    return block


@dataclass
class _Marker:
    kind: str
    meta: dict[str, Any]


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens

    def sequence(self, index: int, stop: str | None = None) -> tuple[list[BlockSpec], int]:
        """Blocks from ``tokens[index:]`` up to the ``stop`` closing token."""

        tokens = self.tokens
        items: list[BlockSpec | _Marker] = []
        pending: dict[str, Any] | None = None
        while index < len(tokens):
            token = tokens[index]
            if stop and token.type == stop:
                index += 1
                break
            produced: list[BlockSpec] = []
            if token.type == "html_block":
                index += 1
                meta = parse_comment(token.content)
                if meta is not None:
                    if pending and pending.get("type") == "file":
                        items.append(self.file(pending))
                    pending = meta
                    continue
                found = _MARKER_RE.match(token.content.strip())
                if found:
                    value = _parse_json(found.group(3)) or {}
                    kind = found.group(1) + found.group(2)
                    if kind == "file":  # written by older versions
                        pending = {"type": "file", "attrs": value}
                        continue
                    items.append(_Marker(kind, _marker_meta(value)))
                    continue
                produced = self.html_block(token.content)
            elif token.type in ("bullet_list_open", "ordered_list_open"):
                produced, index = self.list(index)
            elif token.type == "paragraph_open":
                inline = tokens[index + 1]
                index += 3
                if pending and pending.get("type") == "file":
                    items.append(self.file(pending))
                    pending = None
                    continue
                produced = self.paragraph(inline)
            elif token.type == "heading_open":
                inline = tokens[index + 1]
                index += 3
                nodes, images, meta = self.inline(inline)
                produced = [_apply_meta(spec("heading", {"level": int(token.tag[1:])}, nodes), meta)]
                produced += [spec("image", image) for image in images]
            elif token.type == "blockquote_open":
                children, index = self.sequence(index + 1, "blockquote_close")
                produced = [spec("quote", children=children)]
            elif token.type in ("fence", "code_block"):
                index += 1
                code = token.content[:-1] if token.content.endswith("\n") else token.content
                language = token.info.strip().split()[0] if token.type == "fence" and token.info.strip() else None
                produced = [spec("code", {"language": language}, [text_node(code)] if code else [])]
            elif token.type == "hr":
                index += 1
                produced = [spec("divider")]
            elif token.type == "table_open":
                produced, index = self.gfm_table(index)
            else:
                index += 1
                continue
            if pending and produced:
                if pending.get("type") == "file":
                    items.append(self.file(pending))
                else:
                    _apply_meta(produced[0], pending)
                    if produced[0].type == "table" and isinstance(pending.get("rows"), list):
                        _apply_table_ids(produced[0], pending["rows"])
                pending = None
            items.extend(produced)
        if pending and pending.get("type") == "file":
            items.append(self.file(pending))
        return _attach_parents(_fold_layout(items)), index

    def file(self, meta: dict[str, Any]) -> BlockSpec:
        attrs = meta.get("attrs") if isinstance(meta.get("attrs"), dict) else {}
        try:
            block = spec("file", attrs)
        except BlockShapeError:
            block = spec("file")
        return _apply_meta(block, {k: v for k, v in meta.items() if k not in ("type", "attrs")})

    def inline(self, token: Token) -> tuple[list[dict], list[dict], dict[str, Any] | None]:
        """A paragraph's or heading's nodes, images and trailing fortress comment."""

        children = list(token.children or [])
        meta = None
        while children and children[-1].type == "text" and not children[-1].content.strip():
            children.pop()
        if children and children[-1].type == "html_inline":
            meta = parse_comment(children[-1].content)
            if meta is not None:
                children.pop()
                while children and children[-1].type in ("softbreak", "hardbreak"):
                    children.pop()
                if children and children[-1].type == "text":
                    children[-1] = _copy_token(children[-1], children[-1].content.rstrip())
        stripped = Token("inline", "", 0)
        stripped.children = children
        nodes, images = _nodes_from_inline_token(stripped)
        return nodes, images, meta

    def paragraph(self, token: Token) -> list[BlockSpec]:
        latex = _math_source(token.content)
        if latex is not None:
            return [spec("math", payload={"latex": latex})]
        nodes, images, meta = self.inline(token)
        blocks: list[BlockSpec] = []
        if nodes or not images:
            blocks.append(_apply_meta(spec("paragraph", content=nodes), meta))
            meta = None
        for image in images:
            try:
                blocks.append(spec("image", image))
            except BlockShapeError:
                blocks.append(spec("image", {"url": str(image.get("url", ""))}))
        if meta:
            _apply_meta(blocks[0], meta)
        return blocks

    def list(self, index: int) -> tuple[list[BlockSpec], int]:
        tokens = self.tokens
        ordered = tokens[index].type == "ordered_list_open"
        close = "ordered_list_close" if ordered else "bullet_list_close"
        index += 1
        items: list[BlockSpec] = []
        while index < len(tokens) and tokens[index].type != close:
            if tokens[index].type != "list_item_open":
                index += 1
                continue
            item_token = tokens[index]
            first = tokens[index + 1] if index + 1 < len(tokens) else None
            own_line = (
                first is not None and first.type == "paragraph_open"
                and first.map and item_token.map and first.map[0] == item_token.map[0]
            )
            nodes: list[dict] = []
            meta = None
            images: list[dict] = []
            checked: bool | None = None
            if own_line:
                inline = tokens[index + 2]
                nodes, images, meta = self.inline(inline)
                nodes, checked = _task_marker(nodes)
                index += 4
            else:
                index += 1
            children, index = self.sequence(index, "list_item_close")
            children = [spec("image", image) for image in images] + children
            if checked is not None:
                item = spec("check_item", {"checked": checked}, nodes, children=children)
            else:
                item = spec("numbered_item" if ordered else "bullet_item", content=nodes,
                            children=children)
            items.append(_apply_meta(item, meta))
        return items, index + 1

    def html_block(self, source: str) -> list[BlockSpec]:
        nodes = [node for node in _parse_html(source) if not (isinstance(node, str) and not node.strip())]
        if len(nodes) == 1 and isinstance(nodes[0], _Element) and (
            nodes[0].tag in ("table", "img") or nodes[0].attrs.get("data-type")
        ):
            try:
                parsed = _blocks_from_element(nodes[0], 0)
            except (BlockShapeError, _Unsupported, ValueError):
                parsed = None
            if parsed is not None:
                return parsed
        return [spec("html", payload={"html": source.strip()})]

    def gfm_table(self, index: int) -> tuple[list[BlockSpec], int]:
        tokens = self.tokens
        rows: list[BlockSpec] = []
        header_rows = 0
        row: list[BlockSpec] | None = None
        in_head = False
        index += 1
        while index < len(tokens) and tokens[index].type != "table_close":
            token = tokens[index]
            if token.type == "thead_open":
                in_head = True
            elif token.type == "thead_close":
                in_head = False
            elif token.type == "tr_open":
                row = []
            elif token.type == "tr_close" and row is not None:
                rows.append(spec("table_row", children=row))
                header_rows += 1 if in_head else 0
                row = None
            elif token.type in ("th_open", "td_open") and row is not None:
                style = str(token.attrGet("style") or "")
                align = style.partition("text-align:")[2].strip() or None
                inline = tokens[index + 1] if tokens[index + 1].type == "inline" else None
                nodes, images = _nodes_from_inline_token(inline) if inline else ([], [])
                content = [spec("paragraph", content=nodes)] if nodes else []
                content += [spec("image", image) for image in images]
                row.append(spec("table_cell", {"align": align}, children=content))
            index += 1
        return [spec("table", {"header_rows": header_rows}, children=rows)], index + 1


def _copy_token(token: Token, content: str) -> Token:
    copy = Token(token.type, token.tag, token.nesting)
    copy.content = content
    return copy


_MATH_RE = re.compile(r"^\$\$\s*(.*?)\s*\$\$$", re.DOTALL)


def _math_source(source: str) -> str | None:
    match = _MATH_RE.match(source.strip())
    return match.group(1) if match else None


def _task_marker(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool | None]:
    if nodes and nodes[0]["type"] == "text":
        match = re.match(r"^\[([ xX])\](?:\s+|$)", nodes[0]["text"])
        if match:
            rest = nodes[0]["text"][match.end():]
            head = [{**nodes[0], "text": rest}] if rest else []
            return schema.merge_inline(head + nodes[1:]), match.group(1) != " "
    return nodes, None


def _marker_meta(value: dict[str, Any]) -> dict[str, Any]:
    """Metadata of a columns/column marker (older versions wrote plain attrs)."""

    if "id" in value or "attrs" in value or "provenance" in value:
        return parse_comment(comment(value)) or {}
    return {"attrs": value}


def _apply_table_ids(table: BlockSpec, rows_meta: list) -> None:
    if len(rows_meta) != len(table.children):
        return
    for row, row_meta in zip(table.children, rows_meta):
        cells = row_meta.get("cells") if isinstance(row_meta, dict) else None
        if not isinstance(cells, list) or len(cells) != len(row.children):
            return
    for row, row_meta in zip(table.children, rows_meta):
        _apply_meta(row, {"id": row_meta.get("id")} if isinstance(row_meta.get("id"), str) else None)
        for cell, cell_meta in zip(row.children, row_meta["cells"]):
            if not isinstance(cell_meta, dict):
                continue
            if isinstance(cell_meta.get("id"), str):
                cell.id = cell_meta["id"]
            block_meta = cell_meta.get("block")
            if isinstance(block_meta, dict) and cell.children and cell.children[0].type == "paragraph":
                _apply_meta(cell.children[0], parse_comment(comment(block_meta)))


def _fold_layout(items: list[BlockSpec | _Marker]) -> list[BlockSpec]:
    """Turn ``fortress:columns`` markers and what follows them into columns blocks."""

    result: list[BlockSpec] = []
    open_layouts: list[list] = []  # [columns block, current column or None]

    def target() -> list[BlockSpec]:
        if not open_layouts:
            return result
        layout = open_layouts[-1]
        if layout[1] is None:
            layout[1] = spec("column")
            layout[0].children.append(layout[1])
        return layout[1].children

    for item in items:
        if isinstance(item, BlockSpec):
            target().append(item)
        elif item.kind == "columns":
            columns = _apply_meta(spec("columns"), item.meta)
            target().append(columns)
            open_layouts.append([columns, None])
        elif item.kind == "column" and open_layouts:
            column = _apply_meta(spec("column"), item.meta)
            open_layouts[-1][0].children.append(column)
            open_layouts[-1][1] = column
        elif item.kind == "/columns" and open_layouts:
            open_layouts.pop()
    return result


def _attach_parents(blocks: list[BlockSpec]) -> list[BlockSpec]:
    """Blocks whose comment names a parent written just before them become its children."""

    result: list[BlockSpec] = []
    by_id: dict[str, BlockSpec] = {}

    def remember(block: BlockSpec) -> None:
        if block.id:
            by_id[block.id] = block
        for child in block.children:
            remember(child)

    for block in blocks:
        parent = by_id.get(block.parent_ref) if block.parent_ref else None
        if parent is not None:
            parent.children.append(block)
        else:
            result.append(block)
        remember(block)
    return result


class _Unsupported(Exception):
    """HTML that has no block form."""


_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "hr",
               "table", "img", "div"}


def _element_meta(element: _Element) -> dict[str, Any] | None:
    raw = element.attrs.get("data-block")
    if not raw:
        return None
    value = _parse_json(raw)
    return parse_comment(comment(value)) if value else None


def _children(element: _Element, *tags: str) -> list[_Element]:
    return [child for child in element.children if isinstance(child, _Element) and child.tag in tags]


def _all_text(element: _Element) -> str:
    return "".join(child if isinstance(child, str) else _all_text(child) for child in element.children)


def _blocks_from_nodes(nodes: list[_Element | str], depth: int) -> list[BlockSpec]:
    blocks: list[BlockSpec] = []
    loose: list[_Element | str] = []

    def flush() -> None:
        content, images = _nodes_from_html(loose, collapse=True)
        loose.clear()
        if content:
            blocks.append(spec("paragraph", content=content))
        blocks.extend(spec("image", image) for image in images)

    for node in nodes:
        if not (isinstance(node, _Element) and node.tag in _BLOCK_TAGS):
            loose.append(node)
            continue
        flush()
        if node.tag == "div" and "data-children" in node.attrs and blocks:
            blocks[-1].children.extend(_blocks_from_nodes(node.children, depth + 1))
            continue
        blocks.extend(_blocks_from_element(node, depth))
    flush()
    return blocks


def _blocks_from_element(element: _Element, depth: int) -> list[BlockSpec]:
    if depth > 16:
        raise _Unsupported("too deep")
    tag = element.tag
    meta = _element_meta(element)
    if tag == "p":
        content, images = _nodes_from_html(element.children, collapse=True)
        blocks = [_apply_meta(spec("paragraph", content=content), meta)]
        return blocks + [spec("image", image) for image in images]
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        content, images = _nodes_from_html(element.children, collapse=True)
        blocks = [_apply_meta(spec("heading", {"level": int(tag[1])}, content), meta)]
        return blocks + [spec("image", image) for image in images]
    if tag in {"ul", "ol"}:
        items = []
        for item in _children(element, "li"):
            item_meta = _element_meta(item)
            leading: list[_Element | str] = []
            rest = list(item.children)
            while rest and not (isinstance(rest[0], _Element) and rest[0].tag in _BLOCK_TAGS):
                leading.append(rest.pop(0))
            content, images = _nodes_from_html(leading, collapse=True)
            children = [spec("image", image) for image in images] + _blocks_from_nodes(rest, depth + 1)
            if item.attrs.get("data-checked") in {"true", "false"}:
                block = spec("check_item", {"checked": item.attrs["data-checked"] == "true"},
                             content, children=children)
            else:
                block = spec("numbered_item" if tag == "ol" else "bullet_item", content=content,
                             children=children)
            items.append(_apply_meta(block, item_meta))
        return items
    if tag == "blockquote":
        kind = "callout" if element.attrs.get("data-type") == "callout" else "quote"
        return [_apply_meta(spec(kind, children=_blocks_from_nodes(element.children, depth + 1)), meta)]
    if tag == "pre":
        codes = _children(element, "code")
        code = codes[0] if codes else element
        language = None
        for name in code.attrs.get("class", "").split():
            if name.startswith("language-"):
                language = name[len("language-"):]
        text = _all_text(code)
        return [_apply_meta(spec("code", {"language": language}, [text_node(text)] if text else []), meta)]
    if tag == "hr":
        return [_apply_meta(spec("divider"), meta)]
    if tag == "table":
        return [table_from_element(element, depth)]
    if tag == "img":
        return [_apply_meta(spec("image", image_attrs_from_element(element)), meta)]
    if tag == "div" and element.attrs.get("data-type") == "file":
        file_meta = meta or {}
        if not file_meta.get("attrs"):
            legacy = _parse_json(element.attrs.get("data-attrs")) or {}
            file_meta = {**file_meta, "attrs": legacy}
        attrs = file_meta.get("attrs") or {}
        return [_apply_meta(spec("file", attrs), {k: v for k, v in file_meta.items() if k != "attrs"})]
    if tag == "div" and element.attrs.get("data-type") == "math":
        return [_apply_meta(spec("math", payload={"latex": _all_text(element)}), meta)]
    if tag == "div" and element.attrs.get("data-type") == "html":
        return [_apply_meta(spec("html", payload={"html": element.attrs.get("data-html", "")}), meta)]
    if tag == "div":
        return _blocks_from_nodes(element.children, depth + 1)
    raise _Unsupported(tag)


def table_from_element(element: _Element, depth: int) -> BlockSpec:
    meta = _element_meta(element) or {}
    attrs: dict[str, Any] = dict(meta.get("attrs") or {})
    legacy = _parse_json(element.attrs.get("data-attrs"))
    if legacy:
        attrs.update(legacy)
    if element.attrs.get("data-borders") == "false" or element.attrs.get("border") == "0":
        attrs["borders"] = False
    widths: list[Any] = []
    for group in _children(element, "colgroup"):
        for col in _children(group, "col"):
            widths.append(_number(_style_map(col).get("width") or col.attrs.get("width")))
    row_elements: list[_Element] = []
    for child in _children(element, "tr", "thead", "tbody", "tfoot"):
        row_elements.extend([child] if child.tag == "tr" else _children(child, "tr"))
    header_rows = 0
    counting = True
    rows: list[BlockSpec] = []
    for row_element in row_elements:
        cell_elements = _children(row_element, "td", "th")
        if counting and cell_elements and all(cell.tag == "th" for cell in cell_elements):
            header_rows += 1
        else:
            counting = False
        cells = [_cell_from_element(cell, depth) for cell in cell_elements]
        rows.append(_apply_meta(spec("table_row", children=cells), _element_meta(row_element)))
    if header_rows:
        attrs["header_rows"] = header_rows
    header_cols = _number(element.attrs.get("data-header-cols"))
    if header_cols:
        attrs["header_cols"] = int(header_cols)
    if any(width is not None for width in widths):
        attrs["column_widths"] = widths
    table = spec("table", attrs, children=rows)
    return _apply_meta(table, {k: v for k, v in meta.items() if k != "attrs"})


def _cell_from_element(cell: _Element, depth: int) -> BlockSpec:
    css = _style_map(cell)
    meta = _element_meta(cell) or {}
    attrs: dict[str, Any] = dict(meta.get("attrs") or {})
    background = _safe_color(css.get("background-color") or css.get("background") or cell.attrs.get("bgcolor"))
    if background:
        attrs["background"] = background
    color = _safe_color(css.get("color"))
    if color:
        attrs["color"] = color
    align = (css.get("text-align") or cell.attrs.get("align") or "").lower()
    if align in ("left", "center", "right", "justify"):
        attrs["align"] = align
    for key in ("colspan", "rowspan"):
        number = _number(cell.attrs.get(key))
        if number and int(number) > 1:
            attrs[key] = int(number)
    children = _blocks_from_nodes(cell.children, depth + 1)
    block = spec("table_cell", attrs, children=children)
    return _apply_meta(block, {k: v for k, v in meta.items() if k != "attrs"})
