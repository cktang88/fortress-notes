"""Markdown in and out of canonical blocks (docs/block-model.md §7, docs/open-standards.md).

Markdown is never stored: :func:`render_markdown` writes it from canonical
blocks (exports, the vault mirror), and :func:`parse_markdown` reads it straight
into canonical :class:`BlockSpec` trees (imports, capture, importer sources).

What is written is CommonMark + GFM + Obsidian Flavored Markdown: wikilinks for
note links (``[[Note]]``), ``[[Note#^id]]`` / ``[[Note#Heading]]`` for block
references, ``^id`` block identifiers on the blocks something refers to,
``> [!type]`` callouts, ``==highlight==``, task lists, GFM tables.
Only what no standard can say (extension attributes, nesting under
a paragraph, column layouts) goes in an invisible ``<!-- fortress:block {...} -->``
comment. Reading accepts all of that, plain Obsidian notes, and every older
Fortress form.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import re
from collections.abc import Callable, Collection
from contextvars import ContextVar
from dataclasses import dataclass, field
from html import escape as _html_escape
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import Delimiter, StateInline
from markdown_it.token import Token
from ulid import ULID

from . import block_schema as schema
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
    # Parsing only: the id of the block a Markdown comment says this one belongs under.
    parent_ref: str | None = field(default=None, repr=False, compare=False)
    # Parsing only: how deep a comment says this block is nested under the text block before it.
    indent: int = field(default=0, repr=False, compare=False)

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

    Older versions also wrote where a block came from (``provenance``, or the
    whole comment); that is ignored.
    """

    match = _COMMENT_RE.match(source.strip())
    if not match:
        return None
    value = _parse_json(match.group(1))
    if value is None:
        return None
    meta: dict[str, Any] = {}
    if isinstance(value.get("id"), str):
        meta["id"] = value["id"]
    for key in ("attrs", "rows"):
        if isinstance(value.get(key), (dict, list)):
            meta[key] = value[key]
    for key in ("type", "parent"):
        if isinstance(value.get(key), str):
            meta[key] = value[key]
    indent = value.get("indent")
    if isinstance(indent, int) and not isinstance(indent, bool) and 0 < indent <= 64:
        meta["indent"] = indent
    return meta


# --- Ids, wikilinks and stored files ---------------------------------------------------------

# Ids Fortress makes (ULID) or clients send (UUID): kept as they are when read back.
_OWN_ID_RE = re.compile(
    r"^(?:[0-7][0-9A-HJKMNP-TV-Z]{25}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
# What Obsidian accepts as a block identifier (``^id``): letters, digits and dashes.
_ANCHOR_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_LOOSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# A stored file is /media/<name> in the model and <notes>/assets/<name> on disk.
MEDIA_PREFIX = "/media/"
_ASSET_NAME_RE = re.compile(r"^(?:[0-7][0-9A-HJKMNP-TV-Z]{25}|[0-9a-f]{64})\.[A-Za-z0-9]{1,10}$")
_ASSET_PATH_RE = re.compile(r"^(?:\./)?(?:\.\./)*assets/([A-Za-z0-9_-]+\.[A-Za-z0-9]{1,10})$")
IMAGE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "heic", "heif", "tif", "tiff", "ico"}
)
# Files Obsidian shows in place when embedded with ``![...](...)``.
_EMBED_EXTENSIONS = frozenset(
    {"pdf", "mp3", "wav", "ogg", "m4a", "flac", "webm", "mp4", "mov", "ogv", "mkv"}
)


def new_block_id() -> str:
    return str(ULID())


def media_url(url: str) -> str:
    """The model's URL (``/media/<name>``) for a vault-relative path to a stored file."""

    match = _ASSET_PATH_RE.match(url)
    if match:
        return MEDIA_PREFIX + match.group(1)
    if _ASSET_NAME_RE.match(url):
        return MEDIA_PREFIX + url
    return url


def _extension(url: str) -> str:
    base = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def heading_fragment(text: str) -> str:
    """A heading's name in ``[[Note#Heading]]`` (Obsidian leaves these characters out)."""

    return " ".join(re.sub(r"[#^|:\[\]%]", " ", text.split("\n", 1)[0]).split())


def heading_key(text: str) -> str:
    return heading_fragment(text).casefold()


def _wikilink_label(text: object) -> str:
    label = " ".join(re.sub(r"[\[\]|]", " ", str(text).split("\n", 1)[0]).split())
    if len(label) > 80:
        label = label[:79].rstrip() + "…"
    return label


class ParseLinks:
    """How the wikilinks and ``^ids`` of one parse map to Fortress ids.

    Without context (capture, one uploaded file) a link whose target is itself
    an id is a note link, ``^ids`` Fortress made are kept, and any other ``^id``
    (Obsidian makes short ones, unique only within their note) gets a new block
    id, the same for every mention in this parse. ``vault.ImportLinks`` resolves
    note names and headings across a whole import.
    """

    def __init__(self, document_id: str | None = None) -> None:
        self.document_id = document_id
        self._carets: dict[tuple[str | None, str], str] = {}

    def note(self, name: str) -> str | None:
        """The id of the note a wikilink names (None: unknown)."""

        return name if _LOOSE_ID_RE.match(name) else None

    def block(self, document_id: str | None, caret: str) -> str:
        """The block id for ``^caret`` in a note (None: the note being read)."""

        if _OWN_ID_RE.match(caret) or "_" in caret:
            return caret
        key = (document_id or self.document_id, caret)
        if key not in self._carets:
            self._carets[key] = new_block_id()
        return self._carets[key]

    def heading(self, document_id: str | None, text: str) -> str | None:
        """The block id of the heading ``[[Note#text]]`` names (None: unknown)."""

        return None

    def own_heading(self, text: str, block_id: str | None) -> str | None:
        """The id for a heading of the note being read (``block_id``: its own, if written)."""

        return block_id


class _CheckLinks(ParseLinks):
    """Reads back what the renderer just wrote, to check it: every wikilink is a link."""

    def note(self, name: str) -> str | None:
        return name or "note"

    def block(self, document_id: str | None, caret: str) -> str:
        return caret if _LOOSE_ID_RE.match(caret) else "block"

    def heading(self, document_id: str | None, text: str) -> str | None:
        return "heading"


@dataclass
class RenderLinks:
    """What rendering needs beyond the blocks themselves (the vault mirror, exports).

    ``anchors``: ids of the blocks to identify (``^id``; something refers to
    them); None identifies every block. ``notes``: note id -> (wikilink target,
    title), None when unknown. ``blocks``: block id -> ``{"document_id", "type",
    "text"}``. ``media_prefix`` replaces ``/media/`` in file URLs (``../assets/``).
    """

    document_id: str | None = None
    anchors: Collection[str] | None = None
    notes: Callable[[str], tuple[str, str] | None] | None = None
    blocks: Callable[[str], dict[str, Any] | None] | None = None
    media_prefix: str | None = None

    def media(self, url: str) -> str:
        if self.media_prefix is not None and url.startswith(MEDIA_PREFIX):
            return self.media_prefix + url[len(MEDIA_PREFIX):]
        return url

    def ref(self, node: dict[str, Any], table: bool = False) -> str:
        pipe = "\\|" if table else "|"
        if node["type"] == "docLink":
            found = self.notes(node["id"]) if self.notes else None
            if not found:
                return f"[[{node['id']}]]"
            target, title = found
            label = _wikilink_label(title)
            return f"[[{target}]]" if not label or label == target else f"[[{target}{pipe}{label}]]"
        info = self.blocks(node["id"]) if self.blocks else None
        if not info:
            return f"[[#^{node['id']}]]"
        target = ""
        document_id = info.get("document_id")
        if document_id and document_id != self.document_id:
            found = self.notes(document_id) if self.notes else None
            target = found[0] if found else str(document_id)
        text = str(info.get("text") or "")
        fragment = heading_fragment(text) if info.get("type") == "heading" else ""
        fragment = f"#{fragment}" if fragment else f"#^{node['id']}"
        label = _wikilink_label(text)
        return f"[[{target}{fragment}{pipe}{label}]]" if label else f"[[{target}{fragment}]]"


_DEFAULT_LINKS = RenderLinks()


# --- Inline content to Markdown ---------------------------------------------------------------

# Outermost first: the order marks open in (and close in reverse).
_MARK_ORDER = ("highlight", "color", "underline", "sup", "sub", "bold", "italic", "strike")
_MD_MARKS = {"bold": ("**", "**"), "italic": ("*", "*"), "strike": ("~~", "~~")}
_HTML_MARKS = {
    "bold": ("<strong>", "</strong>"), "italic": ("<em>", "</em>"), "strike": ("<s>", "</s>"),
    "underline": ("<u>", "</u>"), "sup": ("<sup>", "</sup>"), "sub": ("<sub>", "</sub>"),
}
# ``==text==`` (Obsidian) is a highlight in this colour; others are <mark style>.
DEFAULT_HIGHLIGHT = "yellow"
_ENTITY_RE = re.compile(r"&#?[A-Za-z0-9]+;")
_ORDERED_START_RE = re.compile(r"\d{1,9}(?=[.)])")


def _delimited(name: str, value: Any) -> bool:
    """Whether a mark is written with Markdown delimiters (not an HTML tag)."""

    return name in _MD_MARKS or (name == "highlight" and value == DEFAULT_HIGHLIGHT)


def _mark_tags(name: str, value: Any, html: bool) -> tuple[str, str]:
    if name == "color":
        return f'<span style="color:{value}">', "</span>"
    if name == "highlight":
        if not html and value == DEFAULT_HIGHLIGHT:
            return "==", "=="
        return f'<mark style="background:{value}">', "</mark>"
    if not html and name in _MD_MARKS:
        return _MD_MARKS[name]
    return _HTML_MARKS[name]


def _escape_markdown(text: str, at_line_start: bool, table: bool) -> str:
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        following = text[index + 1:index + 2]
        if at_line_start and char not in " \t":
            at_line_start = False
            digits = _ORDERED_START_RE.match(text, index)
            if digits:
                # "1. x" would start a list: escape the dot.
                end = digits.end()
                out.append(text[index:end] + "\\" + text[end])
                index = end + 1
                continue
            if char in "#>-+=" or (char == "$" and following == "$"):
                out.append("\\" + char)
                index += 1
                continue
        if char == "\n":
            out.append(char)
            at_line_start = True
        elif char in "\\`*_~<[]":
            out.append("\\" + char)
        elif char == "(" and following == "(":
            out.append("\\(")
        elif char in "=%" and following == char:
            # ==highlight== and %%comments%% (Obsidian)
            out.append("\\" + char)
        elif char == "^" and (index == 0 or text[index - 1] in " \t\n"):
            # " ^word" at the end of a line is an Obsidian block id
            out.append("\\^")
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
        if node["type"] != "text" or not any(_delimited(m, v) for m, v in marks.items()):
            result.append(node)
            continue
        text = node["text"]
        core = text.strip(" ")
        if not core:
            result.append(node)
            continue
        plain = {k: v for k, v in marks.items() if not _delimited(k, v) and k != "code"}
        lead = text[: len(text) - len(text.lstrip(" "))]
        trail = text[len(text.rstrip(" ")):]
        if lead:
            result.append(text_node(lead, plain))
        result.append(text_node(core, marks))
        if trail:
            result.append(text_node(trail, plain))
    return schema.merge_inline(result)


def _render_inline(nodes: list[dict[str, Any]], *, html: bool, newline: str, table: bool,
                   links: RenderLinks) -> str:
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
                out.append(links.ref(node, table))
                state["line_start"] = False
                continue
            if kind == "link":
                close_to(0)
                href = links.media(node["href"])
                if html:
                    out.append(f'<a href="{_attr(href)}">')
                    emit(node["content"])
                    close_to(0)
                    out.append("</a>")
                else:
                    out.append("[")
                    emit(node["content"])
                    close_to(0)
                    out.append("](" + _link_destination(href) + ")")
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
    flat = []
    for node in nodes:
        if node["type"] == "link":
            flat.append({**node, "href": _INLINE_MD.normalizeLink(media_url(node["href"]))})
        elif node["type"] in ("docLink", "blockRef"):
            flat.append({"type": "ref"})
        else:
            flat.append(node)
    return schema.merge_inline(flat)


def inline_to_markdown(nodes: list[dict[str, Any]], *, newline: str = "\\\n",
                       table: bool = False, links: RenderLinks | None = None) -> str:
    """Markdown for inline nodes: native syntax where CommonMark/GFM/Obsidian have it, HTML otherwise.

    If the native form would not read back as the same nodes (emphasis next to
    punctuation, ...), every mark is written as an HTML tag instead.
    """

    links = links or _DEFAULT_LINKS
    nodes = _split_whitespace(nodes)
    check_newline = "<br>" if table else "\\\n"
    native = _render_inline(nodes, html=False, newline=check_newline, table=table, links=links)
    parsed, images = inline_from_markdown(native, links=_CheckLinks())
    if not images and _comparable(parsed) == _comparable(nodes):
        if newline == check_newline:
            return native
        return _render_inline(nodes, html=False, newline=newline, table=table, links=links)
    # Marks as HTML tags, text still Markdown-escaped.
    return _render_markdown_html_marks(nodes, newline="<br>" if table else newline, table=table,
                                       links=links)


def _render_markdown_html_marks(nodes: list[dict[str, Any]], *, newline: str, table: bool,
                                links: RenderLinks) -> str:
    out: list[str] = []
    for node in nodes:
        if node["type"] in ("docLink", "blockRef"):
            out.append(links.ref(node, table))
        elif node["type"] == "link":
            inner = _render_markdown_html_marks(node["content"], newline=newline, table=table, links=links)
            out.append(f"[{inner}]({_link_destination(links.media(node['href']))})")
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


def inline_to_html(nodes: list[dict[str, Any]], links: RenderLinks | None = None) -> str:
    """HTML for inline nodes (inside HTML tables)."""

    return _render_inline(nodes, html=True, newline="<br>", table=False, links=links or _DEFAULT_LINKS)


# --- Markdown/HTML to inline content ------------------------------------------------------------

# [[target#fragment|label]] and ![[embed]] (Obsidian); ((id)) from older Fortress versions.
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]|\n]*)(?:\|([^\[\]\n]*))?\]\]")
_LEGACY_REF_RE = re.compile(
    r"\(\(\s*([A-Za-z0-9_-]{1,128})(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)\)"
)
_SIZE_RE = re.compile(r"^\s*(\d{1,5})(?:\s*x\s*(\d{1,5}))?\s*$")


def _resolve_wikilink(links: ParseLinks, embed: bool, target: str, label: str | None) -> dict | None:
    """What a wikilink reads as (None: it stays text)."""

    name, _, fragment = target.partition("#")
    name, fragment = name.strip(), fragment.strip()
    base = name.rsplit("/", 1)[-1]
    if embed and "." in base and not base.lower().endswith(".md"):
        attrs: dict[str, Any] = {"url": media_url(name)}
        size = _SIZE_RE.match(label or "")
        if size:
            attrs["width"] = int(size.group(1))
            if size.group(2):
                attrs["height"] = int(size.group(2))
        elif label:
            attrs["alt"] = label
        return {"kind": "image", "attrs": attrs}
    if base.lower().endswith(".md"):
        name = name[:-3]
    document_id = links.note(name) if name else links.document_id
    if fragment:
        last = fragment.split("#")[-1].strip()
        if last.startswith("^"):
            caret = last[1:].strip()
            if _LOOSE_ID_RE.match(caret) and (
                document_id is not None or not name or _OWN_ID_RE.match(caret)
            ):
                return {"kind": "blockRef", "id": links.block(document_id, caret)}
        elif last and (document_id is not None or not name):
            heading_id = links.heading(document_id, last)
            if heading_id:
                return {"kind": "blockRef", "id": heading_id}
    if name and document_id is not None:
        return {"kind": "docLink", "id": document_id}
    return None


def _link_children(tokens: list[Token], links: ParseLinks) -> list[Token]:
    """Wikilinks in runs of unescaped text become ``fortress_ref`` tokens."""

    result: list[Token] = []
    run: list[Token] = []

    def flush() -> None:
        source = "".join(token.content for token in run)
        if "[[" not in source and "((" not in source:
            result.extend(run)
            run.clear()
            return
        escaped: set[int] = set()
        offset = 0
        for token in run:
            if token.type == "text_special":
                escaped.update(range(offset, offset + len(token.content)))
            offset += len(token.content)
        pieces: list[tuple[int, int, dict]] = []
        for match in _WIKILINK_RE.finditer(source):
            opening = match.start() + len(match.group(1))
            if escaped & {opening, opening + 1, match.end() - 2, match.end() - 1}:
                continue
            embed = bool(match.group(1)) and match.start() not in escaped
            meta = _resolve_wikilink(links, embed, match.group(2), match.group(3))
            if meta is not None:
                pieces.append((match.start() if embed else opening, match.end(), meta))
        for match in _LEGACY_REF_RE.finditer(source):
            if escaped & {match.start(), match.start() + 1}:
                continue
            if any(start < match.end() and match.start() < end for start, end, _ in pieces):
                continue
            pieces.append((match.start(), match.end(), {"kind": "blockRef", "id": match.group(1)}))
        if not pieces:
            result.extend(run)
            run.clear()
            return
        last = 0
        for start, end, meta in sorted(pieces, key=lambda piece: piece[0]):
            if start < last:
                continue
            if start > last:
                piece = Token("text", "", 0)
                piece.content = source[last:start]
                result.append(piece)
            ref = Token("fortress_ref", "", 0)
            ref.meta = meta
            result.append(ref)
            last = end
        if last < len(source):
            piece = Token("text", "", 0)
            piece.content = source[last:]
            result.append(piece)
        run.clear()

    for token in tokens:
        if token.type in ("text", "text_special"):
            run.append(token)
            continue
        flush()
        result.append(token)
    flush()
    return result


def _ref_tokens(state) -> None:
    """Core rule: wikilinks (and older ``((id))`` references) become reference tokens."""

    links = state.env.get("links") if isinstance(state.env, dict) else None
    if links is None:
        links = ParseLinks()
        if isinstance(state.env, dict):
            state.env["links"] = links
    for block_token in state.tokens:
        if block_token.type == "inline" and block_token.children:
            block_token.children = _link_children(block_token.children, links)


def _render_ref(self, tokens, index, options, env) -> str:
    meta = tokens[index].meta
    if meta["kind"] == "image":
        attrs = meta["attrs"]
        parts = [f'<img src="{_attr(attrs["url"])}"']
        for key in ("alt", "width", "height"):
            if attrs.get(key) is not None:
                parts.append(f'{key}="{_attr(attrs[key])}"')
        return " ".join(parts) + ">"
    return f'<fortress-ref data-kind="{meta["kind"]}" data-id="{_attr(meta["id"])}"></fortress-ref>'


def _render_softbreak(self, tokens, index, options, env) -> str:
    # A note keeps the lines its author typed (as Obsidian and Bear show them),
    # so a soft break is a line break, not CommonMark's space.
    return "<br>"


def _mark_tokenize(state: StateInline, silent: bool) -> bool:
    """``==highlight==`` (Obsidian; the delimiter rule GFM uses for ``~~``)."""

    start = state.pos
    if silent or state.src[start] != "=":
        return False
    scanned = state.scanDelims(start, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        token = state.push("text", "", 0)
        token.content = "="
        length -= 1
    for _ in range(length // 2):
        token = state.push("text", "", 0)
        token.content = "=="
        state.delimiters.append(Delimiter(
            marker=0x3D, length=0, token=len(state.tokens) - 1, end=-1,
            open=scanned.can_open, close=scanned.can_close,
        ))
    state.pos += scanned.length
    return True


def _mark_pairs(state: StateInline, delimiters: list[Delimiter]) -> None:
    lone: list[int] = []
    for opener in delimiters:
        if opener.marker != 0x3D or opener.end == -1:
            continue
        closer = delimiters[opener.end]
        for index, kind, nesting in ((opener.token, "mark_open", 1), (closer.token, "mark_close", -1)):
            token = state.tokens[index]
            token.type, token.tag, token.nesting, token.markup, token.content = kind, "mark", nesting, "==", ""
        before = state.tokens[closer.token - 1]
        if before.type == "text" and before.content == "=":
            lone.append(closer.token - 1)
    while lone:
        index = lone.pop()
        end = index + 1
        while end < len(state.tokens) and state.tokens[end].type == "mark_close":
            end += 1
        end -= 1
        if index != end:
            state.tokens[index], state.tokens[end] = state.tokens[end], state.tokens[index]


def _mark_post_process(state: StateInline) -> None:
    _mark_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _mark_pairs(state, meta["delimiters"])


def _markdown(*plugins: str) -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(list(plugins)) if plugins else MarkdownIt("commonmark")
    md.inline.ruler.before("emphasis", "fortress_mark", _mark_tokenize)
    md.inline.ruler2.before("emphasis", "fortress_mark", _mark_post_process)
    md.core.ruler.before("text_join", "fortress_refs", _ref_tokens)
    md.add_render_rule("fortress_ref", _render_ref)
    md.add_render_rule("softbreak", _render_softbreak)
    return md


_INLINE_MD = _markdown("strikethrough")
_BLOCK_MD = _markdown("table", "strikethrough")


def inline_from_markdown(markdown: str, links: ParseLinks | None = None
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(inline nodes, image attrs)`` for one block's inline Markdown.

    Images can't be inline content: they are returned separately (the caller
    makes them image blocks after the text).
    """

    tokens = _INLINE_MD.parseInline(markdown, {"links": links or ParseLinks()})
    return _nodes_from_inline_token(tokens[0] if tokens else None)


def _nodes_from_inline_token(token: Token | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if token is None or not token.children:
        return [], []
    html = _BLOCK_MD.renderer.render(token.children, _BLOCK_MD.options, {})
    return _nodes_from_html(_parse_html(html), collapse=False)


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
                href = media_url(item.attrs.get("href", "").strip())
                inner: list[dict[str, Any]] = []
                walk(item.children, marks, inner, True)
                inner = schema.merge_inline(inner)
                if schema.safe_href(href) and inner:
                    target.append({"type": "link", "href": href, "content": inner})
                else:
                    target.extend(inner)
            else:
                # Marks; any other inline tag keeps its text.
                walk(item.children, _with_marks(marks, _element_marks(item)), target, in_link)

    walk(elements, {}, nodes, False)
    nodes = schema.merge_inline(nodes)
    if collapse:
        nodes = _wikilinks_in_text(nodes)
    # Paragraph edges carry no spaces in Markdown or HTML; an image lifted out
    # of the text must not leave one behind either.
    if (collapse or images) and nodes:
        if nodes[0]["type"] == "text":
            nodes[0] = {**nodes[0], "text": nodes[0]["text"].lstrip(" ")}
        if nodes[-1]["type"] == "text":
            nodes[-1] = {**nodes[-1], "text": nodes[-1]["text"].rstrip(" ")}
        nodes = schema.merge_inline(nodes)
    return nodes, images


def _wikilinks_in_text(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wikilinks written as text inside HTML (table cells) become links."""

    links = _PARSE_LINKS.get() or ParseLinks()
    result: list[dict[str, Any]] = []
    for node in nodes:
        marks = node.get("marks") or {}
        if node["type"] != "text" or marks.get("code") or "[[" not in node["text"]:
            result.append(node)
            continue
        text, last = node["text"], 0
        for match in _WIKILINK_RE.finditer(text):
            meta = _resolve_wikilink(links, False, match.group(2), match.group(3))
            if meta is None or match.group(1):
                continue
            if match.start() > last:
                result.append(text_node(text[last:match.start()], marks))
            result.append({"type": meta["kind"], "id": meta["id"]})
            last = match.end()
        if last < len(text):
            result.append(text_node(text[last:], marks))
    return schema.merge_inline(result)


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


def image_attrs_from_element(element: _Element) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    legacy = _parse_json(element.attrs.get("data-attrs"))
    if legacy:
        attrs.update(legacy)
    if "previewWidth" in attrs:
        attrs.setdefault("width", attrs.pop("previewWidth"))
    attrs["url"] = media_url(element.attrs.get("src", ""))
    for html_name, key in (("alt", "alt"), ("title", "caption"), ("data-name", "name")):
        if element.attrs.get(html_name):
            attrs[key] = element.attrs[html_name]
    # Obsidian's ![alt|300](...) / ![alt|300x200](...) sizes
    alt, bar, size = str(attrs.get("alt") or "").rpartition("|")
    sized = _SIZE_RE.match(size) if bar else None
    if sized:
        if alt:
            attrs["alt"] = alt
        else:
            attrs.pop("alt", None)
        attrs["width"] = int(sized.group(1))
        if sized.group(2):
            attrs["height"] = int(sized.group(2))
    for key in ("width", "height"):
        number = _number(element.attrs.get(key))
        if number is not None:
            attrs[key] = number
    return attrs


# --- Rendering --------------------------------------------------------------------------------------

# Attributes the Markdown syntax of a type already says (the rest go in its comment).
_NATIVE_ATTRS = {
    "heading": {"level"}, "check_item": {"checked"}, "code": {"language"},
    "callout": {"callout_type", "callout_fold", "callout_title"},
}
_LIST_TYPES = ("bullet_item", "numbered_item", "check_item")
_CALLOUT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")


def _ordinal(node: dict[str, Any], previous: int) -> int:
    """A numbered item's number: its ``list_start`` extension attribute (an imported
    list restarting at n, e.g. OneNote), else one more than the item before."""

    start = (node.get("attrs") or {}).get("list_start")
    if isinstance(start, int) and not isinstance(start, bool) and 0 <= start < 10**9:
        return start
    return previous + 1


_INLINE_COMMENT_TYPES = frozenset({"paragraph", "heading", *_LIST_TYPES})


def _whole(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 \
        and float(value).is_integer()


def _basename(url: str) -> str:
    return posixpath.basename(url.split("?", 1)[0].split("#", 1)[0])


def _image_extra(attrs: dict[str, Any]) -> dict[str, Any]:
    """An image's attributes ``![alt|WxH](url "caption")`` can't say."""

    extra = {k: v for k, v in attrs.items() if k not in ("url", "alt", "caption", "width", "height")}
    for key in ("alt", "caption"):
        if "\n" in str(attrs.get(key) or ""):
            extra[key] = attrs[key]
    if attrs.get("width") is not None and not _whole(attrs["width"]):
        extra["width"] = attrs["width"]
    if attrs.get("height") is not None and not (_whole(attrs["height"]) and _whole(attrs.get("width"))):
        extra["height"] = attrs["height"]
    return extra


def _file_extra(attrs: dict[str, Any]) -> dict[str, Any]:
    """A file's attributes ``[name](url)`` can't say."""

    extra = {k: v for k, v in attrs.items() if k not in ("url", "name")}
    if extra.get("mime") and mimetypes.guess_type(_basename(str(attrs.get("url") or "")))[0] == extra["mime"]:
        del extra["mime"]
    return extra


class _Renderer:
    def __init__(self, include_ids: bool, links: RenderLinks | None):
        self.include_ids = include_ids
        self.links = links or _DEFAULT_LINKS

    def anchor(self, node: dict[str, Any]) -> str | None:
        """The block's id when it must be written (something refers to it)."""

        block_id = node.get("id")
        if not self.include_ids or not block_id:
            return None
        anchors = self.links.anchors
        return block_id if anchors is None or block_id in anchors else None

    def extra_attrs(self, node: dict[str, Any]) -> dict[str, Any]:
        attrs = node.get("attrs") or {}
        if node["type"] == "image":
            return _image_extra(attrs)
        if node["type"] == "file":
            return _file_extra(attrs)
        native = _NATIVE_ATTRS.get(node["type"], set())
        return {k: v for k, v in attrs.items() if k not in native}

    def meta(self, node: dict[str, Any], *, indent: int = 0, skip_attrs: bool = False) -> dict[str, Any]:
        """What only a comment can say: extension attributes, nesting."""

        meta: dict[str, Any] = {}
        if not skip_attrs:
            extra = self.extra_attrs(node)
            if extra:
                meta["attrs"] = extra
        if indent:
            meta["indent"] = indent
        return meta

    def html_meta(self, node: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """``meta`` plus the id when it must be written (inside HTML, as ``data-block``)."""

        meta = self.meta(node, **kwargs)
        anchor = self.anchor(node)
        if anchor:
            meta["id"] = anchor
        return meta

    def blocks(self, nodes: list[dict[str, Any]], indent: int = 0) -> list[str]:
        fragments: list[str] = []
        ordinal = 0
        previous: str | None = None
        for node in nodes:
            kind = node["type"]
            if kind == "paragraph" and not node.get("content") and not node.get("children") \
                    and not (self.links.anchors is not None and self.anchor(node)):
                # Markdown has no empty paragraph: a blank line is just spacing
                # (kept only when something refers to it).
                continue
            ordinal = _ordinal(node, ordinal) if kind == "numbered_item" else 0
            rendered = [part for part in self.block(node, ordinal, indent) if part]
            if kind in _LIST_TYPES and previous == kind and fragments and rendered:
                # Items of one list stay together (a tight list).
                fragments[-1] += "\n" + rendered.pop(0)
            fragments.extend(rendered)
            previous = kind
        return fragments

    def block(self, node: dict[str, Any], ordinal: int, indent: int) -> list[str]:
        kind = node["type"]
        children = node.get("children") or []
        anchor = self.anchor(node)
        if kind == "columns":
            # No Markdown dialect has columns: the contents follow one another,
            # between invisible markers.
            fragments = [marker("columns", self.html_meta(node, indent=indent))]
            for column in children:
                fragments.append(marker("column", self.html_meta(column)))
                fragments.extend(self.blocks(column.get("children") or []))
            fragments.append("<!-- /fortress:columns -->")
            return fragments
        if kind == "table":
            return [self.table(node, indent, anchor)]
        if kind in _INLINE_COMMENT_TYPES:
            meta = self.meta(node, indent=indent)
            caret = None
            if anchor:
                if kind != "heading" and node.get("content") and _ANCHOR_RE.match(anchor):
                    caret = anchor
                else:
                    meta["id"] = anchor
            text = self.text_block(node, ordinal, meta, caret)
            if kind in _LIST_TYPES:
                # Children align with the item's text after the bullet; a task's
                # "[ ] " is part of that text, so it does not count.
                indent_text = " " * len(
                    self.list_marker(node, ordinal).removesuffix("[ ] ").removesuffix("[x] ")
                )
                body = "\n\n".join(self.blocks(children))
                if body:
                    gap = "\n" if children[0]["type"] in _LIST_TYPES else "\n\n"
                    text += gap + _indent(body, indent_text)
                return [text]
            # Markdown can't nest under a paragraph: children follow, one level deeper.
            return [text, *self.blocks(children, indent + 1)]
        meta = self.meta(node, indent=indent)
        caret = anchor if anchor and _ANCHOR_RE.match(anchor) else None
        if anchor and not caret:
            meta["id"] = anchor
        inline_caret = False
        if kind == "code":
            source = self.code(node)
        elif kind in ("quote", "callout"):
            source = self.quote(node)
        elif kind == "divider":
            source = "---"
        elif kind == "image":
            source, inline_caret = self.image(node), True
        elif kind == "file":
            source = self.file(node)
            if source is None:  # nothing to link to: the comment says it all
                meta = {**meta, "type": "file", "attrs": node.get("attrs") or {}}
                source = ""
            inline_caret = bool(source)
        elif kind == "html":
            source = str((node.get("payload") or {}).get("html", "")).strip() or "<div></div>"
        else:  # pragma: no cover - every flow type is handled above
            source = ""
        if caret and inline_caret:
            source += f" ^{caret}"
        elif caret:
            # Obsidian: the id of a structured block goes on its own line after it.
            source += f"\n\n^{caret}"
        if not meta:
            return [source]
        return [f"{comment(meta)}\n{source}" if source else comment(meta)]

    def list_marker(self, node: dict[str, Any], ordinal: int) -> str:
        if node["type"] == "numbered_item":
            return f"{ordinal}. "
        if node["type"] == "check_item":
            return "- [x] " if (node.get("attrs") or {}).get("checked") else "- [ ] "
        return "- "

    def inline(self, nodes: list[dict[str, Any]], **kwargs: Any) -> str:
        return inline_to_markdown(nodes, links=self.links, **kwargs)

    def text_block(self, node: dict[str, Any], ordinal: int, meta: dict[str, Any],
                   caret: str | None) -> str:
        kind = node["type"]
        nodes = node.get("content") or []
        tail = f" {comment(meta)}" if meta else ""
        if caret:
            tail += f" ^{caret}"
        if kind == "heading":
            level = (node.get("attrs") or {}).get("level", 1)
            body = self.inline(nodes, newline="<br>")
            return "#" * level + " " + body + tail
        if kind in _LIST_TYPES:
            prefix = self.list_marker(node, ordinal)
            body = self.inline(nodes, newline="\\\n" + " " * len(prefix))
            if not body and not tail:
                return prefix.rstrip()
            return prefix + body + tail
        body = self.inline(nodes)
        if not body:
            # An empty paragraph is written only to keep what its comment says.
            return comment({**meta, "type": "paragraph"})
        return body + tail

    def code(self, node: dict[str, Any]) -> str:
        code = schema.inline_text(node.get("content"))
        longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
        fence = "`" * (longest + 1)
        language = (node.get("attrs") or {}).get("language") or ""
        return f"{fence}{language}\n{code}\n{fence}"

    def quote(self, node: dict[str, Any]) -> str:
        """A quote, or an Obsidian callout (``> [!type]`` and an optional title)."""

        attrs = node.get("attrs") or {}
        children = node.get("children") or []
        lines: list[str] = []
        if node["type"] == "callout":
            kind = str(attrs.get("callout_type") or "note")
            head = "[!" + (kind if _CALLOUT_TYPE_RE.match(kind) else "note") + "]"
            if attrs.get("callout_fold") in ("+", "-"):
                head += attrs["callout_fold"]
            first = children[0] if children else None
            if (attrs.get("callout_title") and first and first["type"] == "paragraph"
                    and not first.get("children") and not self.meta(first) and not self.anchor(first)
                    and "\n" not in schema.inline_text(first.get("content"))):
                title = self.inline(first.get("content") or [], newline="<br>")
                if title:
                    head += " " + title
                    children = children[1:]
            lines.append(head)
        body = "\n\n".join(self.blocks(children))
        if body:
            lines.extend(body.split("\n"))
        return "\n".join(f"> {line}" if line else ">" for line in lines or [""])

    def image(self, node: dict[str, Any]) -> str:
        """``![alt|WxH](url "caption")``: CommonMark, with Obsidian's size after a bar."""

        attrs = node.get("attrs") or {}
        alt = str(attrs.get("alt") or "")
        alt = "" if "\n" in alt else _escape_markdown(alt, False, False)
        width, height = attrs.get("width"), attrs.get("height")
        if _whole(width):
            alt += f"|{int(width)}" + (f"x{int(height)}" if _whole(height) else "")
        caption = str(attrs.get("caption") or "")
        title = ""
        if caption and "\n" not in caption:
            title = ' "' + caption.replace("\\", "\\\\").replace('"', '\\"') + '"'
        url = self.links.media(str(attrs.get("url") or ""))
        return f"![{alt}]({_link_destination(url) or '<>'}{title})"

    def file(self, node: dict[str, Any]) -> str | None:
        """``[name](url)``, or ``![name](url)`` for what Obsidian shows in place (PDF, audio, video)."""

        attrs = node.get("attrs") or {}
        url = self.links.media(str(attrs.get("url") or ""))
        if not url:
            return None
        name = str(attrs.get("name") or _basename(url) or "file").replace("\n", " ")
        bang = "!" if _extension(url) in _EMBED_EXTENSIONS else ""
        return f"{bang}[{_escape_markdown(name, False, False)}]({_link_destination(url)})"

    def table(self, node: dict[str, Any], indent: int, anchor: str | None) -> str:
        gfm = self.gfm_table(node)
        if gfm is None:
            return table_html(node, self, indent)
        meta = self.meta(node, indent=indent, skip_attrs=True)
        lines = gfm
        caret = anchor if anchor and _ANCHOR_RE.match(anchor) else None
        if anchor and not caret:
            meta["id"] = anchor
        out = (comment(meta) + "\n" if meta else "") + lines
        return out + (f"\n\n^{caret}" if caret else "")

    def gfm_table(self, node: dict[str, Any]) -> str | None:
        """A GFM table, if the table fits one.

        It fits with one header row, cells of at most one paragraph, no spans,
        colours or widths, and no block inside that something refers to or that
        has attributes Markdown can't say.
        """

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
        for number, row in enumerate(rows):
            if row.get("attrs") or self.html_meta(row, skip_attrs=True):
                return None
            for column, cell in enumerate(row["children"]):
                cell_attrs = cell.get("attrs") or {}
                if cell_attrs.keys() - {"align"} or cell_attrs.get("align") == "justify":
                    return None
                if self.html_meta(cell, skip_attrs=True):
                    return None
                if number == 0:
                    aligns.append(cell_attrs.get("align"))
                elif cell_attrs.get("align") != aligns[column]:
                    return None
                inner = cell.get("children") or []
                if len(inner) > 1:
                    return None
                if inner:
                    paragraph = inner[0]
                    if paragraph["type"] != "paragraph" or paragraph.get("children"):
                        return None
                    if self.anchor(paragraph) or self.meta(paragraph):
                        return None
        lines = []
        for number, row in enumerate(rows):
            cells = []
            for cell in row["children"]:
                inner = cell.get("children") or []
                content = inner[0].get("content") or [] if inner else []
                cells.append(self.inline(content, newline="<br>", table=True))
            lines.append("| " + " | ".join(cells) + " |")
            if number == 0:
                separators = [{"center": ":---:", "right": "---:", "left": ":---"}.get(a or "", "---")
                              for a in aligns]
                lines.append("| " + " | ".join(separators) + " |")
        return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def render_markdown(nodes: list[dict[str, Any]], *, include_ids: bool = True,
                    links: RenderLinks | None = None) -> str:
    """A block tree (``document_tree`` nodes) as Markdown.

    With ``include_ids`` the id of every block in ``links.anchors`` (all blocks
    when ``links`` or its anchors are None) is written: ``^id`` where Obsidian
    has a place for it, else in the block's comment. Attributes the syntax
    can't say are always written. ``links`` names linked notes and blocks and places stored files.
    """

    return "\n\n".join(
        fragment for fragment in _Renderer(include_ids, links).blocks(nodes) if fragment
    )


# --- HTML blocks (tables) ---------------------------------------------------------------------------


def image_html(attrs: dict[str, Any], meta: dict[str, Any] | None = None,
               links: RenderLinks | None = None) -> str:
    url = (links or _DEFAULT_LINKS).media(str(attrs.get("url") or ""))
    parts = [f'<img src="{_attr(url)}"']
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


def table_html(node: dict[str, Any], renderer: _Renderer, indent: int = 0) -> str:
    """A table as HTML: rows, cells and cell blocks carry their metadata in data-block."""

    attrs = node.get("attrs") or {}
    header_rows = attrs.get("header_rows") or 0
    header_cols = attrs.get("header_cols") or 0
    lines = [f"<table{_block_attr(renderer, node, attrs, indent)}>"]
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
    "image": {"url", "alt", "caption", "width", "height", "name"},
    "heading": {"level"}, "check_item": {"checked"}, "code": {"language"},
}


def _block_attr(renderer: _Renderer, node: dict[str, Any], attrs: dict[str, Any], indent: int = 0) -> str:
    meta = renderer.html_meta(node, skip_attrs=True, indent=indent)
    native = _TABLE_NATIVE.get(node["type"], set())
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
    inner = inline_to_html(node.get("content") or [], renderer.links)
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
    links = renderer.links
    if kind == "heading":
        level = attrs.get("level", 1)
        return f"<h{level}{data}>{inline_to_html(content, links)}</h{level}>{tail}"
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
        meta = renderer.html_meta(node, skip_attrs=True)
        extra = {k: v for k, v in attrs.items() if k not in _TABLE_NATIVE["image"]}
        if extra:
            meta["attrs"] = extra
        return image_html(attrs, meta or None, links)
    if kind == "file":
        meta = {**renderer.html_meta(node, skip_attrs=True), "attrs": attrs}
        name = _html_escape(str(attrs.get("name") or attrs.get("url") or "file"), quote=False)
        return (f'<div data-type="file" data-block="{_json_attr(meta)}">'
                f'<a href="{_attr(links.media(str(attrs.get("url", ""))))}">{name}</a></div>')
    if kind == "html":
        raw = str((node.get("payload") or {}).get("html", ""))
        return f'<div data-type="html" data-html="{_attr(raw)}"{data}></div>'
    return f"<p{data}>{inline_to_html(content, links)}</p>{tail}"


# --- Parsing ---------------------------------------------------------------------------------------


def parse_markdown(body: str, links: ParseLinks | None = None) -> list[BlockSpec]:
    """Canonical blocks for a Markdown body.

    Ids come from ``^id`` block identifiers and fortress comments; ``links``
    says which notes, blocks and headings wikilinks name (by default only links
    whose target is an id resolve).
    """

    links = links or ParseLinks()
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    token = _PARSE_LINKS.set(links)
    try:
        tokens = _BLOCK_MD.parse(body, {"links": links})
        blocks, _ = _Parser(tokens, links).sequence(0)
    finally:
        _PARSE_LINKS.reset(token)
    return legalize(blocks)


# The links of the parse in progress, for wikilinks inside HTML (tables).
_PARSE_LINKS: ContextVar[ParseLinks | None] = ContextVar("fortress_parse_links", default=None)
_CARET_RE = re.compile(r"(?:^|[ \t])\^([A-Za-z0-9_-]{1,128})$")
_CALLOUT_RE = re.compile(r"^\[!([A-Za-z][A-Za-z0-9_-]{0,39})\]([+-]?)[ \t]*")


def _apply_meta(block: BlockSpec, meta: dict[str, Any] | None) -> BlockSpec:
    if not meta:
        return block
    if meta.get("id"):
        block.id = meta["id"]
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
    if isinstance(meta.get("indent"), int):
        block.indent = meta["indent"]
    return block


@dataclass
class _Marker:
    kind: str
    meta: dict[str, Any]


class _Parser:
    def __init__(self, tokens: list[Token], links: ParseLinks):
        self.tokens = tokens
        self.links = links

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
                    pending = None
                    if meta.get("type") == "paragraph":
                        # An empty paragraph, kept for what its comment says.
                        items.append(_apply_meta(spec("paragraph"), {**meta, "type": None}))
                    else:
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
                caret = re.fullmatch(r"\^([A-Za-z0-9_-]{1,128})", inline.content.strip())
                if caret and not pending:
                    # Obsidian: a structured block's id on its own line after it.
                    previous = next((item for item in reversed(items) if isinstance(item, BlockSpec)), None)
                    if previous is not None and items[-1] is previous:
                        previous.id = self.links.block(None, caret.group(1))
                        continue
                produced = self.paragraph(inline)
            elif token.type == "heading_open":
                inline = tokens[index + 1]
                index += 3
                nodes, images, meta = self.inline(inline)
                heading = _apply_meta(spec("heading", {"level": int(token.tag[1:])}, nodes), meta)
                heading.id = self.links.own_heading(schema.inline_text(nodes), heading.id)
                produced = [heading, *self.media(images)]
            elif token.type == "blockquote_open":
                children, index = self.sequence(index + 1, "blockquote_close")
                produced = [_callout(children) or spec("quote", children=children)]
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
        caret = None
        while children and children[-1].type == "text" and not children[-1].content.strip():
            children.pop()
        # (checked against the source too: an escaped "\^" is text)
        if children and children[-1].type == "text" and _CARET_RE.search(token.content.rstrip()):
            found = _CARET_RE.search(children[-1].content)
            if found and (found.start() > 0 or len(children) > 1):
                # Obsidian's block id: " ^id" at the end of the block's last line.
                caret = self.links.block(None, found.group(1))
                children[-1] = _copy_token(children[-1], children[-1].content[:found.start()].rstrip())
                if not children[-1].content:
                    children.pop()
                while children and children[-1].type in ("softbreak", "hardbreak"):
                    children.pop()
        if children and children[-1].type == "html_inline":
            meta = parse_comment(children[-1].content)
            if meta is not None:
                children.pop()
                while children and children[-1].type in ("softbreak", "hardbreak"):
                    children.pop()
                if children and children[-1].type == "text":
                    children[-1] = _copy_token(children[-1], children[-1].content.rstrip())
        if caret:
            meta = {**(meta or {}), "id": caret}
        stripped = Token("inline", "", 0)
        stripped.children = children
        nodes, images = _nodes_from_inline_token(stripped)
        return nodes, images, meta

    def paragraph(self, token: Token) -> list[BlockSpec]:
        formula = _DISPLAY_MATH_RE.match(token.content.strip())
        if formula:
            # Fortress has no math blocks; a ``$$…$$`` formula keeps its source verbatim.
            source = formula.group(1)
            return [spec("code", {"language": "latex"}, [text_node(source)] if source else [])]
        nodes, images, meta = self.inline(token)
        blocks: list[BlockSpec] = []
        if not images and len(nodes) == 1 and nodes[0]["type"] == "link" \
                and nodes[0]["href"].startswith(MEDIA_PREFIX):
            # A paragraph that is only a link to a stored file is that file.
            name = schema.inline_text(nodes[0]["content"])
            return [_apply_meta(_file_spec({"url": nodes[0]["href"], "name": name}), meta)]
        if nodes or not images:
            blocks.append(_apply_meta(spec("paragraph", content=nodes), meta))
            meta = None
        blocks.extend(self.media(images))
        if meta:
            _apply_meta(blocks[0], meta)
        return blocks

    def media(self, images: list[dict[str, Any]]) -> list[BlockSpec]:
        """Image blocks, or file blocks for embeds of what isn't a picture (PDF, audio, ...)."""

        blocks = []
        for image in images:
            url = str(image.get("url", ""))
            extension = _extension(url)
            if extension and extension not in IMAGE_EXTENSIONS:
                blocks.append(_file_spec({"url": url, "name": image.get("alt") or _basename(url)}))
                continue
            try:
                blocks.append(spec("image", image))
            except BlockShapeError:
                blocks.append(spec("image", {"url": url}))
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
            children = self.media(images) + children
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
                content += self.media(images)
                row.append(spec("table_cell", {"align": align}, children=content))
            index += 1
        return [spec("table", {"header_rows": header_rows}, children=rows)], index + 1


def _copy_token(token: Token, content: str) -> Token:
    copy = Token(token.type, token.tag, token.nesting)
    copy.content = content
    return copy


_DISPLAY_MATH_RE = re.compile(r"^\$\$\s*(.*?)\s*\$\$$", re.DOTALL)


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
    # The last block at each nesting depth: a block indented n goes under the one at n - 1.
    levels: list[BlockSpec] = []

    def remember(block: BlockSpec) -> None:
        if block.id:
            by_id[block.id] = block
        for child in block.children:
            remember(child)

    for block in blocks:
        parent = by_id.get(block.parent_ref) if block.parent_ref else None
        depth = 0
        if parent is None and 0 < block.indent <= len(levels):
            depth = block.indent
            parent = levels[depth - 1]
        if parent is not None:
            parent.children.append(block)
        else:
            result.append(block)
        del levels[depth:]
        levels.append(block)
        remember(block)
    return result


def _file_spec(attrs: dict[str, Any]) -> BlockSpec:
    mime = mimetypes.guess_type(_basename(str(attrs.get("url") or "")))[0]
    if mime:
        attrs = {**attrs, "mime": mime}
    try:
        return spec("file", attrs)
    except BlockShapeError:
        return spec("file", {"url": str(attrs.get("url") or "")})


def _split_first_line(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    for index, node in enumerate(nodes):
        if node["type"] == "text" and "\n" in node["text"]:
            before, _, after = node["text"].partition("\n")
            head = nodes[:index] + ([{**node, "text": before}] if before else [])
            tail = ([{**node, "text": after}] if after else []) + nodes[index + 1:]
            return schema.merge_inline(head), schema.merge_inline(tail)
    return nodes, []


def _callout(children: list[BlockSpec]) -> BlockSpec | None:
    """An Obsidian callout: a quote whose first line is ``[!type]``, maybe with a title."""

    first = children[0] if children else None
    if first is None or first.type != "paragraph" or not first.content:
        return None
    head = first.content[0]
    found = _CALLOUT_RE.match(head["text"]) if head["type"] == "text" and not head.get("marks") else None
    if not found:
        return None
    rest = head["text"][found.end():]
    title, body = _split_first_line(([{**head, "text": rest}] if rest else []) + first.content[1:])
    attrs: dict[str, Any] = {}
    if found.group(1).lower() != "note":
        attrs["callout_type"] = found.group(1).lower()
    if found.group(2):
        attrs["callout_fold"] = found.group(2)
    blocks: list[BlockSpec] = []
    if title:
        attrs["callout_title"] = True
        blocks.append(spec("paragraph", content=title))
    if body or first.children:
        blocks.append(spec("paragraph", content=body, children=first.children))
    if blocks:
        # The paragraph's id and comment belong to its last line.
        last = blocks[-1]
        last.id, last.indent = first.id, first.indent
        last.attrs = {**first.attrs, **last.attrs}
    try:
        return spec("callout", attrs, children=blocks + children[1:])
    except BlockShapeError:
        return None


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
