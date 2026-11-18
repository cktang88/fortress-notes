"""The block type registry and inline content (docs/block-model.md §2-§3, §5).

Everything that knows what a block *is* lives here: the canonical types, their
category, attributes, payload and allowed children; the inline node form and its
normalization; and the plain ``text`` the server derives from a block. Pure
functions, no SQLite. ``schema_json()`` is what ``GET /api/block-schema`` serves.

Stored legacy shapes (BlockNote names and runs, ``[[id|Title]]`` links, table
JSON) are converted once by the schema 14 migration, via :mod:`app.block_legacy`;
the API accepts only this module's canonical form.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

SCHEMA_VERSION = 1


class BlockShapeError(ValueError):
    """A block, its attrs, content or nesting is not what the registry allows."""


# --- Attribute specs -----------------------------------------------------------------

_COLOR_RE = re.compile(
    r"^(?:#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,32}|(?:rgb|rgba|hsl|hsla)\([0-9\s.,%/]{1,48}\))$"
)
_EXTENSION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ATTRS_BYTES = 65_536


@dataclass(frozen=True)
class Attr:
    """One declared attribute: ``kind`` is int, number, bool, string, color, enum or widths."""

    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    default: Any = None
    exclusive_minimum: bool = False

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind}
        if self.minimum is not None:
            result["exclusive_minimum" if self.exclusive_minimum else "minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if self.default is not None:
            result["default"] = self.default
        return result


def _number(value: object, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise BlockShapeError(f"{name} must be a number")
    return int(value) if float(value).is_integer() else float(value)


def color_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not _COLOR_RE.match(value.strip()):
        raise BlockShapeError(f"{name} must be a CSS colour name or #hex value")
    return value.strip()


def _check_attr(spec: Attr, name: str, value: object) -> object:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise BlockShapeError(f"{name} must be true or false")
        return value
    if spec.kind == "string":
        if not isinstance(value, str):
            raise BlockShapeError(f"{name} must be a string")
        return value
    if spec.kind == "color":
        return color_value(value, name)
    if spec.kind == "enum":
        if value not in spec.choices:
            raise BlockShapeError(f"{name} must be one of {', '.join(spec.choices)}")
        return value
    if spec.kind == "widths":
        if not isinstance(value, list) or len(value) > 1000:
            raise BlockShapeError(f"{name} must be a list")
        return [None if item is None else _bounded(spec, name, _number(item, name)) for item in value]
    number = _number(value, name)
    if spec.kind == "int" and not isinstance(number, int):
        raise BlockShapeError(f"{name} must be a whole number")
    return _bounded(spec, name, number)


def _bounded(spec: Attr, name: str, number: int | float) -> int | float:
    if spec.minimum is not None:
        if spec.exclusive_minimum and number <= spec.minimum:
            raise BlockShapeError(f"{name} must be more than {spec.minimum}")
        if not spec.exclusive_minimum and number < spec.minimum:
            raise BlockShapeError(f"{name} must be at least {spec.minimum}")
    if spec.maximum is not None and number > spec.maximum:
        raise BlockShapeError(f"{name} must be at most {spec.maximum}")
    return number


# --- The registry ----------------------------------------------------------------------

TEXT, CONTAINER, ATOMIC = "text", "container", "atomic"


@dataclass(frozen=True)
class BlockType:
    name: str
    category: str
    # "rich" (marks and links), "plain" (unmarked text only) or None (no inline content).
    inline: str | None = None
    attrs: dict[str, Attr] = field(default_factory=dict)
    # Payload keys -> required?; None: the type has no payload.
    payload: dict[str, bool] | None = None
    children: frozenset[str] = frozenset()
    # Types in the same group can change into each other (None: never).
    change_group: str | None = None


_STR, _COLOR, _BOOL = Attr("string"), Attr("color"), Attr("bool")
_PX = Attr("number", minimum=0)

_TEXT_TYPES = ("paragraph", "heading", "bullet_item", "numbered_item", "check_item", "code")
FLOW: frozenset[str] = frozenset(
    _TEXT_TYPES + ("quote", "callout", "columns", "table", "divider", "image", "file", "math", "html")
)
_IN_COLUMN = FLOW - {"columns"}

_DEFINITIONS = (
    BlockType("paragraph", TEXT, "rich", children=FLOW, change_group=TEXT),
    BlockType("heading", TEXT, "rich", {"level": Attr("int", 1, 6, default=1)}, children=FLOW,
              change_group=TEXT),
    BlockType("bullet_item", TEXT, "rich", children=FLOW, change_group=TEXT),
    BlockType("numbered_item", TEXT, "rich", children=FLOW, change_group=TEXT),
    BlockType("check_item", TEXT, "rich", {"checked": Attr("bool", default=False)}, children=FLOW,
              change_group=TEXT),
    BlockType("code", TEXT, "plain", {"language": _STR}, change_group=TEXT),
    BlockType("quote", CONTAINER, children=FLOW, change_group="quote"),
    BlockType("callout", CONTAINER, attrs={"icon": _STR, "color": _COLOR}, children=FLOW,
              change_group="quote"),
    BlockType("columns", CONTAINER, children=frozenset({"column"})),
    BlockType("column", CONTAINER, attrs={"width": Attr("number", 0, exclusive_minimum=True)},
              children=_IN_COLUMN),
    BlockType("table", CONTAINER, attrs={
        "column_widths": Attr("widths", 0), "header_rows": Attr("int", 0),
        "header_cols": Attr("int", 0), "borders": _BOOL,
    }, children=frozenset({"table_row"})),
    BlockType("table_row", CONTAINER, children=frozenset({"table_cell"})),
    BlockType("table_cell", CONTAINER, attrs={
        "colspan": Attr("int", 1), "rowspan": Attr("int", 1), "background": _COLOR,
        "color": _COLOR, "align": Attr("enum", choices=("left", "center", "right", "justify")),
    }, children=_IN_COLUMN),
    BlockType("divider", ATOMIC),
    BlockType("image", ATOMIC, attrs={
        "url": _STR, "alt": _STR, "caption": _STR, "name": _STR, "width": _PX, "height": _PX,
    }),
    BlockType("file", ATOMIC, attrs={
        "url": _STR, "name": _STR, "mime": _STR, "caption": _STR, "size": _PX,
    }),
    BlockType("math", ATOMIC, payload={"latex": True, "mathml": False}),
    BlockType("html", ATOMIC, payload={"html": True}),
)
TYPES: dict[str, BlockType] = {definition.name: definition for definition in _DEFINITIONS}
TEXT_TYPES: frozenset[str] = frozenset(_TEXT_TYPES)
# Attribute names some type declares (dropped on a type change to a type without them).
_DECLARED = frozenset(name for definition in _DEFINITIONS for name in definition.attrs)


def block_type(name: object) -> BlockType:
    definition = TYPES.get(name) if isinstance(name, str) else None
    if definition is None:
        raise BlockShapeError(f"unknown block type: {name!r}")
    return definition


def allowed_children(parent_type: str | None) -> frozenset[str]:
    """The types ``parent_type`` may hold (None: the top level of a document)."""

    return FLOW if parent_type is None else block_type(parent_type).children


def check_nesting(parent_type: str | None, child_type: str) -> None:
    if child_type not in allowed_children(parent_type):
        where = "the top level" if parent_type is None else f"a {parent_type} block"
        raise BlockShapeError(f"a {child_type} block can't be inside {where}")


def check_type_change(old: str, new: str) -> None:
    if old == new:
        return
    old_group, new_group = block_type(old).change_group, block_type(new).change_group
    if old_group is None or old_group != new_group:
        raise BlockShapeError(f"a {old} block can't become a {new} block")


# --- Attributes ------------------------------------------------------------------------


def normalize_attrs(type_name: str, attrs: object) -> dict[str, Any]:
    """Validated attrs: declared ones checked, extension ones kept, defaults filled in."""

    definition = block_type(type_name)
    if attrs is None:
        attrs = {}
    if not isinstance(attrs, dict):
        raise BlockShapeError("attrs must be an object")
    result: dict[str, Any] = {}
    for name, value in attrs.items():
        if value is None:
            continue
        spec = definition.attrs.get(name)
        if spec is not None:
            result[name] = _check_attr(spec, name, value)
        elif not isinstance(name, str) or not _EXTENSION_KEY_RE.match(name):
            raise BlockShapeError(f"attribute names must be lower-case words: {name!r}")
        else:
            result[name] = value
    for name, spec in definition.attrs.items():
        if spec.default is not None and name not in result:
            result[name] = spec.default
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise BlockShapeError("attrs must be JSON") from exc
    if len(encoded) > _MAX_ATTRS_BYTES:
        raise BlockShapeError("attrs are too large")
    return result


def patch_attrs(current: dict[str, Any], patch: object) -> dict[str, Any]:
    """``current`` with ``patch`` applied: keys set, ``null`` deletes, others kept."""

    if patch is None:
        return dict(current)
    if not isinstance(patch, dict):
        raise BlockShapeError("attrs must be an object")
    result = dict(current)
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = value
    return result


def retype_attrs(old: str, new: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Attrs for a block changing type: the old type's own attributes go."""

    keep = block_type(new).attrs
    return {
        name: value for name, value in attrs.items()
        if name in keep or name not in _DECLARED
    }


# --- Inline content ----------------------------------------------------------------------

BOOLEAN_MARKS = ("bold", "italic", "underline", "strike", "code", "sup", "sub")
COLOR_MARKS = ("color", "highlight")
MARKS = BOOLEAN_MARKS + COLOR_MARKS
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _marks(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BlockShapeError("marks must be an object")
    marks: dict[str, Any] = {}
    for name, value in raw.items():
        if name in BOOLEAN_MARKS:
            if value is True:
                marks[name] = True
            elif value not in (False, None):
                raise BlockShapeError(f"mark {name} must be true")
        elif name in COLOR_MARKS:
            if value not in (None, ""):
                marks[name] = color_value(value, name)
        else:
            raise BlockShapeError(f"unknown mark: {name}")
    if marks.get("sup") and marks.get("sub"):
        raise BlockShapeError("text can't be both superscript and subscript")
    return marks


def text_node(text: str, marks: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = dict(marks)
    return node


_UNSAFE_SCHEME_RE = re.compile(r"^\s*(javascript|vbscript|data)\s*:", re.IGNORECASE)


def safe_href(href: object) -> bool:
    """A link target a client may open: not empty, not script or inline data."""

    if not isinstance(href, str) or not href.strip():
        return False
    # Browsers ignore control characters and whitespace inside a scheme.
    squeezed = re.sub(r"[\x00-\x20]", "", href)
    return not _UNSAFE_SCHEME_RE.match(squeezed)


def normalize_inline(value: object, *, plain: bool = False) -> list[dict[str, Any]]:
    """Canonical inline nodes (docs/block-model.md §3); ``plain`` for code blocks."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise BlockShapeError("content must be a list of inline nodes")
    nodes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BlockShapeError("an inline node must be an object")
        kind = item.get("type")
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise BlockShapeError("a text node needs text")
            marks = _marks(item.get("marks"))
            if text:
                nodes.append(text_node(text, None if plain else marks))
        elif plain:
            raise BlockShapeError("code blocks hold plain text only")
        elif kind == "link":
            href = item.get("href")
            if not isinstance(href, str) or not href.strip():
                raise BlockShapeError("a link needs an href")
            if not safe_href(href):
                raise BlockShapeError("a link can't run script or embed data")
            inner = normalize_inline(item.get("content"))
            if any(node["type"] != "text" for node in inner):
                raise BlockShapeError("a link holds only text")
            if inner:
                nodes.append({"type": "link", "href": href.strip(), "content": inner})
        elif kind in ("docLink", "blockRef"):
            target = item.get("id")
            if not isinstance(target, str) or not _ID_RE.match(target):
                raise BlockShapeError(f"a {kind} needs an id")
            nodes.append({"type": kind, "id": target})
        else:
            raise BlockShapeError(f"unknown inline node: {kind!r}")
    return merge_inline(nodes)


def merge_inline(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty text, merge neighbours with equal marks (and links with equal hrefs)."""

    merged: list[dict[str, Any]] = []
    for node in nodes:
        if node["type"] == "text" and not node["text"]:
            continue
        if node["type"] == "link" and not node["content"]:
            continue
        last = merged[-1] if merged else None
        if last and last["type"] == node["type"] == "text" and last.get("marks") == node.get("marks"):
            merged[-1] = {**last, "text": last["text"] + node["text"]}
        elif last and last["type"] == node["type"] == "link" and last["href"] == node["href"]:
            merged[-1] = {**last, "content": merge_inline(last["content"] + node["content"])}
        else:
            merged.append(node)
    return merged


Labeler = Callable[[str, str], str]


def _no_label(kind: str, target: str) -> str:
    return ""


def inline_text(nodes: list[dict[str, Any]] | None, label: Labeler = _no_label) -> str:
    """The words of inline content; links to notes/blocks read as ``label(kind, id)``."""

    parts: list[str] = []
    for node in nodes or []:
        kind = node.get("type")
        if kind == "text":
            parts.append(node["text"])
        elif kind == "link":
            parts.append(inline_text(node["content"]))
        elif kind in ("docLink", "blockRef"):
            parts.append(label(kind, node["id"]))
    return "".join(parts)


def references(nodes: list[dict[str, Any]] | None) -> tuple[set[str], set[str]]:
    """``(document ids, block ids)`` an inline content links to."""

    documents: set[str] = set()
    blocks: set[str] = set()
    for node in nodes or []:
        if node.get("type") == "docLink":
            documents.add(node["id"])
        elif node.get("type") == "blockRef":
            blocks.add(node["id"])
    return documents, blocks


# --- Whole blocks ---------------------------------------------------------------------------


def normalize_payload(type_name: str, payload: object) -> dict[str, Any] | None:
    spec = block_type(type_name).payload
    if spec is None:
        if payload not in (None, {}):
            raise BlockShapeError(f"a {type_name} block has no payload")
        return None
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise BlockShapeError("payload must be an object")
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in spec:
            raise BlockShapeError(f"unknown {type_name} payload field: {key}")
        if value is None:
            continue
        if not isinstance(value, str):
            raise BlockShapeError(f"payload.{key} must be a string")
        result[key] = value
    for key, required in spec.items():
        if required and key not in result:
            result[key] = ""
    if "latex" in result:
        result["latex"] = result["latex"].strip()
    return result


def normalize_content(type_name: str, content: object) -> list[dict[str, Any]] | None:
    definition = block_type(type_name)
    if definition.inline is None:
        if content not in (None, []):
            raise BlockShapeError(f"a {type_name} block has no inline content")
        return None
    return normalize_inline(content, plain=definition.inline == "plain")


def plain_content(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Content for a block becoming ``code``: the visible text, unmarked."""

    text = inline_text(nodes)
    return [text_node(text)] if text else []


@dataclass
class Block:
    """A canonical block's own data (no id, placement or children)."""

    type: str
    attrs: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None


def normalize_block(type_name: str, attrs: object = None, content: object = None,
                    payload: object = None) -> Block:
    """Validate one block's own data against the registry (raises BlockShapeError)."""

    return Block(
        type_name,
        normalize_attrs(type_name, attrs),
        normalize_content(type_name, content),
        normalize_payload(type_name, payload),
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def derive_text(block: Block, label: Labeler = _no_label) -> str:
    """The searchable plain text of one block (its own, not its children's)."""

    kind = block.type
    if block.content is not None:
        return inline_text(block.content, label)
    attrs = block.attrs
    if kind == "image":
        return "\n".join(str(attrs[key]) for key in ("alt", "caption") if attrs.get(key))
    if kind == "file":
        return "\n".join(str(attrs[key]) for key in ("name", "caption") if attrs.get(key))
    if kind == "math":
        return str((block.payload or {}).get("latex", ""))
    if kind == "html":
        return html_text(str((block.payload or {}).get("html", "")))
    return ""


_MEDIA_RE = re.compile(r"/media/([A-Za-z0-9_-]+\.[A-Za-z0-9]+)")


def media_names(attrs: dict[str, Any]) -> set[str]:
    """Uploaded files (``/media/<name>``) a block's attributes point at."""

    names: set[str] = set()
    for value in attrs.values():
        if isinstance(value, str) and "/media/" in value:
            names.update(_MEDIA_RE.findall(value))
    return names


def canonical_hash(block: Block) -> str:
    """What a re-import compares: a hash of the canonical type, attrs, content, payload."""

    encoded = json.dumps(
        [block.type, block.attrs, block.content, block.payload],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def schema_json() -> dict[str, Any]:
    """The registry as JSON (``GET /api/block-schema``)."""

    return {
        "version": SCHEMA_VERSION,
        "flow": sorted(FLOW),
        "top_level": sorted(FLOW),
        "types": {
            definition.name: {
                "category": definition.category,
                "inline": definition.inline,
                "attrs": {name: spec.describe() for name, spec in definition.attrs.items()},
                "payload": None if definition.payload is None else {
                    key: {"type": "string", "required": required}
                    for key, required in definition.payload.items()
                },
                "children": sorted(definition.children),
                "change_group": definition.change_group,
            }
            for definition in _DEFINITIONS
        },
        "inline": {
            "nodes": {
                "text": {"text": "string", "marks": "object (optional)"},
                "link": {"href": "string", "content": "text nodes"},
                "docLink": {"id": "document id"},
                "blockRef": {"id": "block id"},
            },
            "marks": {
                **{name: "true" for name in BOOLEAN_MARKS},
                **{name: "color" for name in COLOR_MARKS},
            },
            "exclusive_marks": [["sup", "sub"]],
        },
        "extension_attrs": _EXTENSION_KEY_RE.pattern,
    }
