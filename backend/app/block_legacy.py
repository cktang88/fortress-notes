"""Reading block shapes from before the canonical model (docs/block-model.md §9-§10).

Two callers: the schema migration, which converts every stored block once, and
the transaction API, which still accepts the Mac app's current requests
(BlockNote type names and ``content.blocknote`` runs) until it sends canonical
operations. Nothing here is ever stored as is: everything comes out canonical.
"""

from __future__ import annotations

import re
from typing import Any

from . import block_schema as schema
from .block_markdown import BlockSpec, legalize, parse_markdown, spec
from .block_schema import BlockShapeError, text_node

# Older and BlockNote type names -> canonical names (``list`` depends on its attrs).
TYPE_NAMES = {
    "bulletListItem": "bullet_item", "toggleListItem": "bullet_item",
    "numberedListItem": "numbered_item", "checkListItem": "check_item",
    "codeBlock": "code", "thematic_break": "divider", "hr": "divider",
    "blockquote": "quote", "audio": "file", "video": "file", "pdf": "file",
    "mathPlaceholder": "math", "raw": "html", "columnList": "columns",
}

_MARK_NAMES = {
    "bold": "bold", "b": "bold", "italic": "italic", "i": "italic",
    "underline": "underline", "u": "underline", "strike": "strike", "s": "strike",
    "strikethrough": "strike", "code": "code", "sup": "sup", "superscript": "sup",
    "sub": "sub", "subscript": "sub", "color": "color", "textColor": "color",
    "highlight": "highlight", "backgroundColor": "highlight", "background": "highlight",
}
_REF_RE = re.compile(
    r"\[\[([A-Za-z0-9_-]{1,128})(?:\|[^\]\n]*)?\]\]"
    r"|\(\(\s*([A-Za-z0-9_-]{1,128})(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)\)"
)
# Attributes the old shapes used that the canonical type says another way.
_CONSUMED_ATTRS = {"ordered", "checked", "level", "language", "previewWidth", "latex"}
# BlockNote's default block props (no information).
_BLOCKNOTE_DEFAULTS = {"textAlignment": "left", "textColor": "default", "backgroundColor": "default"}


def canonical_type(name: str, attrs: dict[str, Any] | None = None) -> str:
    attrs = attrs or {}
    if name == "list":
        if isinstance(attrs.get("checked"), bool):
            return "check_item"
        return "numbered_item" if attrs.get("ordered") else "bullet_item"
    return TYPE_NAMES.get(name, name)


def _snake(name: str) -> str:
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", str(name)).lower()
    snake = re.sub(r"[^a-z0-9_]", "_", snake).strip("_")
    if not snake or not snake[0].isalpha():
        snake = "x_" + snake
    return snake[:64]


def _marks(*sources: object) -> dict[str, Any]:
    marks: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            name = _MARK_NAMES.get(key)
            if name is None or value in (None, False, 0, "", "default"):
                continue
            if name in schema.COLOR_MARKS:
                try:
                    marks[name] = schema.color_value(value, name)
                except BlockShapeError:
                    continue
            else:
                marks[name] = True
    if marks.get("sup") and marks.get("sub"):
        del marks["sub"]
    return marks


def _linkify(text: str, marks: dict[str, Any]) -> list[dict[str, Any]]:
    """``[[id|Title]]`` / ``((id "label"))`` typed as text become link nodes."""

    if marks.get("code") or ("[[" not in text and "((" not in text):
        return [text_node(text, marks)]
    nodes: list[dict[str, Any]] = []
    last = 0
    for match in _REF_RE.finditer(text):
        nodes.append(text_node(text[last:match.start()], marks))
        if match.group(1):
            nodes.append({"type": "docLink", "id": match.group(1)})
        else:
            nodes.append({"type": "blockRef", "id": match.group(2)})
        last = match.end()
    nodes.append(text_node(text[last:], marks))
    return nodes


def _href_ref(href: str) -> dict[str, Any] | None:
    match = _REF_RE.fullmatch(href.strip())
    if match is None:
        return None
    if match.group(1):
        return {"type": "docLink", "id": match.group(1)}
    return {"type": "blockRef", "id": match.group(2)}


def inline(value: object, *, plain: bool = False) -> list[dict[str, Any]]:
    """Canonical inline nodes from any older inline shape (lenient: bad marks are dropped).

    Reads canonical nodes, BlockNote runs (``styles``, link nodes), the compact
    importer form (``{"text", "b": 1, "color": ..., "link": ...}``) and plain
    strings; note and block links written as ``[[id|Title]]``/``((id))`` (as
    text or as a link's href) become ``docLink``/``blockRef`` nodes.
    """

    nodes = _inline(value)
    if plain:
        return schema.plain_content(nodes)
    return schema.normalize_inline(schema.merge_inline(nodes))


def _inline(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return _linkify(value, {})
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    nodes: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            nodes.extend(_linkify(item, {}))
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "text")
        if kind in ("docLink", "blockRef"):
            if isinstance(item.get("id"), str):
                nodes.append({"type": kind, "id": item["id"]})
            continue
        if kind == "link":
            href = str(item.get("href") or "").strip()
            ref = _href_ref(href)
            if ref is not None:
                nodes.append(ref)
                continue
            inner = [node for node in _inline(item.get("content", item.get("text", "")))
                     if node["type"] == "text"]
            if href and inner:
                nodes.append({"type": "link", "href": href, "content": schema.merge_inline(inner)})
            else:
                nodes.extend(inner)
            continue
        text = item.get("text")
        if not isinstance(text, str):
            if item.get("content") is not None:
                nodes.extend(_inline(item["content"]))
            continue
        compact = {key: val for key, val in item.items() if key not in ("type", "text", "styles", "marks")}
        marks = _marks(item.get("styles"), item.get("marks"), compact)
        href = item.get("href", item.get("link"))
        if isinstance(href, str) and href.strip():
            ref = _href_ref(href)
            if ref is not None:
                nodes.append(ref)
            else:
                nodes.append({"type": "link", "href": href.strip(), "content": [text_node(text, marks)]})
            continue
        nodes.extend(_linkify(text, marks))
    return [node for node in nodes if node["type"] != "text" or node["text"]]


def attrs(type_name: str, raw: object) -> dict[str, Any]:
    """Older attrs for a canonical type: renamed, extension keys snake_cased, junk dropped."""

    result: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return result
    declared = schema.block_type(type_name).attrs
    for key, value in raw.items():
        if value is None or _BLOCKNOTE_DEFAULTS.get(key) == value:
            continue
        if key in _CONSUMED_ATTRS and key not in declared:
            continue
        result[key if key in declared else _snake(key)] = value
    if type_name == "image" and "previewWidth" in raw:
        result.setdefault("width", raw["previewWidth"])
    if type_name == "callout" and "emoji" in result:
        result.setdefault("icon", result.pop("emoji"))
    if type_name == "heading":
        level = raw.get("level")
        result["level"] = level if isinstance(level, int) and 1 <= level <= 6 else 1
    if type_name == "check_item":
        result["checked"] = raw.get("checked") is True
    if type_name == "code" and isinstance(raw.get("language"), str) and raw["language"]:
        result["language"] = raw["language"]
    clean: dict[str, Any] = {}
    for key, value in result.items():
        try:
            clean.update(schema.normalize_attrs(type_name, {key: value}))
        except BlockShapeError:
            continue  # an unusable value (a bad colour, ...) is dropped
    return schema.normalize_attrs(type_name, clean)


def table(raw: object, table_attrs: dict[str, Any]) -> BlockSpec:
    """A BlockNote-style table JSON (``{"rows": [{"cells": [...]}], ...}``) as table blocks."""

    value = raw if isinstance(raw, dict) else {}
    result = dict(table_attrs)
    for old, new in (("columnWidths", "column_widths"), ("headerRows", "header_rows"),
                     ("headerCols", "header_cols")):
        item = value.get(old)
        if item is True:
            item = 1
        if item not in (None, False, [], 0):
            result[new] = item
    if isinstance(result.get("column_widths"), list) and all(
        width is None for width in result["column_widths"]
    ):
        del result["column_widths"]
    rows: list[BlockSpec] = []
    for row in value.get("rows") or []:
        cells = row.get("cells") if isinstance(row, dict) else row
        if not isinstance(cells, list):
            continue
        rows.append(spec("table_row", children=[_cell(cell) for cell in cells]))
    return spec("table", attrs("table", result), children=rows)


_CELL_PROPS = {
    "backgroundColor": "background", "background": "background", "textColor": "color",
    "color": "color", "textAlignment": "align", "align": "align", "colspan": "colspan",
    "rowspan": "rowspan",
}


def _cell(cell: object) -> BlockSpec:
    if not isinstance(cell, dict) or cell.get("type") not in (None, "tableCell"):
        cell = {"content": cell}
    props: dict[str, Any] = {}
    for key, value in (cell.get("props") or {}).items():
        props[_CELL_PROPS.get(key, key)] = value
    if props.get("align") == "left":
        del props["align"]
    for key in ("colspan", "rowspan"):
        if props.get(key) == 1:
            del props[key]
    children: list[BlockSpec] = []
    nodes = inline(cell.get("content"))
    if nodes:
        children.append(spec("paragraph", content=nodes))
    for nested in cell.get("blocks") or []:
        if isinstance(nested, dict):
            children.extend(convert(
                str(nested.get("type") or "paragraph"), nested.get("attrs"),
                nested.get("content"), "", nested_children=nested.get("children") or [],
            ))
    return spec("table_cell", attrs("table_cell", props), children=children)


def convert(
    type_name: str, raw_attrs: object, raw_content: object, text: str,
    nested_children: list | None = None,
) -> list[BlockSpec]:
    """One stored legacy block as canonical blocks (usually one; never none).

    The first returned block stands for the stored block (keeps its id and gets
    its stored children appended); any further blocks follow it. ``nested_children``
    are table-cell blocks of the old JSON shape.
    """

    old_attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
    content = raw_content if isinstance(raw_content, dict) else {}
    kind = canonical_type(type_name, old_attrs)
    runs = content.get("blocknote")
    markdown = content.get("markdown")
    result = _convert(kind, type_name, old_attrs, content, runs, markdown, text)
    if nested_children:
        for child in nested_children:
            if isinstance(child, dict):
                result[0].children.extend(convert(
                    str(child.get("type") or "paragraph"), child.get("attrs"), child.get("content"),
                    "", nested_children=child.get("children") or [],
                ))
    return result


def _convert(kind, type_name, old_attrs, content, runs, markdown, text) -> list[BlockSpec]:
    if kind == "table":
        if isinstance(runs, dict):
            return [table(runs, old_attrs)]
        return _from_markdown(kind, old_attrs, markdown, text)
    if kind in ("image", "file"):
        return [spec(kind, attrs(kind, old_attrs))]
    if kind == "math":
        latex = content.get("latex", old_attrs.get("latex", ""))
        payload = {"latex": latex if isinstance(latex, str) else ""}
        if isinstance(content.get("mathml"), str):
            payload["mathml"] = content["mathml"]
        return [spec("math", attrs("math", old_attrs), payload=payload)]
    if kind in ("columns", "column", "divider", "table_row", "table_cell"):
        return [spec(kind, attrs(kind, old_attrs))]
    if kind == "html":
        if isinstance(markdown, str):
            return _from_markdown(kind, old_attrs, markdown, text)
        return [spec("html", payload={"html": text})]
    if kind in ("quote", "callout"):
        if isinstance(runs, (list, str)):
            paragraph = spec("paragraph", content=inline(runs))
            return [spec(kind, attrs(kind, old_attrs), children=[paragraph])]
        return _from_markdown(kind, old_attrs, markdown, text)
    if kind not in schema.TYPES:
        nodes = inline(runs) if isinstance(runs, (list, str)) else inline(str(text or markdown or ""))
        extra = attrs("paragraph", old_attrs)
        return [spec("paragraph", {**extra, "legacy_type": _snake(type_name)}, nodes)]
    if isinstance(runs, (list, str)):
        return [spec(kind, attrs(kind, old_attrs), inline(runs, plain=kind == "code"))]
    return _from_markdown(kind, old_attrs, markdown, text)


def _from_markdown(kind: str, old_attrs: dict, markdown: object, text: str) -> list[BlockSpec]:
    """A Markdown-only stored block (imported before the canonical model)."""

    source = markdown if isinstance(markdown, str) else str(text or "")
    blocks = parse_markdown(source) if source.strip() else []
    if kind == "quote" and blocks and blocks[0].type == "quote":
        # The old importer stored the rest of the note inside a quote's
        # Markdown (duplicating the blocks after it): keep the quote only.
        blocks = blocks[:1]
    if not blocks:
        empty_kind = kind if kind in schema.TEXT_TYPES else "paragraph"
        blocks = [spec(empty_kind, attrs(empty_kind, old_attrs))]
    first = blocks[0]
    if first.type in schema.TEXT_TYPES and kind in schema.TEXT_TYPES and first.type != kind:
        if kind == "heading" or (kind in ("bullet_item", "numbered_item", "check_item")
                                 and first.type == "paragraph"):
            # The stored type wins over what its text happens to look like.
            first.attrs = schema.retype_attrs(first.type, kind, first.attrs)
            first.type = kind
            if kind == "code":
                first.content = schema.plain_content(first.content)
    extra = {key: value for key, value in attrs(first.type, old_attrs).items()
             if key not in schema.block_type(first.type).attrs}
    if extra or first.type in ("heading", "check_item"):
        merged = {**first.attrs, **extra}
        if first.type == "heading" and isinstance(old_attrs.get("level"), int):
            merged["level"] = attrs("heading", old_attrs)["level"]
        first.attrs = schema.normalize_attrs(first.type, merged)
    for block in blocks:
        block.id = None
    return legalize(blocks)
