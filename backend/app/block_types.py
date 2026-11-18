"""Block shapes beyond one line of Markdown (spec.md §2a).

Rich inline text (bold, italic, underline, strike, code, super/subscript, text
and highlight colours, links), tables, columns layouts, images, file
attachments and math.  Everything here is pure (no SQLite): validation, the
Markdown and plain text the store keeps beside the structured content, and the
Markdown/HTML round trip.

Inline content is BlockNote's (what the web editor already stores in
``content.blocknote``), extended with ``superscript``/``subscript`` styles and
hex colours::

    {"type": "text", "text": "Hi", "styles": {"bold": true, "textColor": "#C00000"}}
    {"type": "link", "href": "https://…", "content": [<text runs>]}
"""

from __future__ import annotations

import json
import re
from html import escape as _html_escape
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt


class BlockShapeError(ValueError):
    """A block's structured content is not one of the documented shapes."""


class _Unsupported(Exception):
    """Markup that inline runs cannot hold (an image inside a paragraph, ...)."""


BOOLEAN_STYLES = ("bold", "italic", "underline", "strike", "code", "superscript", "subscript")
COLOR_STYLES = ("textColor", "backgroundColor")
# Styles plain CommonMark cannot write; Markdown export uses inline HTML for them.
EXTENDED_STYLES = frozenset({"underline", "superscript", "subscript", *COLOR_STYLES})
# Short names an importer may use (the OneNote converter's run format).
STYLE_ALIASES = {
    "b": "bold", "i": "italic", "u": "underline", "s": "strike", "strikethrough": "strike",
    "sup": "superscript", "sub": "subscript", "code": "code", "color": "textColor", "highlight": "backgroundColor",
    "background": "backgroundColor",
}

# Blocks whose content is a line of inline text (Markdown import's and the web editor's).
TEXT_TYPES = frozenset({
    "paragraph", "heading", "list", "quote", "code", "callout", "bulletListItem",
    "numberedListItem", "checkListItem", "toggleListItem", "codeBlock",
})
# Blocks whose Markdown and plain text the server always derives from their structure.
STRUCTURED_TYPES = frozenset({"table", "image", "file", "math", "columns", "column"})
# What a table cell may hold besides inline text.
_NESTED_TYPES = TEXT_TYPES | {"table", "image", "file", "math", "thematic_break", "divider"}
_ALIGNMENTS = ("left", "center", "right", "justify")
_COLOR_RE = re.compile(
    r"^(?:#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,32}|(?:rgb|rgba|hsl|hsla)\([0-9\s.,%/]{1,48}\))$"
)
_MAX_DEPTH = 8


# --- Inline runs: validation ----------------------------------------------------


def _color(value: object, name: str) -> str | None:
    if value is None or value == "" or value == "default":
        return None
    if not isinstance(value, str) or not _COLOR_RE.match(value.strip()):
        raise BlockShapeError(f"{name} must be a CSS colour name or #hex value")
    return value.strip()


def _styles(raw: object, compact: dict[str, Any] | None = None) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BlockShapeError("styles must be an object")
    items = list(raw.items())
    strict = len(items)
    if compact:
        items += [(key, value) for key, value in compact.items() if key in STYLE_ALIASES]
    styles: dict[str, Any] = {}
    for index, (key, value) in enumerate(items):
        name = STYLE_ALIASES.get(key, key)
        if name in BOOLEAN_STYLES:
            if value in (True, 1):
                styles[name] = True
            elif value not in (False, 0, None):
                raise BlockShapeError(f"style {key} must be true or false")
        elif name in COLOR_STYLES:
            color = _color(value, key)
            if color:
                styles[name] = color
        elif index < strict:
            raise BlockShapeError(f"unknown inline style: {key}")
    return styles


def _text_run(text: str, styles: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": dict(styles)}


def normalize_inline(value: object) -> list[dict[str, Any]]:
    """Validated canonical runs from runs, a plain string or the compact form.

    The compact form is what an importer has at hand:
    ``{"text": "x", "b": 1, "i": 1, "u": 1, "s": 1, "sup": 1, "sub": 1,
    "code": 1, "color": "#C00000", "highlight": "#FFFF00", "link": "https://…"}``
    (unknown keys such as ``font`` or ``size`` are ignored).
    """

    if value is None:
        return []
    if isinstance(value, str):
        return _merge([_text_run(value, {})])
    if not isinstance(value, list):
        raise BlockShapeError("inline content must be a list of runs or a string")
    runs: list[dict[str, Any]] = []
    for item in value:
        runs.extend(_normalize_run(item))
    return _merge(runs)


def _normalize_run(item: object) -> list[dict[str, Any]]:
    if isinstance(item, str):
        return [_text_run(item, {})] if item else []
    if not isinstance(item, dict):
        raise BlockShapeError("an inline run must be an object or a string")
    kind = item.get("type", "text")
    href = item.get("href", item.get("link"))
    if kind == "link":
        if not isinstance(href, str) or not href.strip():
            raise BlockShapeError("a link needs an href")
        content = normalize_inline(item.get("content", item.get("text", "")))
        if any(run["type"] != "text" for run in content):
            raise BlockShapeError("links cannot contain links")
        return [{"type": "link", "href": href.strip(), "content": content}] if content else []
    if kind != "text":
        raise BlockShapeError(f"unknown inline content type: {kind}")
    text = item.get("text", "")
    if not isinstance(text, str):
        raise BlockShapeError("run text must be a string")
    compact = {key: value for key, value in item.items() if key not in {"type", "text", "styles"}}
    run = _text_run(text, _styles(item.get("styles"), compact))
    if not text:
        return []
    if href is not None and href != "":
        if not isinstance(href, str):
            raise BlockShapeError("a link needs an href")
        return [{"type": "link", "href": href.strip(), "content": [run]}]
    return [run]


def _merge(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for run in runs:
        if run["type"] == "text" and not run["text"]:
            continue
        last = merged[-1] if merged else None
        if last and last["type"] == run["type"] == "text" and last["styles"] == run["styles"]:
            merged[-1] = {**last, "text": last["text"] + run["text"]}
        elif last and last["type"] == run["type"] == "link" and last["href"] == run["href"]:
            merged[-1] = {**last, "content": _merge(last["content"] + run["content"])}
        else:
            merged.append(run)
    return merged


def check_loose_inline(value: object) -> None:
    """The light check for text blocks from editors (anything a renderer can walk)."""

    if value is None or isinstance(value, str):
        return
    if not isinstance(value, list) or not all(isinstance(item, (str, dict)) for item in value):
        raise BlockShapeError("content.blocknote must be a list of inline runs")


def inline_plain_text(value: object) -> str:
    """The words of inline content: what search, embeddings and Ask read."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(inline_plain_text(item.get("content")))
    return "".join(parts)


def has_extended_styles(value: object) -> bool:
    """Whether runs use a style plain Markdown cannot write (underline, colours, ...)."""

    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        styles = item.get("styles")
        if isinstance(styles, dict) and any(
            styles.get(name) not in (None, False, "", "default") for name in EXTENDED_STYLES
        ):
            return True
        if has_extended_styles(item.get("content")):
            return True
    return False


# --- Inline runs: Markdown and HTML -------------------------------------------------

# Outermost first: the order marks open in (and close in reverse).
_MARK_ORDER = ("backgroundColor", "textColor", "underline", "superscript", "subscript",
               "bold", "italic", "strike")
_MD_MARKS = {"bold": ("**", "**"), "italic": ("*", "*"), "strike": ("~~", "~~")}
_HTML_MARKS = {
    "bold": ("<strong>", "</strong>"), "italic": ("<em>", "</em>"), "strike": ("<s>", "</s>"),
    "underline": ("<u>", "</u>"), "superscript": ("<sup>", "</sup>"),
    "subscript": ("<sub>", "</sub>"),
}
_WIKI_RE = re.compile(r"^\[\[[^\]]+\]\]$")
_ENTITY_RE = re.compile(r"&#?[A-Za-z0-9]+;")
_ORDERED_START_RE = re.compile(r"\d{1,9}(?=[.)])")


def _mark_tags(name: str, value: Any, html: bool) -> tuple[str, str]:
    if name == "textColor":
        return f'<span style="color:{value}">', "</span>"
    if name == "backgroundColor":
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
            if char in "#>-+=":
                out.append("\\" + char)
                index += 1
                continue
        if char == "\n":
            out.append(char)
            at_line_start = True
        elif char in "\\`*_~<":
            out.append("\\" + char)
        elif char == "|" and table:
            out.append("\\|")
        elif char in "[]":
            doubled = (index + 1 < length and text[index + 1] == char) or (
                index > 0 and text[index - 1] == char
            )
            out.append(char if doubled else "\\" + char)
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


def _split_whitespace(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move edge whitespace out of emphasis (``**a **`` is not bold in Markdown)."""

    result: list[dict[str, Any]] = []
    for run in runs:
        if run["type"] != "text" or not any(run["styles"].get(m) for m in _MD_MARKS):
            result.append(run)
            continue
        text = run["text"]
        core = text.strip(" ")
        if not core:
            result.append(run)
            continue
        lead = text[: len(text) - len(text.lstrip(" "))]
        trail = text[len(text.rstrip(" ")):]
        plain = {k: v for k, v in run["styles"].items() if k not in _MD_MARKS and k != "code"}
        if lead:
            result.append(_text_run(lead, plain))
        result.append(_text_run(core, run["styles"]))
        if trail:
            result.append(_text_run(trail, plain))
    return _merge(result)


def _render_inline(
    runs: list[dict[str, Any]], *, html: bool, newline: str, table: bool, escape_html: bool
) -> str:
    out: list[str] = []
    stack: list[tuple[str, Any]] = []
    state = {"line_start": True}

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            name, value = stack.pop()
            out.append(_mark_tags(name, value, html)[1])

    def text_of(text: str, code: bool) -> str:
        if escape_html:
            return _html_escape(text, quote=False).replace("\n", "<br>")
        if code and not html:
            return _code_span(text)
        pieces = text.split("\n")
        rendered: list[str] = []
        for number, piece in enumerate(pieces):
            if number:
                rendered.append(newline)
                state["line_start"] = True
            if piece:
                rendered.append(_escape_markdown(piece, state["line_start"], table))
                state["line_start"] = False
        return "".join(rendered)

    def emit(items: list[dict[str, Any]]) -> None:
        for run in items:
            if run["type"] == "link":
                close_to(0)
                href = run["href"]
                if not html and not escape_html and _WIKI_RE.match(href):
                    out.append(href)
                    state["line_start"] = False
                    continue
                if html or escape_html:
                    out.append(f'<a href="{_attr(href)}">')
                    emit(run["content"])
                    close_to(0)
                    out.append("</a>")
                else:
                    out.append("[")
                    emit(run["content"])
                    close_to(0)
                    out.append("](" + _link_destination(href) + ")")
                state["line_start"] = False
                continue
            styles = run.get("styles") or {}
            wanted = [(name, styles[name]) for name in _MARK_ORDER if styles.get(name)]
            keep = 0
            while keep < len(stack) and keep < len(wanted) and stack[keep] == wanted[keep]:
                keep += 1
            close_to(keep)
            for name, value in wanted[keep:]:
                stack.append((name, value))
                out.append(_mark_tags(name, value, html or escape_html)[0])
            code = bool(styles.get("code"))
            if code and (html or escape_html):
                out.append("<code>" + text_of(run["text"], True) + "</code>")
            else:
                out.append(text_of(run["text"], code))

    emit(runs)
    close_to(0)
    return "".join(out)


def _link_destination(href: str) -> str:
    if re.search(r"[\s()<>]", href):
        return "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
    return href


def _comparable(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for run in runs:
        if run["type"] == "link" and _WIKI_RE.match(run["href"]):
            flat.append(_text_run(run["href"], {}))
        elif run["type"] == "link":
            href = _INLINE_MD.normalizeLink(run["href"])
            flat.append({**run, "href": href, "content": _comparable(run["content"])})
        else:
            flat.append(run)
    merged = _merge(flat)
    if merged and merged[0]["type"] == "text":
        merged[0] = {**merged[0], "text": merged[0]["text"].lstrip()}
    if merged and merged[-1]["type"] == "text":
        merged[-1] = {**merged[-1], "text": merged[-1]["text"].rstrip()}
    return _merge(merged)


def inline_to_markdown(value: object, *, newline: str = "\\\n", table: bool = False) -> str:
    """Markdown for inline runs: native marks where CommonMark has them, HTML otherwise.

    Bold/italic/strike/code/links are written natively; underline, super/subscript,
    text colour and highlight as ``<u>``, ``<sup>``, ``<sub>``,
    ``<span style="color:…">`` and ``<mark style="background:…">``. If the
    native form would not read back as the same runs (emphasis next to
    punctuation, ...), every mark is written as HTML instead.
    """

    try:
        runs = normalize_inline(value)
    except BlockShapeError:
        return inline_plain_text(value)
    runs = _split_whitespace(runs)
    check_newline = "<br>" if table else "\\\n"
    native = _render_inline(runs, html=False, newline=check_newline, table=table, escape_html=False)
    parsed = inline_from_markdown(native)
    if parsed is not None and _comparable(parsed) == _comparable(runs):
        if newline == check_newline:
            return native
        return _render_inline(runs, html=False, newline=newline, table=table, escape_html=False)
    return _render_inline(
        runs, html=True, newline="<br>" if table else newline, table=table, escape_html=False
    )


def inline_to_html(value: object) -> str:
    """HTML for inline runs (inside HTML tables)."""

    try:
        runs = normalize_inline(value)
    except BlockShapeError:
        return _html_escape(inline_plain_text(value), quote=False)
    return _render_inline(runs, html=True, newline="<br>", table=False, escape_html=True)


_INLINE_MD = MarkdownIt("commonmark").enable(["strikethrough"])


def inline_from_markdown(markdown: str) -> list[dict[str, Any]] | None:
    """Runs for one block's inline Markdown, or None when it holds things runs can't."""

    try:
        html = _INLINE_MD.renderInline(markdown)
        return _runs_from_nodes(_parse_html(html), collapse=False)
    except _Unsupported:
        return None


def inline_from_html(html: str) -> list[dict[str, Any]] | None:
    try:
        return _runs_from_nodes(_parse_html(html), collapse=True)
    except _Unsupported:
        return None


# --- A tiny HTML tree ------------------------------------------------------------

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


_INLINE_TAG_STYLES = {
    "strong": "bold", "b": "bold", "em": "italic", "i": "italic", "s": "strike",
    "del": "strike", "strike": "strike", "u": "underline", "ins": "underline",
    "sup": "superscript", "sub": "subscript", "code": "code",
}


def _safe_color(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if _COLOR_RE.match(value) and value != "default" else None


def _element_styles(element: _Element) -> dict[str, Any]:
    tag = element.tag
    if tag in _INLINE_TAG_STYLES:
        return {_INLINE_TAG_STYLES[tag]: True}
    css = _style_map(element)
    styles: dict[str, Any] = {}
    color = _safe_color(css.get("color") or (element.attrs.get("color") if tag == "font" else None))
    background = _safe_color(css.get("background-color") or css.get("background"))
    if tag == "mark":
        background = background or _safe_color(element.attrs.get("data-color")) or "yellow"
    if color:
        styles["textColor"] = color
    if background:
        styles["backgroundColor"] = background
    return styles


def _runs_from_nodes(nodes: list[_Element | str], *, collapse: bool) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    state = {"skip_newline": False}

    def text(value: str, styles: dict[str, Any], target: list[dict[str, Any]]) -> None:
        if state["skip_newline"] and value.startswith("\n"):
            value = value[1:]
        state["skip_newline"] = False
        if collapse:
            value = re.sub(r"\s+", " ", value)
        if value:
            target.append(_text_run(value, styles))

    def walk(items: list[_Element | str], styles: dict[str, Any], target: list) -> None:
        for node in items:
            if isinstance(node, str):
                text(node, styles, target)
                continue
            tag = node.tag
            if tag == "br":
                target.append(_text_run("\n", styles))
                state["skip_newline"] = True
                continue
            if tag == "a":
                href = node.attrs.get("href", "")
                inner: list[dict[str, Any]] = []
                walk(node.children, styles, inner)
                inner = _merge(inner)
                if not href:
                    target.extend(inner)
                elif inner:
                    target.append({"type": "link", "href": href, "content": inner})
                continue
            if tag in _INLINE_TAG_STYLES or tag in {"span", "mark", "font"}:
                walk(node.children, {**styles, **_element_styles(node)}, target)
                continue
            raise _Unsupported(tag)

    walk(nodes, {}, runs)
    runs = _merge(runs)
    if collapse and runs:
        if runs[0]["type"] == "text":
            runs[0] = {**runs[0], "text": runs[0]["text"].lstrip(" ")}
        if runs[-1]["type"] == "text":
            runs[-1] = {**runs[-1], "text": runs[-1]["text"].rstrip(" ")}
        runs = _merge(runs)
    return runs


def _attr(value: object) -> str:
    return _html_escape(str(value), quote=True).replace("\n", "&#10;").replace("\r", "")


def _json_attr(value: dict[str, Any]) -> str:
    return _attr(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def strip_inline_html(markdown: str) -> str:
    """Markdown without the inline mark tags (so search does not index ``span``)."""

    return _MARK_TAG_RE.sub("", markdown)


_MARK_TAG_RE = re.compile(r"</?(?:u|ins|sup|sub|mark|span|font|s|del)(?:\s[^<>]*)?>", re.IGNORECASE)
_HAS_MARK_TAG_RE = re.compile(r"<(?:u|ins|sup|sub|mark|span|font)\b", re.IGNORECASE)


def has_inline_mark_html(markdown: str) -> bool:
    return bool(_HAS_MARK_TAG_RE.search(markdown))


# --- Small helpers ---------------------------------------------------------------


def _number(value: object, name: str, *, minimum: float = 0) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise BlockShapeError(f"{name} must be a number")
    if value < minimum:
        raise BlockShapeError(f"{name} must be at least {minimum}")
    return int(value) if float(value).is_integer() else value


def _optional_str(attrs: dict[str, Any], key: str) -> None:
    if key in attrs and attrs[key] is not None and not isinstance(attrs[key], str):
        raise BlockShapeError(f"{key} must be a string")


def _parse_number(value: str | None) -> int | float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _dict(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BlockShapeError(f"{name} must be an object")
    return value


# --- Tables ------------------------------------------------------------------------


def normalize_table(value: object, depth: int = 0) -> dict[str, Any]:
    """Validate a table's ``content.blocknote`` (BlockNote's ``tableContent``, extended).

    ``{"type": "tableContent", "columnWidths": [120, null], "headerRows": 1,
    "rows": [{"cells": [{"type": "tableCell", "props": {"backgroundColor": "#DEEAF6"},
    "content": [<runs>], "blocks": [<nested blocks>]}]}]}``
    """

    if depth > _MAX_DEPTH:
        raise BlockShapeError("tables are nested too deeply")
    if not isinstance(value, dict):
        raise BlockShapeError("a table's content.blocknote must be an object with rows")
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise BlockShapeError("a table needs at least one row")
    if len(rows) > 5000:
        raise BlockShapeError("a table may have at most 5000 rows")
    normalized_rows = []
    for row in rows:
        cells = row.get("cells") if isinstance(row, dict) else row
        if not isinstance(cells, list) or not cells:
            raise BlockShapeError("every table row needs a list of cells")
        if len(cells) > 500:
            raise BlockShapeError("a table row may have at most 500 cells")
        normalized_rows.append({"cells": [_normalize_cell(cell, depth) for cell in cells]})
    columns = max(sum(cell["props"].get("colspan", 1) for cell in row["cells"]) for row in normalized_rows)
    widths = value.get("columnWidths")
    if widths is None:
        widths = []
    if not isinstance(widths, list):
        raise BlockShapeError("columnWidths must be a list")
    widths = [_number(width, "a column width") for width in widths][:columns]
    widths += [None] * (columns - len(widths))
    result: dict[str, Any] = {"type": "tableContent", "columnWidths": widths}
    for key in ("headerRows", "headerCols"):
        count = value.get(key)
        if count is None or count is False:
            continue
        if count is True:
            count = 1
        if not isinstance(count, int) or count < 0:
            raise BlockShapeError(f"{key} must be a whole number")
        if count:
            result[key] = count
    result["rows"] = normalized_rows
    return result


def _normalize_cell(cell: object, depth: int) -> dict[str, Any]:
    if isinstance(cell, (str, list)) or cell is None:
        return {"type": "tableCell", "props": {}, "content": normalize_inline(cell)}
    if not isinstance(cell, dict):
        raise BlockShapeError("a table cell must be an object, a list of runs or a string")
    if cell.get("type", "tableCell") != "tableCell":
        raise BlockShapeError("a table cell's type must be tableCell")
    props: dict[str, Any] = {}
    for key, value in _dict(cell.get("props"), "cell props").items():
        if key in COLOR_STYLES:
            color = _color(value, key)
            if color:
                props[key] = color
        elif key == "textAlignment":
            if value not in _ALIGNMENTS:
                raise BlockShapeError("textAlignment must be left, center, right or justify")
            if value != "left":
                props[key] = value
        elif key in {"colspan", "rowspan"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BlockShapeError(f"{key} must be a positive whole number")
            if value != 1:
                props[key] = value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            props[key] = value
        else:
            raise BlockShapeError(f"cell prop {key} must be a plain value")
    result: dict[str, Any] = {
        "type": "tableCell", "props": props, "content": normalize_inline(cell.get("content")),
    }
    blocks = cell.get("blocks")
    if blocks is not None:
        if not isinstance(blocks, list):
            raise BlockShapeError("a cell's blocks must be a list")
        normalized = [normalize_nested_block(block, depth + 1) for block in blocks]
        if normalized:
            result["blocks"] = normalized
    return result


def normalize_nested_block(block: object, depth: int = 1) -> dict[str, Any]:
    """A block inside a table cell: ``{"type", "attrs", "content", "children"}`` (no id)."""

    if depth > _MAX_DEPTH:
        raise BlockShapeError("blocks are nested too deeply")
    if not isinstance(block, dict):
        raise BlockShapeError("a nested block must be an object")
    block_type = block.get("type", "paragraph")
    if block_type not in _NESTED_TYPES:
        raise BlockShapeError(f"a table cell cannot hold a {block_type!r} block")
    attrs = dict(_dict(block.get("attrs"), "attrs"))
    content = _dict(block.get("content"), "content")
    result_content: dict[str, Any] = {}
    if block_type == "table":
        result_content["blocknote"] = normalize_table(content.get("blocknote"), depth)
        attrs = _table_attrs(attrs)
    elif block_type in TEXT_TYPES:
        result_content["blocknote"] = normalize_inline(content.get("blocknote"))
        attrs = _text_attrs(block_type, attrs)
    elif block_type == "image":
        attrs = _image_attrs(attrs)
    elif block_type == "file":
        attrs = _file_attrs(attrs)
    elif block_type == "math":
        result_content.update(_math_content(content))
    children = block.get("children") or []
    if not isinstance(children, list):
        raise BlockShapeError("children must be a list")
    result: dict[str, Any] = {"type": block_type, "attrs": attrs, "content": result_content}
    if children:
        result["children"] = [normalize_nested_block(child, depth + 1) for child in children]
    return result


def _text_attrs(block_type: str, attrs: dict[str, Any]) -> dict[str, Any]:
    if block_type == "heading" and "level" in attrs:
        level = attrs["level"]
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise BlockShapeError("heading level must be 1 to 6")
    if "checked" in attrs and attrs["checked"] is not None and not isinstance(attrs["checked"], bool):
        raise BlockShapeError("checked must be true or false")
    if "ordered" in attrs and not isinstance(attrs["ordered"], bool):
        raise BlockShapeError("ordered must be true or false")
    return attrs


def _table_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    if "borders" in attrs and not isinstance(attrs["borders"], bool):
        raise BlockShapeError("borders must be true or false")
    return attrs


def keep_cell_blocks(old: object, new: object) -> object:
    """``new`` table content with each cell's nested blocks carried over from ``old``.

    An editor that cannot show nested cell blocks (the web editor) saves cells
    without a ``blocks`` key; while the table keeps its shape, those blocks stay.
    A cell sent with ``"blocks": []`` clears them.
    """

    if not (isinstance(old, dict) and isinstance(new, dict)):
        return new
    old_rows, new_rows = old.get("rows"), new.get("rows")
    if not (isinstance(old_rows, list) and isinstance(new_rows, list)):
        return new

    def cells(rows: list) -> list[int]:
        return [len(row.get("cells") or []) if isinstance(row, dict) else -1 for row in rows]

    if cells(old_rows) != cells(new_rows):
        return new
    rows = []
    for old_row, new_row in zip(old_rows, new_rows):
        merged = []
        for old_cell, new_cell in zip(old_row["cells"], new_row["cells"]):
            if (
                isinstance(old_cell, dict) and old_cell.get("blocks")
                and isinstance(new_cell, dict) and "blocks" not in new_cell
            ):
                new_cell = {**new_cell, "blocks": old_cell["blocks"]}
            merged.append(new_cell)
        rows.append({**new_row, "cells": merged})
    return {**new, "rows": rows}


def table_plain_text(table: dict[str, Any]) -> str:
    """Rows on lines, cells separated by tabs (what search and Ask read)."""

    return "\n".join(
        "\t".join(_cell_plain_text(cell) for cell in row["cells"]) for row in table["rows"]
    )


def _cell_plain_text(cell: dict[str, Any]) -> str:
    parts = [inline_plain_text(cell.get("content"))]
    parts += [nested_plain_text(block) for block in cell.get("blocks", [])]
    return " ".join(" ".join(part.split()) for part in parts if part and part.strip())


def nested_plain_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    content = block.get("content") or {}
    if block_type == "table":
        own = table_plain_text(content["blocknote"]).replace("\t", " ").replace("\n", " ")
    else:
        own = plain_text(block_type, block.get("attrs") or {}, content)
    children = [nested_plain_text(child) for child in block.get("children", [])]
    return " ".join(part for part in [own, *children] if part)


def _is_simple_table(attrs: dict[str, Any], table: dict[str, Any]) -> bool:
    if table.get("headerRows") != 1 or table.get("headerCols"):
        return False
    if any(key != "borders" for key in attrs) or attrs.get("borders") is False:
        return False
    if any(width is not None for width in table["columnWidths"]):
        return False
    if set(table) - {"type", "columnWidths", "headerRows", "rows"}:
        return False
    widths = {len(row["cells"]) for row in table["rows"]}
    if len(widths) != 1:
        return False
    for row in table["rows"]:
        for cell in row["cells"]:
            if cell.get("blocks") or set(cell["props"]) - {"textAlignment"}:
                return False
            if cell["props"].get("textAlignment") == "justify":
                return False
    for column in range(widths.pop()):
        if len({row["cells"][column]["props"].get("textAlignment") for row in table["rows"]}) > 1:
            return False
    return True


def table_markdown(attrs: dict[str, Any], table: dict[str, Any]) -> str:
    """A GFM table when every cell is one line of text, else an HTML ``<table>``."""

    if not _is_simple_table(attrs, table):
        return table_html(attrs, table)
    rows = table["rows"]
    lines = []
    for number, row in enumerate(rows):
        cells = [inline_to_markdown(cell["content"], newline="<br>", table=True) for cell in row["cells"]]
        lines.append("| " + " | ".join(cells) + " |")
        if number == 0:
            separators = []
            for cell in row["cells"]:
                align = cell["props"].get("textAlignment")
                separators.append({"center": ":---:", "right": "---:"}.get(align, "---"))
            lines.append("| " + " | ".join(separators) + " |")
    return "\n".join(lines)


def table_html(attrs: dict[str, Any], table: dict[str, Any], *, one_line: bool = False) -> str:
    """An HTML table with no blank lines (Markdown keeps it as one HTML block)."""

    table_attrs = ""
    if attrs.get("borders") is False:
        table_attrs += ' data-borders="false"'
    extra = {key: value for key, value in attrs.items() if key != "borders"}
    if extra:
        table_attrs += f' data-attrs="{_json_attr(extra)}"'
    if table.get("headerCols"):
        table_attrs += f' data-header-cols="{int(table["headerCols"])}"'
    lines = [f"<table{table_attrs}>"]
    if any(width is not None for width in table["columnWidths"]):
        cols = "".join(
            f'<col style="width:{width}px">' if width is not None else "<col>"
            for width in table["columnWidths"]
        )
        lines.append(f"<colgroup>{cols}</colgroup>")
    header_rows = table.get("headerRows", 0)
    header_cols = table.get("headerCols", 0)
    for row_number, row in enumerate(table["rows"]):
        cells = []
        for column, cell in enumerate(row["cells"]):
            tag = "th" if row_number < header_rows or column < header_cols else "td"
            cells.append(f"<{tag}{_cell_attributes(cell['props'])}>{_cell_html(cell)}</{tag}>")
        lines.append("<tr>" + "".join(cells) + "</tr>")
    lines.append("</table>")
    return ("" if one_line else "\n").join(lines)


def _cell_attributes(props: dict[str, Any]) -> str:
    css = []
    if props.get("backgroundColor"):
        css.append(f"background-color:{props['backgroundColor']}")
    if props.get("textColor"):
        css.append(f"color:{props['textColor']}")
    if props.get("textAlignment"):
        css.append(f"text-align:{props['textAlignment']}")
    result = f' style="{_attr(";".join(css))}"' if css else ""
    for key in ("colspan", "rowspan"):
        if props.get(key):
            result += f' {key}="{int(props[key])}"'
    return result


def _cell_html(cell: dict[str, Any]) -> str:
    html = inline_to_html(cell.get("content"))
    if cell.get("blocks"):
        html += blocks_to_html(cell["blocks"])
    return html


def blocks_to_html(blocks: list[dict[str, Any]]) -> str:
    """Nested (in-cell) blocks as single-line HTML."""

    out: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        kind = _list_kind(block)
        if kind:
            items = []
            while index < len(blocks) and _list_kind(blocks[index]) == kind:
                items.append(_list_item_html(blocks[index]))
                index += 1
            out.append(f"<{kind}>{''.join(items)}</{kind}>")
            continue
        out.append(_block_html(block))
        index += 1
    return "".join(out)


def _list_kind(block: dict[str, Any]) -> str | None:
    block_type = block.get("type")
    attrs = block.get("attrs") or {}
    if block_type == "numberedListItem" or (block_type == "list" and attrs.get("ordered")):
        return "ol"
    if block_type in {"list", "bulletListItem", "checkListItem"}:
        return "ul"
    return None


def _list_item_html(block: dict[str, Any]) -> str:
    attrs = block.get("attrs") or {}
    checked = attrs.get("checked")
    extra = f' data-checked="{"true" if checked else "false"}"' if isinstance(checked, bool) else ""
    inner = inline_to_html((block.get("content") or {}).get("blocknote"))
    children = block.get("children") or []
    return f"<li{extra}>{inner}{blocks_to_html(children) if children else ''}</li>"


def _block_html(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    attrs = block.get("attrs") or {}
    content = block.get("content") or {}
    children = block.get("children") or []
    tail = blocks_to_html(children) if children else ""
    runs = content.get("blocknote")
    if block_type == "heading":
        level = attrs.get("level", 1)
        return f"<h{level}>{inline_to_html(runs)}</h{level}>{tail}"
    if block_type == "quote":
        return f"<blockquote>{inline_to_html(runs)}{tail}</blockquote>"
    if block_type in {"code", "codeBlock"}:
        language = attrs.get("language")
        css = f' class="language-{_attr(language)}"' if language else ""
        code = _html_escape(inline_plain_text(runs), quote=False).replace("\n", "&#10;")
        return f"<pre><code{css}>{code}</code></pre>{tail}"
    if block_type in {"thematic_break", "divider"}:
        return "<hr>" + tail
    if block_type == "table":
        return table_html(attrs, content["blocknote"], one_line=True) + tail
    if block_type == "image":
        return image_html(attrs) + tail
    if block_type == "file":
        name = _html_escape(str(attrs.get("name") or attrs.get("url") or "file"), quote=False)
        return (
            f'<div data-type="file" data-attrs="{_json_attr(attrs)}">'
            f'<a href="{_attr(attrs.get("url", ""))}">{name}</a></div>{tail}'
        )
    if block_type == "math":
        latex = _html_escape(str(content.get("latex", "")), quote=False).replace("\n", "&#10;")
        return f'<div data-type="math">{latex}</div>{tail}'
    return f"<p>{inline_to_html(runs)}</p>{tail}"


_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "hr",
               "table", "img", "div"}


def table_from_html(html: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(attrs, tableContent)`` from one HTML ``<table>``, or None if it isn't one."""

    nodes = [node for node in _parse_html(html) if not (isinstance(node, str) and not node.strip())]
    if len(nodes) != 1 or not isinstance(nodes[0], _Element) or nodes[0].tag != "table":
        return None
    try:
        return _table_from_element(nodes[0], 0)
    except (_Unsupported, BlockShapeError, ValueError):
        return None


def _children(element: _Element, *tags: str) -> list[_Element]:
    return [child for child in element.children if isinstance(child, _Element) and child.tag in tags]


def _table_from_element(element: _Element, depth: int) -> tuple[dict[str, Any], dict[str, Any]]:
    attrs: dict[str, Any] = {}
    if element.attrs.get("data-attrs"):
        extra = json.loads(element.attrs["data-attrs"])
        if isinstance(extra, dict):
            attrs.update(extra)
    if element.attrs.get("data-borders") == "false" or element.attrs.get("border") == "0":
        attrs["borders"] = False
    widths: list[Any] = []
    for group in _children(element, "colgroup"):
        for col in _children(group, "col"):
            widths.append(_parse_number(_style_map(col).get("width") or col.attrs.get("width")))
    rows: list[_Element] = []
    for child in _children(element, "tr", "thead", "tbody", "tfoot"):
        rows.extend([child] if child.tag == "tr" else _children(child, "tr"))
    if not rows:
        raise _Unsupported("table without rows")
    header_rows = 0
    counting = True
    table_rows = []
    for row in rows:
        cells = _children(row, "td", "th")
        if not cells:
            continue
        if counting and all(cell.tag == "th" for cell in cells):
            header_rows += 1
        else:
            counting = False
        table_rows.append({"cells": [_cell_from_element(cell, depth) for cell in cells]})
    table: dict[str, Any] = {"columnWidths": widths, "rows": table_rows}
    if header_rows:
        table["headerRows"] = header_rows
    header_cols = _parse_number(element.attrs.get("data-header-cols"))
    if header_cols:
        table["headerCols"] = int(header_cols)
    return attrs, normalize_table(table, depth)


def _cell_from_element(cell: _Element, depth: int) -> dict[str, Any]:
    css = _style_map(cell)
    props: dict[str, Any] = {}
    background = _safe_color(css.get("background-color") or css.get("background") or cell.attrs.get("bgcolor"))
    if background:
        props["backgroundColor"] = background
    color = _safe_color(css.get("color"))
    if color:
        props["textColor"] = color
    align = (css.get("text-align") or cell.attrs.get("align") or "").lower()
    if align in _ALIGNMENTS and align != "left":
        props["textAlignment"] = align
    for key in ("colspan", "rowspan"):
        number = _parse_number(cell.attrs.get(key))
        if number and int(number) > 1:
            props[key] = int(number)
    content, blocks = _split_cell_nodes(cell.children, depth)
    result: dict[str, Any] = {"type": "tableCell", "props": props, "content": content}
    if blocks:
        result["blocks"] = blocks
    return result


def _is_block_node(node: _Element | str) -> bool:
    return isinstance(node, _Element) and node.tag in _BLOCK_TAGS


def _split_cell_nodes(nodes: list[_Element | str], depth: int):
    """A cell's leading inline text, then its blocks."""

    leading: list[_Element | str] = []
    index = 0
    while index < len(nodes) and not _is_block_node(nodes[index]):
        leading.append(nodes[index])
        index += 1
    content = _runs_from_nodes(leading, collapse=True)
    return content, _blocks_from_nodes(nodes[index:], depth + 1)


def _blocks_from_nodes(nodes: list[_Element | str], depth: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    loose: list[_Element | str] = []

    def flush() -> None:
        runs = _runs_from_nodes(loose, collapse=True)
        loose.clear()
        if runs:
            blocks.append({"type": "paragraph", "attrs": {}, "content": {"blocknote": runs}})

    for node in nodes:
        if not _is_block_node(node):
            loose.append(node)
            continue
        flush()
        assert isinstance(node, _Element)
        blocks.extend(_blocks_from_element(node, depth))
    flush()
    return blocks


def _blocks_from_element(element: _Element, depth: int) -> list[dict[str, Any]]:
    tag = element.tag
    if depth > _MAX_DEPTH:
        raise _Unsupported("too deep")
    if tag == "p":
        return [{"type": "paragraph", "attrs": {}, "content": {
            "blocknote": _runs_from_nodes(element.children, collapse=True)}}]
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return [{"type": "heading", "attrs": {"level": int(tag[1])}, "content": {
            "blocknote": _runs_from_nodes(element.children, collapse=True)}}]
    if tag in {"ul", "ol"}:
        items = []
        for item in _children(element, "li"):
            attrs: dict[str, Any] = {}
            if tag == "ol":
                attrs["ordered"] = True
            if item.attrs.get("data-checked") in {"true", "false"}:
                attrs["checked"] = item.attrs["data-checked"] == "true"
            runs, children = _split_cell_nodes(item.children, depth)
            block: dict[str, Any] = {"type": "list", "attrs": attrs, "content": {"blocknote": runs}}
            if children:
                block["children"] = children
            items.append(block)
        return items
    if tag == "blockquote":
        runs, children = _split_cell_nodes(element.children, depth)
        block = {"type": "quote", "attrs": {}, "content": {"blocknote": runs}}
        if children:
            block["children"] = children
        return [block]
    if tag == "pre":
        code = element
        codes = _children(element, "code")
        if codes:
            code = codes[0]
        language = ""
        for name in code.attrs.get("class", "").split():
            if name.startswith("language-"):
                language = name[len("language-"):]
        text = _all_text(code)
        attrs = {"language": language} if language else {}
        runs = [_text_run(text, {})] if text else []
        return [{"type": "code", "attrs": attrs, "content": {"blocknote": runs}}]
    if tag == "hr":
        return [{"type": "thematic_break", "attrs": {}, "content": {}}]
    if tag == "table":
        attrs, table = _table_from_element(element, depth)
        return [{"type": "table", "attrs": attrs, "content": {"blocknote": table}}]
    if tag == "img":
        return [{"type": "image", "attrs": image_attrs_from_element(element), "content": {}}]
    if tag == "div" and element.attrs.get("data-type") == "file":
        attrs = json.loads(element.attrs.get("data-attrs") or "{}")
        return [{"type": "file", "attrs": attrs if isinstance(attrs, dict) else {}, "content": {}}]
    if tag == "div" and element.attrs.get("data-type") == "math":
        return [{"type": "math", "attrs": {}, "content": {"latex": _all_text(element)}}]
    if tag == "div":
        return _blocks_from_nodes(element.children, depth)
    raise _Unsupported(tag)


def _all_text(element: _Element) -> str:
    parts: list[str] = []
    for child in element.children:
        parts.append(child if isinstance(child, str) else _all_text(child))
    return "".join(parts)


# --- Images, files, math -------------------------------------------------------------

_IMAGE_KNOWN = ("url", "alt", "caption", "width", "height", "name")


def _image_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(attrs)
    for key in ("url", "alt", "caption", "name"):
        _optional_str(attrs, key)
    for key in ("width", "height", "previewWidth"):
        if key in attrs:
            attrs[key] = _number(attrs[key], key)
            if attrs[key] is None:
                del attrs[key]
    return attrs


def image_html(attrs: dict[str, Any]) -> str:
    parts = [f'<img src="{_attr(attrs.get("url") or "")}"']
    if attrs.get("alt"):
        parts.append(f'alt="{_attr(attrs["alt"])}"')
    if attrs.get("caption"):
        parts.append(f'title="{_attr(attrs["caption"])}"')
    width = attrs.get("width", attrs.get("previewWidth"))
    if width is not None:
        parts.append(f'width="{width}"')
    if attrs.get("height") is not None:
        parts.append(f'height="{attrs["height"]}"')
    if attrs.get("name"):
        parts.append(f'data-name="{_attr(attrs["name"])}"')
    extra = {key: value for key, value in attrs.items() if key not in _IMAGE_KNOWN}
    if extra:
        parts.append(f'data-attrs="{_json_attr(extra)}"')
    return " ".join(parts) + ">"


def image_attrs_from_element(element: _Element) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if element.attrs.get("data-attrs"):
        try:
            extra = json.loads(element.attrs["data-attrs"])
        except ValueError:
            extra = None
        if isinstance(extra, dict):
            attrs.update(extra)
    attrs["url"] = element.attrs.get("src", "")
    for html_name, key in (("alt", "alt"), ("title", "caption"), ("data-name", "name")):
        if element.attrs.get(html_name):
            attrs[key] = element.attrs[html_name]
    for key in ("width", "height"):
        number = _parse_number(element.attrs.get(key))
        if number is not None:
            attrs[key] = number
    if "previewWidth" in attrs and attrs.get("width") == attrs["previewWidth"]:
        del attrs["width"]
    return _image_attrs(attrs)


def image_from_html(html: str) -> dict[str, Any] | None:
    """Image attrs when ``html`` is exactly one ``<img>`` tag."""

    nodes = [node for node in _parse_html(html.strip()) if not (isinstance(node, str) and not node.strip())]
    if len(nodes) != 1 or not isinstance(nodes[0], _Element) or nodes[0].tag != "img":
        return None
    try:
        return image_attrs_from_element(nodes[0])
    except BlockShapeError:
        return None


def _file_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(attrs)
    for key in ("url", "name", "mime", "caption"):
        _optional_str(attrs, key)
    if "size" in attrs:
        attrs["size"] = _number(attrs["size"], "size")
        if attrs["size"] is None:
            del attrs["size"]
    return attrs


def _human_size(size: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover


def marker(kind: str, value: dict[str, Any] | None = None) -> str:
    """``<!-- fortress:<kind> {json} -->``: invisible in Markdown viewers, read back on import."""

    if not value:
        return f"<!-- fortress:{kind} -->"
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- fortress:{kind} {encoded.replace('>', chr(92) + 'u003e')} -->"


_MARKER_RE = re.compile(r"^<!--\s*(/?)fortress:(columns|column|file)\s*(\{.*\})?\s*-->$", re.DOTALL)


def parse_marker(source: str) -> tuple[str, dict[str, Any]] | None:
    """``("columns"|"column"|"/columns"|"file", attrs)`` for one of our markers."""

    match = _MARKER_RE.match(source.strip())
    if not match:
        return None
    value: Any = {}
    if match.group(3):
        try:
            value = json.loads(match.group(3))
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    return match.group(1) + match.group(2), value


def file_markdown(attrs: dict[str, Any]) -> str:
    name = str(attrs.get("name") or attrs.get("url") or "file")
    url = attrs.get("url") or ""
    label = _escape_markdown(name, True, False)
    link = f"[{label}]({_link_destination(url)})" if url else label
    if isinstance(attrs.get("size"), (int, float)):
        link += f" ({_human_size(attrs['size'])})"
    return f"{marker('file', attrs)}\n{link}"


def _math_content(content: dict[str, Any]) -> dict[str, Any]:
    latex = content.get("latex", "")
    if not isinstance(latex, str):
        raise BlockShapeError("math needs latex text")
    result = {"latex": latex.strip()}
    if content.get("mathml") is not None:
        if not isinstance(content["mathml"], str):
            raise BlockShapeError("mathml must be a string")
        result["mathml"] = content["mathml"]
    return result


def math_markdown(latex: str) -> str:
    # A blank line would end the paragraph that holds the formula.
    body = "\n".join(line for line in latex.split("\n") if line.strip())
    return f"$$\n{body}\n$$"


_MATH_RE = re.compile(r"^\$\$\s*(.*?)\s*\$\$$", re.DOTALL)


def math_from_markdown(source: str) -> str | None:
    match = _MATH_RE.match(source.strip())
    return match.group(1) if match else None


# --- Whole blocks -----------------------------------------------------------------------


def _text_prefix(block_type: str, attrs: dict[str, Any]) -> tuple[str, str]:
    """(first-line prefix, continuation prefix) of a text block's Markdown."""

    if block_type == "heading":
        level = attrs.get("level", 1)
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        return "#" * level + " ", ""
    if block_type in {"list", "bulletListItem", "numberedListItem", "checkListItem", "toggleListItem"}:
        ordered = block_type == "numberedListItem" or (block_type == "list" and attrs.get("ordered"))
        marker_text = "1. " if ordered else "- "
        checked = attrs.get("checked")
        if block_type == "checkListItem" or isinstance(checked, bool):
            return marker_text + ("[x] " if checked else "[ ] "), " " * len(marker_text)
        return marker_text, " " * len(marker_text)
    if block_type == "quote":
        return "> ", "> "
    return "", ""


def text_block_markdown(block_type: str, attrs: dict[str, Any], runs: object) -> str:
    if block_type in {"code", "codeBlock"}:
        code = inline_plain_text(runs)
        longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
        fence = "`" * (longest + 1)
        return f"{fence}{attrs.get('language') or ''}\n{code}\n{fence}"
    first, rest = _text_prefix(block_type, attrs)
    body = inline_to_markdown(runs, newline="\\\n" + rest)
    return (first + body).rstrip() if body or first.strip() else ""


def block_markdown(block_type: str, attrs: dict[str, Any], content: dict[str, Any]) -> str:
    """The Markdown the store keeps (``content.markdown``) for a structured block."""

    if block_type == "table":
        return table_markdown(attrs, content["blocknote"])
    if block_type == "image":
        return image_html(attrs)
    if block_type == "file":
        return file_markdown(attrs)
    if block_type == "math":
        return math_markdown(content.get("latex", ""))
    if block_type in {"columns", "column"}:
        return ""
    return text_block_markdown(block_type, attrs, content.get("blocknote"))


def plain_text(block_type: str | None, attrs: dict[str, Any], content: dict[str, Any]) -> str:
    """A block's searchable words (``blocks.text``)."""

    if block_type == "table":
        return table_plain_text(content["blocknote"])
    if block_type == "image":
        return "\n".join(str(attrs[key]) for key in ("alt", "caption") if attrs.get(key))
    if block_type == "file":
        return "\n".join(str(attrs[key]) for key in ("name", "caption") if attrs.get(key))
    if block_type == "math":
        return str(content.get("latex", ""))
    if block_type in {"columns", "column"}:
        return ""
    return inline_plain_text(content.get("blocknote"))


def normalize_block(
    block_type: str, attrs: dict[str, Any] | None, content: dict[str, Any] | None,
    text: str | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Validate a block about to be stored and fill in its derived fields.

    Structured types (table, image, file, math, columns, column) always get
    their Markdown and plain text derived here. A text block whose
    ``content.blocknote`` holds runs gets ``content.markdown`` (when missing or
    lacking the HTML for underline/colours/...) and ``text`` (when missing).
    Other blocks pass through unchanged. Raises :class:`BlockShapeError`.
    """

    attrs = dict(_dict(attrs, "attrs"))
    content = dict(_dict(content, "content"))
    if block_type == "table":
        attrs = _table_attrs(attrs)
        table = normalize_table(content.get("blocknote"))
        content = {"blocknote": table}
    elif block_type in {"image", "file"}:
        attrs = _image_attrs(attrs) if block_type == "image" else _file_attrs(attrs)
        # Other content keys are kept as the client sent them.
        content = {key: value for key, value in content.items() if key != "markdown"}
        content["markdown"] = block_markdown(block_type, attrs, content)
        # With no alt/caption/name, a caller's own text still describes the block.
        return attrs, content, plain_text(block_type, attrs, content) or (text or "")
    elif block_type == "math":
        if "latex" not in content and isinstance(attrs.get("latex"), str):
            content["latex"] = attrs.pop("latex")
        content = _math_content(content)
    elif block_type == "column":
        if "width" in attrs:
            width = _number(attrs["width"], "column width")
            if width is not None and width <= 0:
                raise BlockShapeError("column width must be positive")
            attrs["width"] = width
        return attrs, {}, ""
    elif block_type == "columns":
        return attrs, {}, ""
    else:
        runs = content.get("blocknote")
        check_loose_inline(runs)
        if block_type in TEXT_TYPES:
            attrs = _text_attrs(block_type, attrs)
        if isinstance(runs, list) and block_type in TEXT_TYPES:
            markdown = content.get("markdown")
            if not isinstance(markdown, str) or (
                has_extended_styles(runs) and not has_inline_mark_html(markdown)
            ):
                content["markdown"] = text_block_markdown(block_type, attrs, runs)
            if text is None:
                text = inline_plain_text(runs)
        return attrs, content, text if text is not None else ""
    content["markdown"] = block_markdown(block_type, attrs, content)
    return attrs, content, plain_text(block_type, attrs, content)


def block_fingerprint(text: str, content: object) -> str:
    """What a re-import compares: the block's Markdown when it has one, else its text."""

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    if isinstance(content, dict) and isinstance(content.get("markdown"), str):
        return content["markdown"]
    return text
