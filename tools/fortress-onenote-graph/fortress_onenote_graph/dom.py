"""A small, forgiving DOM built with the standard library's ``html.parser``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                  "param", "source", "track", "wbr"})
# Closing one of these implicitly closes an open <p> / <li> (enough for Graph's output).
_P_CLOSERS = frozenset({"p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
                        "blockquote"})


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def elements(self) -> list["Node"]:
        return [child for child in self.children if isinstance(child, Node)]

    def find_all(self, tag: str) -> list["Node"]:
        found = []
        for child in self.elements():
            if child.tag == tag:
                found.append(child)
            found.extend(child.find_all(tag))
        return found

    def find(self, tag: str) -> "Node | None":
        found = self.find_all(tag)
        return found[0] if found else None

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    @property
    def style(self) -> dict[str, str]:
        return parse_style(self.attrs.get("style", ""))


def parse_style(value: str) -> dict[str, str]:
    style: dict[str, str] = {}
    for part in (value or "").split(";"):
        name, colon, val = part.partition(":")
        if colon and name.strip():
            style[name.strip().lower()] = val.strip()
    return style


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _P_CLOSERS and any(node.tag == "p" for node in self.stack[1:]):
            self._close("p")
        if tag == "li" and self.stack[-1].tag == "li":
            self._close("li")
        if tag in ("tr", "td", "th"):
            for closing in ({"tr": ("td", "th", "tr"), "td": ("td", "th"), "th": ("td", "th")}[tag]):
                if self.stack[-1].tag == closing:
                    self._close(closing)
        node = Node(tag, {name.lower(): (value if value is not None else "") for name, value in attrs},
                    parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        node = Node(tag, {name.lower(): (value if value is not None else "") for name, value in attrs},
                    parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in VOID:
            self._close(tag)

    def _close(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


_LENGTH_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(px|pt|in|cm|mm)?\s*$", re.IGNORECASE)


def length_px(value: str | None) -> float | None:
    """A CSS length in CSS px (96 dpi), or None."""

    if value is None:
        return None
    match = _LENGTH_RE.match(str(value))
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    return number * {"px": 1.0, "pt": 96 / 72, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4}[unit]
