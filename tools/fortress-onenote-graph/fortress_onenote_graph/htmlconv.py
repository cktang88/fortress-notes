"""Graph page HTML ("output HTML") -> manifest page items and blocks.

The output is the block vocabulary of tools/fortress-onenote/SCHEMA.md (format
version 1), so ``backend/app/onenote_import.py`` imports it like a converted
``.one`` file. What Graph returns is described in docs/onenote-import.md §2a and
Microsoft's "Input and output HTML" / "note tags" pages:

* every outline is an absolutely positioned ``div`` (``left``/``top``/``width``
  in px) directly under ``body``; floating images/attachments can be too;
* text is ``p``/``h1``–``h6``/``ul``/``ol``/``li``/``span`` with inline styles
  (``background-color`` is OneNote's highlighter);
* note tags are ``data-tag="to-do:completed,important"`` on the paragraph, or on
  a ``span`` inside a ``li``;
* tables nest, never merge cells; images carry ``data-fullres-src`` and
  ``alt`` (OneNote's OCR text); attachments and printouts are ``object``s,
  printout pages ``img data-options="printout" data-index=n``;
* ``includeIDs=true`` adds ``id="p:{GUID}{N}"`` to elements.

Equations (MathML) are kept as their text in a ``code`` block (Fortress has no
math blocks) and reported; ink is never requested.
"""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .dom import Node, length_px, parse

PX_PER_UNIT = 48.0
DEFAULT_FONT = "calibri"
DEFAULT_SIZE_PT = 11.0
MONOSPACE = ("consolas", "courier", "lucida console", "menlo", "monaco", "source code pro",
             "cascadia", "fira code", "fira mono", "jetbrains mono", "sf mono", "dejavu sans mono",
             "andale mono", "monospace")
CHECKABLE_TAGS = frozenset({
    "to-do", "to-do-priority-1", "to-do-priority-2", "discuss-with-person-a", "discuss-with-person-b",
    "discuss-with-manager", "send-in-email", "schedule-meeting", "call-back", "client-request",
})
TAG_SHAPES = {
    "to-do": "to_do_check_box", "important": "yellow_star", "question": "question_mark",
    "critical": "red_exclamation", "idea": "light_bulb", "password": "key", "contact": "contact",
    "address": "house", "phone-number": "phone", "highlight": "highlighter",
}
_GUID = r"\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?"
_ELEMENT_ID_RE = re.compile(r"^[a-z0-9]+:\{([0-9A-Fa-f-]{36})\}\{(\d+)\}$")
_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "div", "pre",
                         "blockquote", "img", "object", "iframe", "cite", "math", "tbody", "thead", "tfoot"})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]")
_SOURCE_RE = re.compile(r"^(?:Pasted from|Clipped from:?|From)\s*<?\s*(https?://\S+?)\s*>?\s*$", re.IGNORECASE)
_NAMED_COLORS = {"black": "#000000", "white": "#FFFFFF", "red": "#FF0000", "green": "#008000",
                 "blue": "#0000FF", "yellow": "#FFFF00", "gray": "#808080", "grey": "#808080",
                 "orange": "#FFA500", "purple": "#800080", "aqua": "#00FFFF", "lime": "#00FF00",
                 "fuchsia": "#FF00FF", "maroon": "#800000", "navy": "#000080", "olive": "#808000",
                 "silver": "#C0C0C0", "teal": "#008080"}


def color(value: str | None) -> str | None:
    """CSS colour -> ``#RRGGBB`` upper case, None for none/automatic/unknown."""

    if not value:
        return None
    value = value.strip().lower().replace("!important", "").strip()
    if value in _NAMED_COLORS:
        return _NAMED_COLORS[value]
    match = re.fullmatch(r"#([0-9a-f]{6})", value)
    if match:
        return "#" + match.group(1).upper()
    match = re.fullmatch(r"#([0-9a-f]{3})", value)
    if match:
        return "#" + "".join(ch * 2 for ch in match.group(1)).upper()
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if match:
        if match.group(4) is not None and float(match.group(4)) == 0:
            return None
        return "#" + "".join(f"{min(255, int(part)):02X}" for part in match.groups()[:3])
    return None


def clean_text(text: str) -> str:
    return _CONTROL_RE.sub("\ufffd", text)


def guid(value: str) -> str:
    return "{" + value.strip("{}").upper() + "}"


def onenote_link(href: str) -> dict[str, Any] | None:
    """The OneNote target of a link: ``onenote:`` URLs and OneNote-for-the-web
    links (``…wd=target(Section.one|{section}/Page|{page}/)``), else None."""

    lower = href.lower()
    if lower.startswith("onenote:"):
        rest = href[len("onenote:"):]
        section_file, _, fragment = rest.partition("#")
        page_id = re.search(r"page-id=" + _GUID, fragment, re.IGNORECASE)
        section_id = re.search(r"section-id=" + _GUID, fragment, re.IGNORECASE)
        object_id = re.search(r"object-id=" + _GUID + r"(?:&([0-9A-Fa-f]{1,8})(?=&|$))?", fragment, re.IGNORECASE)
        title = fragment.split("&section-id=")[0].split("&page-id=")[0] if fragment else ""
        info = {
            "page_id": guid(page_id.group(1)) if page_id else None,
            "section_id": guid(section_id.group(1)) if section_id else None,
            "object_id": guid(object_id.group(1)) if object_id else None,
            "object_ref": (f"{guid(object_id.group(1))}&{int(object_id.group(2), 16):X}"
                           if object_id and object_id.group(2) else None),
            "section_file": urllib.parse.unquote(section_file) or None,
            "page_title": urllib.parse.unquote(title) or None,
        }
        return info
    if lower.startswith("https://") and "wd=target" in lower:
        decoded = urllib.parse.unquote(urllib.parse.unquote(href))
        match = re.search(r"wd=target\((.*)\)", decoded)
        if not match:
            return None
        parts = match.group(1).split("|")
        section_id = re.search(_GUID, parts[1]) if len(parts) > 1 else None
        page_id = re.search(_GUID, parts[2]) if len(parts) > 2 else None
        if page_id is None and section_id is None:
            return None
        return {
            "page_id": guid(page_id.group(1)) if page_id else None,
            "section_id": guid(section_id.group(1)) if section_id else None,
            "object_id": None, "object_ref": None,
            "section_file": parts[0] or None,
            "page_title": (parts[2].rsplit("|", 1)[0].split("/")[0] if len(parts) > 2 else None) or None,
        }
    return None


def element_object_id(node: Node) -> str | None:
    """``id="p:{GUID}{N}"`` (includeIDs) -> ``{GUID}&N`` with N in hex, as the
    manifest writes ExGUIDs. Used as stable block keys."""

    match = _ELEMENT_ID_RE.match(node.attrs.get("id", ""))
    if not match:
        return None
    return f"{guid(match.group(1))}&{int(match.group(2)):X}"


def parse_tags(value: str | None) -> list[dict[str, Any]]:
    tags = []
    for raw in (value or "").split(","):
        raw = raw.strip().lower()
        if not raw:
            continue
        name, _, status = raw.partition(":")
        checkable = name in CHECKABLE_TAGS
        tags.append({
            "label": name.replace("-", " ").capitalize().replace("To do", "To Do"),
            "shape": TAG_SHAPES.get(name, name.replace("-", "_")),
            "checkable": checkable,
            "completed": checkable and status == "completed",
            "completed_at": None,
            "highlight_color": None,
            "text_color": None,
        })
    return tags


# --- rich text ------------------------------------------------------------------------------

_HARD_BREAK = "\u2028"  # internal: <br>, never collapsed


@dataclass
class _Style:
    values: dict[str, Any] = field(default_factory=dict)

    def with_(self, **changes: Any) -> "_Style":
        merged = dict(self.values)
        for key, value in changes.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return _Style(merged)


def _element_style(node: Node, style: _Style, heading: bool) -> _Style:
    css = node.style
    changes: dict[str, Any] = {}
    tag = node.tag
    if tag in ("b", "strong") or css.get("font-weight", "") in ("bold", "bolder", "600", "700", "800", "900"):
        changes["bold"] = True
    if css.get("font-weight", "") in ("normal", "400"):
        changes["bold"] = None
    if tag in ("i", "em") or css.get("font-style", "") == "italic":
        changes["italic"] = True
    if css.get("font-style", "") == "normal":
        changes["italic"] = None
    decoration = css.get("text-decoration", "") + " " + css.get("text-decoration-line", "")
    if tag == "u" or "underline" in decoration:
        changes["underline"] = True
    if tag in ("s", "strike", "del") or "line-through" in decoration:
        changes["strikethrough"] = True
    vertical = css.get("vertical-align", "")
    if tag == "sup" or vertical == "super":
        changes["superscript"] = True
    if tag == "sub" or vertical == "sub":
        changes["subscript"] = True
    if "color" in css:
        value = color(css["color"])
        changes["color"] = value if value not in (None, "#000000") else None
    if "background-color" in css or "background" in css:
        value = color(css.get("background-color") or css.get("background"))
        changes["highlight"] = value if value not in (None, "#FFFFFF") else None
    if not heading and "font-family" in css:
        family = css["font-family"].split(",")[0].strip().strip("'\"")
        changes["font"] = family if family and family.lower() != DEFAULT_FONT else None
    if not heading and "font-size" in css:
        px = length_px(css["font-size"])
        if px:
            pt = round(px * 72 / 96 * 2) / 2
            changes["font_size"] = pt if pt != DEFAULT_SIZE_PT else None
    if tag == "a" and node.attrs.get("href"):
        changes["link"] = node.attrs["href"]
    return style.with_(**changes)


class RichText:
    """Characters with their formatting; whitespace collapsed like a browser."""

    def __init__(self) -> None:
        self.chars: list[tuple[str, _Style]] = []
        self.math = 0  # equations kept as text

    def add(self, text: str, style: _Style) -> None:
        for ch in text:
            self.chars.append((ch, style))

    def is_empty(self) -> bool:
        return not any(ch.strip() and ch != _HARD_BREAK for ch, _ in self.chars)

    def all_monospace(self) -> bool:
        visible = [style for ch, style in self.chars if ch.strip() and ch != _HARD_BREAK]
        return bool(visible) and all(
            any(name in str(style.values.get("font", DEFAULT_FONT)).lower() for name in MONOSPACE)
            for style in visible)

    def build(self, heading: bool = False) -> tuple[str, list[dict[str, Any]]]:
        out: list[tuple[str, _Style]] = []
        for ch, style in self.chars:
            if ch == _HARD_BREAK:
                while out and out[-1][0] == " ":
                    out.pop()
                out.append(("\n", style))
            elif ch in " \t\n\r\f\v":
                if out and out[-1][0] not in (" ", "\n"):
                    out.append((" ", style))
            else:
                out.append((ch, style))  # a no-break space is kept (code indentation)
        while out and out[-1][0] in (" ", "\n"):
            out.pop()
        start = 0
        while start < len(out) and out[start][0] in (" ", "\n"):
            start += 1
        out = out[start:]
        out = [(" " if ch == "\u00a0" else ch, style) for ch, style in out]
        text = clean_text("".join(ch for ch, _ in out))
        marks: list[dict[str, Any]] = []
        keys = ["bold", "italic", "underline", "strikethrough", "superscript", "subscript", "highlight",
                "color", "font", "font_size", "link"]
        if heading:
            keys = [key for key in keys if key not in ("font", "font_size")]
        for key in keys:
            index = 0
            while index < len(out):
                value = out[index][1].values.get(key)
                if value is None or (out[index][0] in (" ", "\n") and key != "link"):
                    index += 1
                    continue
                end = index
                while end < len(out) and (out[end][1].values.get(key) == value
                                          or (out[end][0] in (" ", "\n") and key != "link"
                                              and end + 1 < len(out)
                                              and out[end + 1][1].values.get(key) == value)):
                    end += 1
                stop = end
                while key != "link" and stop > index + 1 and out[stop - 1][0] in (" ", "\n"):
                    stop -= 1
                marks.append(_mark(key, value, index, stop))
                index = end
        marks.sort(key=lambda mark: (mark["start"], mark["end"], mark["type"]))
        return text, marks


def _mark(key: str, value: Any, start: int, end: int) -> dict[str, Any]:
    mark: dict[str, Any] = {"start": start, "end": end}
    if key in ("highlight", "color"):
        mark.update(type=key, color=value)
    elif key == "font":
        mark.update(type="font", family=value)
    elif key == "font_size":
        mark.update(type="font_size", pt=value)
    elif key == "link":
        mark.update(type="link", href=value)
        target = onenote_link(value)
        if target is not None:
            mark["onenote"] = target
    else:
        mark["type"] = key
    return mark


def math_text(node: Node) -> str:
    """A MathML element as readable text (token elements joined)."""

    parts: list[str] = []

    def walk(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                if child.strip():
                    parts.append(child.strip())
            elif child.tag not in ("annotation", "annotation-xml"):
                walk(child)
    walk(node)
    return " ".join(parts)


# --- page conversion -----------------------------------------------------------------------


Resolver = Callable[[str, str, str | None], dict[str, Any]]
"""(url, kind "image"|"file", filename) -> {"asset", "data_status", "mime", "size"}"""


@dataclass
class PageResult:
    items: list[dict[str, Any]]
    source_url: str | None
    warnings: list[str]
    first_line: str | None
    object_ids: int = 0


class PageConverter:
    def __init__(self, resolve: Resolver):
        self.resolve = resolve
        self.warnings: list[str] = []
        self.used_ids: set[str] = set()
        self.source_url: str | None = None
        self.math = 0

    # entry point

    def convert(self, html: str) -> PageResult:
        root = parse(html)
        body = root.find("body") or root
        items: list[dict[str, Any]] = []
        loose: list[Node | str] = []

        def flush_loose() -> None:
            if any(not isinstance(node, str) or node.strip() for node in loose):
                blocks = self.blocks(loose, 0)
                if blocks:
                    items.append(self._item("outline", None, None, None, blocks))
            loose.clear()

        for child in body.children:
            if isinstance(child, Node) and _positioned(child):
                flush_loose()
                css = child.style
                x, y = _units(css.get("left")), _units(css.get("top"))
                width = _units(css.get("width")) or _units(child.attrs.get("width"))
                if child.tag in ("img", "object", "iframe"):
                    blocks = self.blocks([child], 0)
                    kind = {"img": "image", "object": "file", "iframe": "image"}[child.tag]
                else:
                    blocks = self.blocks(child.children, 0)
                    kind = "outline"
                if blocks:
                    items.append(self._item(kind, x, y, width, blocks))
            else:
                loose.append(child)
        flush_loose()
        for index, item in enumerate(items):
            item["id"] = f"i{index}"
            if self.source_url and item["kind"] == "outline":
                if any(block.get("is_source_citation") for block in item["blocks"]):
                    item["source_url"] = self.source_url
                    for block in item["blocks"]:
                        block["source_url"] = self.source_url
        if self.math:
            self.warnings.append(f"{self.math} equation(s) kept as plain text in a code block "
                                 "(Fortress has no math blocks)")
        return PageResult(items, self.source_url, self.warnings, _first_line(items),
                          object_ids=len(self.used_ids))

    def _item(self, kind: str, x: float | None, y: float | None, width: float | None,
              blocks: list[dict[str, Any]]) -> dict[str, Any]:
        est_width = width or 13.0
        return {
            "id": "", "kind": kind, "x": x, "y": y, "width": width, "width_set_by_user": False,
            "est_width": est_width, "est_height": round(_estimate_height(blocks, est_width), 3),
            "is_background": False, "source_url": None, "blocks": blocks,
        }

    # blocks

    def blocks(self, nodes: list[Node | str], indent: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        inline: list[Node | str] = []

        def flush() -> None:
            if inline:
                holder = Node("p", children=list(inline))
                out.extend(self.paragraph(holder, indent))
                inline.clear()

        for node in nodes:
            if isinstance(node, str) or node.tag not in _BLOCK_TAGS:
                inline.append(node)
                continue
            flush()
            tag = node.tag
            if tag in ("p", "cite", "pre") or re.fullmatch(r"h[1-6]", tag):
                out.extend(self.paragraph(node, indent))
            elif tag in ("ul", "ol"):
                out.extend(self.list_items(node, indent))
            elif tag == "table":
                out.append(self.table(node, indent))
            elif tag == "img":
                block = self.image(node, indent)
                if block:
                    out.append(block)
            elif tag == "object":
                out.append(self.file(node, indent))
            elif tag == "iframe":
                out.append(self.embed(node, indent))
            elif tag == "math":
                out.append(self.equation(node, indent))
            else:  # div, blockquote, tbody...
                out.extend(self.blocks(node.children, indent))
        flush()
        return _merge_code(out)

    def _common(self, block: dict[str, Any], node: Node, tags: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if tags:
            block["tags"] = tags
        align = node.style.get("text-align", "").lower()
        if align in ("center", "right"):
            block["align"] = align
        object_id = element_object_id(node)
        if object_id and object_id not in self.used_ids:
            self.used_ids.add(object_id)
            block["object_id"] = object_id
        return block

    def _rich(self, node: Node, heading: bool, skip: Callable[[Node], bool] | None = None,
              embedded: list[Node] | None = None) -> RichText:
        rich = RichText()

        def walk(current: Node, style: _Style) -> None:
            for child in current.children:
                if isinstance(child, str):
                    rich.add(child, style)
                    continue
                if skip is not None and skip(child):
                    continue
                if child.tag == "br":
                    rich.add(_HARD_BREAK, style)
                elif child.tag in ("img", "object", "iframe", "table"):
                    if embedded is not None:
                        embedded.append(child)
                elif child.tag == "math":
                    self.math += 1
                    rich.add(" " + math_text(child) + " ", style)
                elif child.tag in ("p", "div", "li") and rich.chars:
                    rich.add(_HARD_BREAK, style)
                    walk(child, _element_style(child, style, heading))
                else:
                    walk(child, _element_style(child, style, heading))

        base = _Style()
        if node.tag == "p" or node.tag == "pre" or node.tag == "li":
            base = _element_style(node, base, heading)
        walk(node, base)
        return rich

    def paragraph(self, node: Node, indent: int) -> list[dict[str, Any]]:
        heading = re.fullmatch(r"h([1-6])", node.tag)
        tags = parse_tags(node.attrs.get("data-tag"))
        for span in node.find_all("span"):
            if span.attrs.get("data-tag") and not tags:
                tags = parse_tags(span.attrs["data-tag"])
        # A paragraph that is only an equation -> a code block with its text.
        maths = node.find_all("math")
        if maths and not RichText_without(node, "math").strip():
            return [self.equation(math_node, indent) for math_node in maths]
        embedded: list[Node] = []
        rich = self._rich(node, bool(heading), embedded=embedded)
        blocks: list[dict[str, Any]] = []
        if not rich.is_empty():
            text, marks = rich.build(heading=bool(heading))
            if node.tag == "pre" or (not heading and rich.all_monospace()):
                blocks.append(self._common({"type": "code", "indent": indent, "text": text}, node, tags))
            else:
                checkable = next((tag for tag in tags if tag["checkable"]), None)
                is_cite = node.tag == "cite" or (_only_child_tag(node) == "cite")
                source = None if heading else _SOURCE_RE.match(text)
                if checkable is not None:
                    block = {"type": "check_item", "indent": indent, "checked": checkable["completed"],
                             "checked_at": None, "list": None, "text": text}
                elif source:
                    block = {"type": "paragraph", "indent": indent, "text": text, "is_source_citation": True}
                    self.source_url = self.source_url or source.group(1)
                elif heading:
                    block = {"type": "heading", "indent": indent, "level": int(heading.group(1)), "text": text}
                elif is_cite:
                    block = {"type": "quote", "indent": indent, "variant": "cite", "text": text}
                else:
                    block = {"type": "paragraph", "indent": indent, "text": text}
                if marks:
                    block["marks"] = _clip_marks(marks, len(text))
                blocks.append(self._common(block, node, tags))
        for child in embedded:
            blocks.extend(self.blocks([child], indent))
        return blocks

    def equation(self, node: Node, indent: int) -> dict[str, Any]:
        self.math += 1
        return self._common({"type": "code", "indent": indent, "text": math_text(node)}, node)

    def list_items(self, node: Node, indent: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        ordered = node.tag == "ol"
        try:
            restart = int(node.attrs["start"]) if "start" in node.attrs else None
        except ValueError:
            restart = None
        for child in node.elements():
            if child.tag in ("ul", "ol"):
                out.extend(self.list_items(child, indent + 1))
                continue
            if child.tag != "li":
                out.extend(self.blocks([child], indent))
                continue
            kind = (child.style.get("list-style-type") or node.style.get("list-style-type")
                    or ("decimal" if ordered else "disc")).lower()
            is_ordered = kind in ("decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman",
                                  "lower-latin", "upper-latin", "decimal-leading-zero")
            style = {"decimal": "decimal", "decimal-leading-zero": "decimal", "lower-alpha": "lower_alpha",
                     "lower-latin": "lower_alpha", "upper-alpha": "upper_alpha", "upper-latin": "upper_alpha",
                     "lower-roman": "lower_roman", "upper-roman": "upper_roman"}.get(kind, "bullet")
            marker = "%." if is_ordered else {"circle": "◦", "square": "▪", "none": ""}.get(kind, "•")
            info = {"ordered": is_ordered, "style": style, "marker": marker, "restart": restart}
            restart = None

            def nested(element: Node) -> bool:
                return element.tag in ("ul", "ol")

            tags = parse_tags(child.attrs.get("data-tag"))
            if not tags:
                for span in child.find_all("span"):
                    if span.attrs.get("data-tag") and not _inside_list(span, child):
                        tags = parse_tags(span.attrs["data-tag"])
                        break
            embedded: list[Node] = []
            rich = self._rich(child, False, skip=nested, embedded=embedded)
            if not rich.is_empty():
                text, marks = rich.build()
                checkable = next((tag for tag in tags if tag["checkable"]), None)
                if checkable is not None:
                    block = {"type": "check_item", "indent": indent, "checked": checkable["completed"],
                             "checked_at": None, "list": info, "text": text}
                else:
                    block = {"type": "list_item", "indent": indent, "list": info, "text": text}
                if marks:
                    block["marks"] = _clip_marks(marks, len(text))
                out.append(self._common(block, child, tags))
            for element in embedded:
                out.extend(self.blocks([element], indent + 1))
            for element in child.elements():
                if nested(element):
                    out.extend(self.list_items(element, indent + 1))
        return out

    def table(self, node: Node, indent: int) -> dict[str, Any]:
        rows = []
        for tr in _rows(node):
            cells = []
            for td in tr.elements():
                if td.tag not in ("td", "th"):
                    continue
                cells.append({"background": color(td.style.get("background-color")),
                              "blocks": self.blocks(td.children, 0), "_width": _units(td.style.get("width"))})
            rows.append({"cells": cells})
        col_count = max((len(row["cells"]) for row in rows), default=0)
        widths: list[float] = []
        for row in rows:
            candidate = [cell["_width"] for cell in row["cells"]]
            if len(candidate) == col_count and all(width for width in candidate):
                widths = [round(width, 3) for width in candidate]
                break
        for row in rows:
            for cell in row["cells"]:
                del cell["_width"]
            while len(row["cells"]) < col_count:  # ragged HTML: pad (OneNote tables are rectangular)
                row["cells"].append({"background": None, "blocks": []})
        css = node.style
        border = (css.get("border") or css.get("border-width") or "").lower()
        borders = bool(border) and not re.match(r"^\s*(0|0px|0pt|none|hidden)\b", border)
        block = {"type": "table", "indent": indent, "row_count": len(rows), "col_count": col_count,
                 "column_widths": widths, "borders_visible": borders, "rows": rows}
        return self._common(block, node)

    def image(self, node: Node, indent: int) -> dict[str, Any] | None:
        url = node.attrs.get("data-fullres-src") or node.attrs.get("src") or ""
        resource = self.resolve(url, "image", None) if url else {"asset": None, "data_status": "missing"}
        css = node.style
        width = _units(node.attrs.get("width")) or _units(css.get("width"))
        height = _units(node.attrs.get("height")) or _units(css.get("height"))
        printout = "printout" in (node.attrs.get("data-options") or "").lower()
        try:
            page_number = int(node.attrs["data-index"]) if printout and "data-index" in node.attrs else None
        except ValueError:
            page_number = None
        link = node.parent.attrs.get("href") if node.parent is not None and node.parent.tag == "a" else None
        alt = clean_text((node.attrs.get("alt") or "").strip()) or None
        block = {
            "type": "image", "indent": indent, "asset": resource.get("asset"),
            "data_status": resource.get("data_status", "missing"), "alt_text": alt,
            "width": width, "height": height, "hyperlink": link, "is_printout_page": printout,
            "printout_page_number": page_number, "is_background": False, "original_name": None,
            "original_asset": None,
        }
        return self._common(block, node, parse_tags(node.attrs.get("data-tag")))

    def file(self, node: Node, indent: int) -> dict[str, Any]:
        name = safe_name(node.attrs.get("data-attachment") or "") or "attachment"
        url = node.attrs.get("data") or ""
        resource = self.resolve(url, "file", name) if url else {"asset": None, "data_status": "missing"}
        mime = (node.attrs.get("type") or resource.get("mime") or "application/octet-stream").lower()
        extension = name.rsplit(".", 1)[1].lower() if "." in name else None
        media = "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "other"
        block = {
            "type": "file", "indent": indent, "asset": resource.get("asset"), "filename": name,
            "extension": extension, "mime": mime, "size": int(resource.get("size") or 0),
            "data_status": resource.get("data_status", "missing"), "media_kind": media, "icon_asset": None,
        }
        return self._common(block, node, parse_tags(node.attrs.get("data-tag")))

    def embed(self, node: Node, indent: int) -> dict[str, Any]:
        url = node.attrs.get("data-original-src") or node.attrs.get("src") or ""
        return self._common({"type": "embed", "indent": indent, "url": url, "thumbnail_asset": None,
                             "alt_text": None}, node)


def RichText_without(node: Node, tag: str) -> str:  # noqa: N802 - helper used above
    """Text of ``node`` outside ``tag`` elements."""

    return "".join(child if isinstance(child, str) else ("" if child.tag == tag else RichText_without(child, tag))
                   for child in node.children)


def safe_name(name: str) -> str:
    name = unicodedata.normalize("NFC", clean_text(name)).replace("/", "_").replace("\\", "_")
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name[:200]


def _positioned(node: Node) -> bool:
    css = node.style
    return css.get("position", "").lower() == "absolute" and ("left" in css or "top" in css)


def _units(value: str | None) -> float | None:
    px = length_px(value)
    return round(px / PX_PER_UNIT, 3) if px is not None and px >= 0 else None


def _rows(table: Node) -> list[Node]:
    rows: list[Node] = []
    for child in table.elements():
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("tbody", "thead", "tfoot"):
            rows.extend(row for row in child.elements() if row.tag == "tr")
    return rows


def _inside_list(node: Node, stop: Node) -> bool:
    current = node.parent
    while current is not None and current is not stop:
        if current.tag in ("ul", "ol", "li"):
            return True
        current = current.parent
    return False


def _only_child_tag(node: Node) -> str | None:
    elements = [child for child in node.children if not (isinstance(child, str) and not child.strip())]
    if len(elements) == 1 and isinstance(elements[0], Node):
        return elements[0].tag
    return None


def _clip_marks(marks: list[dict[str, Any]], length: int) -> list[dict[str, Any]]:
    return [mark for mark in marks if 0 <= mark["start"] < mark["end"] <= length]


def _merge_code(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive code paragraphs at the same indent become one code block."""

    out: list[dict[str, Any]] = []
    for block in blocks:
        previous = out[-1] if out else None
        if (previous is not None and block["type"] == "code" and previous["type"] == "code"
                and previous["indent"] == block["indent"] and not block.get("tags") and not previous.get("tags")):
            previous["text"] += "\n" + block["text"]
            continue
        out.append(block)
    return out


def _estimate_height(blocks: list[dict[str, Any]], width: float) -> float:
    """Rendered height in geometry units, deliberately on the low side (OneNote and
    Graph never store heights): 11 pt text ~ 0.4 units per line, ~14 characters
    per unit of width."""

    height = 0.0
    for block in blocks:
        kind = block["type"]
        if kind == "table":
            height += sum(max([_estimate_height(cell["blocks"], width / max(1, block["col_count"]))
                               for cell in row["cells"]] or [0.4]) for row in block["rows"])
        elif kind == "image":
            height += block.get("height") or 2.0
        elif kind in ("file", "embed"):
            height += 0.8
        else:
            text = block.get("text") or ""
            lines = sum(max(1, math.ceil(len(line) / max(1.0, width * 14))) for line in text.split("\n"))
            height += lines * (0.6 if kind == "heading" else 0.4)
    return height


def _first_line(items: list[dict[str, Any]]) -> str | None:
    for item in items:
        for block in item["blocks"]:
            text = (block.get("text") or "").strip()
            if text:
                return text.split("\n")[0][:200]
    return None


def page_title(html: str) -> str | None:
    root = parse(html)
    title = root.find("title")
    return " ".join(title.text().split()) if title is not None else None


def resource_urls(html: str) -> list[tuple[str, str]]:
    """``(url, kind)`` of every resource a page references (full-resolution images,
    attachments and printouts), in document order."""

    root = parse(html)
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()

    def walk(node: Node) -> None:
        for child in node.elements():
            url = None
            if child.tag == "img":
                url, kind = child.attrs.get("data-fullres-src") or child.attrs.get("src"), "image"
            elif child.tag == "object":
                url, kind = child.attrs.get("data"), "file"
            if url and url not in seen:
                seen.add(url)
                urls.append((url, kind))
            walk(child)
    walk(root)
    return urls
