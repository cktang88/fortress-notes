"""Living documents: notes that came from an outside source and stay in step with it.

Importers (OneNote, Markdown, ...) call :func:`upsert_document_from_source` once
per source page. The call is an idempotent upsert keyed by ``(source kind,
external id)``: the first call creates a note, later calls update that same note
in place, so its Fortress ID, links, backlinks and history survive.

Conflict policy (simple on purpose):

* **Blocks are matched by their source id** (``SourceBlock.external_id``; blocks
  without one get a content-derived id, so an unchanged block still matches).
* **Untouched blocks follow the source.** A block whose content is still what
  the last import wrote is updated to the source's new content.
* **Local edits win.** A block edited in Fortress keeps the local text. If the
  source changed it too, the source's version is recorded on the block as
  ``provenance.conflict`` (listed by ``document_info``), to settle later with
  :func:`resolve_block_conflict`.
* **Local additions stay** where they were, after the block they followed.
* **Local deletions stick.** A source block that was imported before and is no
  longer in the note (deleted, or moved to another note) is not re-created.
* **Blocks gone from the source** are deleted if untouched; an edited one is
  kept and marked ``provenance.source_removed``.
* **Structure follows the source**: source blocks take the source's order and
  nesting.
* **Title** follows the source unless renamed locally (then the source's title
  is recorded as a title conflict). **Folder** follows the source unless the note
  was moved locally.
* A note the user **trashed** is left alone (``skipped_deleted``); a purged one is
  imported afresh.

A whole re-import is one undoable step in the note's history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import block_types as _types
from . import ocr as _ocr
from . import provenance as _provenance
from .block_store import (
    _block_snapshot,
    _document_insert_position,
    _new_ulid,
    _now,
    _parse_blocks,
    _rebuild_document_assets,
    _rebuild_document_references,
    _replace_attrs,
    _shift_document_positions,
    apply_transaction,
    connection_scope,
    document_tree,
    initialize,
)
from .models import BlockOperation


@dataclass
class SourceBlock:
    """One block as the source has it.

    Either ``markdown`` -- the block's own Markdown (one paragraph, heading, list
    item, code fence, ...), with ``type`` defaulting to what it parses as -- or
    ``type`` plus structured ``content`` (spec.md §2a): rich text runs
    (``{"blocknote": [...]}``), a table (``{"blocknote": {"rows": ...}}``),
    math (``{"latex": ...}``); images/files/columns carry their data in
    ``attrs``. Nested content goes in ``children`` (a ``columns`` block's
    children are ``column`` blocks). ``provenance`` holds extra per-block fields
    such as ``source_path`` or ``extra``. :func:`source_block` builds one from a
    plain dict.
    """

    markdown: str = ""
    external_id: str | None = None
    type: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["SourceBlock"] = field(default_factory=list)
    created_at: str | None = None
    modified_at: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    # Structured content; Markdown and plain text are derived from it.
    content: dict[str, Any] | None = None
    # Searchable text, when it should differ from what is derived.
    text: str | None = None
    # Source ids this block had in earlier imports (e.g. a content hash before
    # the source exposed a stable id): a stored block with one of them is this
    # block, and takes ``external_id`` from now on.
    previous_external_ids: list[str] = field(default_factory=list)


@dataclass
class UpsertResult:
    document_id: str
    # "created" | "updated" | "unchanged" | "skipped_deleted"
    status: str
    revision: int = 0
    blocks_created: int = 0
    blocks_updated: int = 0
    blocks_deleted: int = 0
    # Source blocks not re-created because they were deleted/moved locally.
    blocks_skipped: int = 0
    # New conflicts found by this call: {"block_id"|"field", ...}.
    conflicts: list[dict[str, Any]] = field(default_factory=list)


# --- Building source blocks from plain dicts ------------------------------------------

_TEXT_ATTR_KEYS = ("level", "ordered", "checked", "checked_at", "language", "list_format", "tags")
_MEDIA_ATTR_KEYS = ("url", "alt", "caption", "width", "height", "name", "size", "mime")


def source_block(data: dict[str, Any] | str) -> SourceBlock:
    """A :class:`SourceBlock` from the plain dict an importer has at hand.

    Common keys: ``type``, ``id`` (the source's id for this block), ``attrs``,
    ``children``, ``created_at``, ``modified_at``, ``provenance``, ``markdown``.
    Per type:

    * text (``paragraph`` (default), ``heading``, ``list``, ``quote``, ``code``):
      ``inline`` -- runs, canonical or compact (``{"text", "b", "i", "u", "s",
      "sup", "sub", "code", "color", "highlight", "link"}``) -- or ``text``
      (plain); ``level``, ``ordered``, ``checked``, ``checked_at``, ``language``.
    * ``table``: ``rows`` (see :func:`table_content`), ``column_widths`` (CSS px
      or None), ``header_rows``, ``borders``.
    * ``columns``: ``columns: [{"width": 0.5, "blocks": [...], "id": ...}]``.
    * ``image``: ``url``, ``alt``, ``caption``, ``width``, ``height``, ``name``.
    * ``file``: ``url``, ``name``, ``size``, ``mime``.
    * ``math``: ``latex`` (and optional ``mathml``).

    A bare string is one block of Markdown.
    """

    if isinstance(data, str):
        return SourceBlock(markdown=data)
    if not isinstance(data, dict):
        raise _types.BlockShapeError("a source block must be a dict or a Markdown string")
    block_type = data.get("type")
    attrs = dict(data.get("attrs") or {})
    content = data.get("content")
    children = [source_block(child) for child in data.get("children") or []]
    if block_type == "table":
        if content is None:
            content = {"blocknote": table_content(
                data.get("rows") or [],
                column_widths=data.get("column_widths"),
                header_rows=data.get("header_rows", 0),
                header_cols=data.get("header_cols", 0),
            )}
        if "borders" in data:
            attrs["borders"] = data["borders"]
    elif block_type == "columns":
        if data.get("columns") is not None:
            children = [
                source_block({**column, "type": "column"}) for column in data["columns"]
            ]
    elif block_type == "column":
        if "width" in data:
            attrs["width"] = data["width"]
        children = [source_block(child) for child in data.get("blocks") or []] + children
    elif block_type in {"image", "file"}:
        attrs.update({key: data[key] for key in _MEDIA_ATTR_KEYS if key in data})
    elif block_type == "math":
        if content is None:
            content = {"latex": data.get("latex", "")}
            if data.get("mathml"):
                content["mathml"] = data["mathml"]
    else:
        attrs.update({key: data[key] for key in _TEXT_ATTR_KEYS if key in data})
        if content is None:
            if "inline" in data or "runs" in data:
                content = {"blocknote": _types.normalize_inline(data.get("inline", data.get("runs")))}
            elif "text" in data and "markdown" not in data:
                content = {"blocknote": _types.normalize_inline(data["text"])}
        if content is not None and block_type is None:
            block_type = "paragraph"
    return SourceBlock(
        markdown=str(data.get("markdown") or ""),
        external_id=data.get("id", data.get("external_id")),
        type=block_type,
        attrs=attrs,
        children=children,
        created_at=data.get("created_at"),
        modified_at=data.get("modified_at"),
        provenance=dict(data.get("provenance") or {}),
        content=content,
        previous_external_ids=[str(item) for item in data.get("previous_ids") or [] if item],
    )


def source_blocks(items: Iterable[dict[str, Any] | str]) -> list[SourceBlock]:
    """:func:`source_block` for each item."""

    return [source_block(item) for item in items]


def nested_block(data: dict[str, Any] | str) -> dict[str, Any]:
    """A block inside a table cell, from the same dicts :func:`source_block` takes."""

    children = data.get("children") if isinstance(data, dict) else None
    block = source_block({**data, "children": []} if isinstance(data, dict) else data)
    content = {key: value for key, value in (block.content or {}).items() if key != "markdown"}
    block_type = block.type
    attrs = dict(block.attrs)
    if block_type is None or (block_type in _types.TEXT_TYPES and "blocknote" not in content):
        parsed = _parse_blocks(block.markdown) if block.markdown else []
        if len(parsed) == 1:
            attrs = {**parsed[0].attrs, **attrs}
            block_type = block_type or parsed[0].type
        if len(parsed) == 1 and parsed[0].content is not None:
            content = {k: v for k, v in parsed[0].content.items() if k != "markdown"}
        else:
            inline = _LINE_MARKER.sub("", block.markdown)
            runs = _types.inline_from_markdown(inline) if inline else []
            content = {"blocknote": runs if runs is not None else _types.normalize_inline(inline)}
        block_type = block_type or "paragraph"
    result: dict[str, Any] = {"type": block_type, "attrs": attrs, "content": content}
    if children:
        result["children"] = [nested_block(child) for child in children]
    return _types.normalize_nested_block(result)


_LINE_MARKER = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)")


def table_content(
    rows: list[Any],
    *,
    column_widths: list[float | None] | None = None,
    header_rows: int = 0,
    header_cols: int = 0,
) -> dict[str, Any]:
    """A table's ``content.blocknote`` from plain rows.

    ``rows`` is a list of rows; a row is a list of cells (or ``{"cells": [...]}``).
    A cell is a string, a list of runs, or a dict with ``inline``/``text`` (its
    first line of text), ``blocks`` (more blocks, as :func:`source_block`
    dicts: lists, images, nested tables, ...), ``background``, ``color``,
    ``align`` (left/center/right/justify), ``colspan``, ``rowspan``.
    ``column_widths`` are CSS pixels (None: automatic).
    """

    table_rows = []
    for row in rows:
        cells = row.get("cells") if isinstance(row, dict) else row
        if not isinstance(cells, list):
            raise _types.BlockShapeError("every table row must be a list of cells")
        table_rows.append({"cells": [_table_cell(cell) for cell in cells]})
    return _types.normalize_table(
        {
            "columnWidths": column_widths or [],
            "headerRows": header_rows,
            "headerCols": header_cols,
            "rows": table_rows,
        }
    )


def _table_cell(cell: Any) -> Any:
    if not isinstance(cell, dict) or cell.get("type") == "tableCell":
        return cell
    props: dict[str, Any] = dict(cell.get("props") or {})
    for key, name in (("background", "backgroundColor"), ("backgroundColor", "backgroundColor"),
                      ("color", "textColor"), ("textColor", "textColor"),
                      ("align", "textAlignment"), ("textAlignment", "textAlignment"),
                      ("colspan", "colspan"), ("rowspan", "rowspan")):
        if cell.get(key) is not None:
            props[name] = cell[key]
    inline = cell.get("inline", cell.get("runs", cell.get("content", cell.get("text"))))
    result: dict[str, Any] = {"type": "tableCell", "props": props,
                              "content": _types.normalize_inline(inline)}
    if cell.get("blocks"):
        result["blocks"] = [nested_block(block) for block in cell["blocks"]]
    return result


# --- Folders ------------------------------------------------------------------


def ensure_folder_path(
    path: Path,
    source: str,
    segments: Iterable[tuple[str, str] | dict[str, str]],
    parent_id: str | None = None,
) -> str | None:
    """Find or create a folder chain by source ids; returns the last folder's id.

    ``segments`` run from the top, e.g. ``[(notebook_guid, "Work"),
    (section_guid, "Meetings")]``. A folder is found again by its source id even
    after the user renamed or moved it (both are kept); a folder still carrying
    the name the source gave it follows the source's renames. New top folders
    are created under ``parent_id`` (None: the top level).
    """

    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        for segment in segments:
            if isinstance(segment, dict):
                external_id, name = segment["external_id"], segment["name"]
            else:
                external_id, name = segment
            if not external_id:
                raise ValueError("every folder segment needs an external id")
            name = str(name).strip() or "Untitled"
            row = connection.execute(
                """SELECT id, name, source_name FROM folders
                    WHERE source_kind = ? AND source_external_id = ?""",
                (source, external_id),
            ).fetchone()
            if row is not None:
                if row["name"] == row["source_name"] and name != row["name"]:
                    connection.execute(
                        "UPDATE folders SET name = ?, source_name = ?, updated_at = ? WHERE id = ?",
                        (name, name, _now(), row["id"]),
                    )
                parent_id = row["id"]
                continue
            now = _now()
            folder_id = _new_ulid()
            position = connection.execute(
                "SELECT COUNT(*) FROM folders WHERE parent_id IS ?", (parent_id,)
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO folders(id, parent_id, name, position, created_at, updated_at,
                                       source_kind, source_external_id, source_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (folder_id, parent_id, name, position, now, now, source, external_id, name),
            )
            parent_id = folder_id
    return parent_id


# --- Upsert -------------------------------------------------------------------


@dataclass
class _Planned:
    """A source block resolved to its type and text, with a unique source id."""

    external_id: str
    type: str
    markdown: str
    attrs: dict[str, Any]
    children: list["_Planned"]
    created_at: str | None
    modified_at: str | None
    provenance: dict[str, Any]
    content: dict[str, Any] | None = None
    text: str | None = None
    previous_external_ids: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return _provenance.content_hash(self.type, self.markdown)

    @property
    def stored_content(self) -> dict[str, Any]:
        return self.content if self.content is not None else {"markdown": self.markdown}

    @property
    def stored_text(self) -> str:
        return self.text if self.text is not None else self.markdown


def _fingerprint(row: sqlite3.Row) -> str:
    """What a stored block's source hash was computed from."""

    return _types.block_fingerprint(row["text"], row["content_json"])


def _prepare(blocks: list[SourceBlock] | str) -> list[_Planned]:
    if isinstance(blocks, str):
        blocks = _from_markdown(blocks)
    seen: dict[str, int] = {}

    def convert(block: SourceBlock) -> _Planned:
        block_type, markdown, attrs = block.type, block.markdown, dict(block.attrs)
        content: dict[str, Any] | None = None
        text = block.text
        extra_children: list[SourceBlock] = []
        if block.content is None and block_type in _types.STRUCTURED_TYPES - {"columns", "column"} and markdown:
            # A table/image/... given as Markdown: read its structure from it.
            parsed = _parse_blocks(markdown)
            if len(parsed) != 1 or parsed[0].type != block_type or parsed[0].content is None:
                raise _types.BlockShapeError(f"markdown is not one {block_type} block")
            content, attrs = dict(parsed[0].content), {**parsed[0].attrs, **attrs}
        elif block.content is not None:
            content = dict(block.content)
        if content is not None or block_type in _types.STRUCTURED_TYPES:
            block_type = block_type or "paragraph"
            if content is not None and "markdown" not in content and markdown:
                content["markdown"] = markdown
            has_runs = isinstance((content or {}).get("blocknote"), list)
            attrs, content, derived = _types.normalize_block(
                block_type, attrs, content or {}, text
            )
            markdown = str(content.get("markdown", markdown))
            if block_type in _types.STRUCTURED_TYPES or has_runs:
                text = derived
            elif text is None:
                text = markdown
        elif block_type is None:
            parsed = _parse_blocks(markdown)
            if parsed:
                first = parsed[0]
                block_type = first.type
                attrs = {**first.attrs, **attrs}
                if len(parsed) == 1:
                    markdown = first.source
                    content, text = first.content, first.text
                    extra_children = _from_imported(first.children)
                elif block_type in _types.STRUCTURED_TYPES:
                    block_type = "raw"
            else:
                block_type = "paragraph"
        children = [convert(child) for child in [*extra_children, *block.children]]
        base_id = block.external_id or "~" + _provenance.content_hash(block_type, markdown)
        seen[base_id] = seen.get(base_id, 0) + 1
        external_id = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        return _Planned(
            external_id, block_type, markdown, attrs, children,
            block.created_at, block.modified_at, dict(block.provenance),
            content, text, list(block.previous_external_ids),
        )

    return [convert(block) for block in blocks]


def _from_markdown(body: str) -> list[SourceBlock]:
    return _from_imported(_parse_blocks(body))


def _from_imported(blocks) -> list[SourceBlock]:
    result = []
    for block in blocks:
        restored = block.provenance or {}
        result.append(
            SourceBlock(
                markdown=block.source,
                external_id=restored.get("external_id"),
                type=block.type,
                attrs=dict(block.attrs),
                children=_from_imported(block.children),
                content=dict(block.content) if block.content is not None else None,
                text=block.text,
                provenance={
                    key: value for key, value in restored.items()
                    if key in {"source_path", "source_url", "extra"}
                },
            )
        )
    return result


def _block_provenance(source: str, block: _Planned, previous: dict | None = None) -> dict:
    value = dict(previous or {})
    for key in ("conflict", "source_removed", "inherited"):
        value.pop(key, None)
    value.update(
        {
            key: item
            for key, item in _provenance.normalize(
                {**block.provenance, "source": source}
            ).items()
        }
    )
    value["source"] = source
    value["external_id"] = block.external_id
    value["source_hash"] = block.hash
    for key, item in (("source_created_at", block.created_at), ("source_modified_at", block.modified_at)):
        if item:
            value[key] = item
    return value


def upsert_document_from_source(
    path: Path,
    *,
    source: str,
    external_id: str,
    title: str,
    blocks: list[SourceBlock] | str,
    folder_id: str | None = None,
    folder_path: Iterable[tuple[str, str] | dict[str, str]] | None = None,
    created_at: str | None = None,
    modified_at: str | None = None,
    provenance: dict[str, Any] | None = None,
    importer: dict[str, str] | None = None,
    status: str = "rough",
) -> UpsertResult:
    """Create or update the note for one source page (see the module docstring).

    ``blocks`` is a list of :class:`SourceBlock` or a Markdown body.
    ``provenance`` adds document fields such as ``source_path`` ("Notebook ›
    Section › Page"), ``source_url`` or ``extra``. ``folder_path`` (source-id,
    name pairs) is resolved with :func:`ensure_folder_path`; otherwise
    ``folder_id`` is used (None: top level).
    """

    if not external_id:
        raise ValueError("external_id is required")
    title = " ".join(str(title).split()) or "Untitled"
    planned = _prepare(blocks)
    document_provenance = _provenance.normalize(
        {
            **(provenance or {}),
            "source": source,
            "external_id": external_id,
            "importer": importer,
            "source_created_at": created_at,
            "source_modified_at": modified_at,
        }
    )
    if folder_path is not None:
        folder_id = ensure_folder_path(path, source, folder_path)
    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        if folder_id is not None and connection.execute(
            "SELECT 1 FROM folders WHERE id = ?", (folder_id,)
        ).fetchone() is None:
            raise KeyError("folder not found")
        row = connection.execute(
            "SELECT * FROM documents WHERE source_kind = ? AND source_external_id = ?",
            (source, external_id),
        ).fetchone()
        if row is None:
            return _create(
                connection, title, status, planned, folder_id,
                created_at, modified_at, document_provenance,
            )
        if row["deleted_at"] is not None:
            return UpsertResult(row["id"], "skipped_deleted", row["revision"])
        return _update(connection, row, source, title, planned, folder_id, modified_at,
                       document_provenance)


def mark_documents_source_removed(
    path: Path, source: str, document_ids: Iterable[str], *, at: str | None = None
) -> list[dict[str, Any]]:
    """Record that the source no longer has these notes; nothing is deleted.

    Sets ``provenance.source_removed: true`` and ``source_removed_at`` (kept from
    an earlier run if already marked). The next upsert of a note clears both.
    Returns ``{"document_id", "title", "source_path", "source_removed_at",
    "newly_removed"}`` per note of ``source`` that is not in the trash.
    """

    now = at or _now()
    result: list[dict[str, Any]] = []
    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        for document_id in document_ids:
            row = connection.execute(
                """SELECT id, title, provenance_json FROM documents
                    WHERE id = ? AND source_kind = ? AND deleted_at IS NULL""",
                (document_id, source),
            ).fetchone()
            if row is None:
                continue
            value = _provenance.loads(row["provenance_json"])
            newly = not value.get("source_removed")
            if newly:
                value["source_removed"] = True
                value["source_removed_at"] = now
                connection.execute(
                    "UPDATE documents SET provenance_json = ? WHERE id = ?",
                    (_provenance.dumps(value), document_id),
                )
            result.append({
                "document_id": row["id"],
                "title": row["title"],
                "source_path": value.get("source_path"),
                "source_removed_at": value.get("source_removed_at") or now,
                "newly_removed": newly,
            })
    return result


def _flatten(blocks: list[_Planned]) -> list[_Planned]:
    result = []
    for block in blocks:
        result.append(block)
        result.extend(_flatten(block.children))
    return result


def _insert_planned(
    connection: sqlite3.Connection,
    document_id: str,
    source: str,
    block: _Planned,
    block_id: str,
    parent_id: str | None,
    position: int,
    default_created: str,
    revision: int | None,
) -> None:
    created = block.created_at or default_created
    connection.execute(
        """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
               content_json, text, created_at, updated_at, provenance_json, revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            block_id, document_id, parent_id, position, block.type,
            json.dumps(block.attrs), json.dumps(block.stored_content),
            block.stored_text, created, block.modified_at or created,
            _provenance.dumps(_block_provenance(source, block)), revision,
        ),
    )
    _replace_attrs(connection, block_id, block.attrs)


def _create(
    connection: sqlite3.Connection,
    title: str,
    status: str,
    planned: list[_Planned],
    folder_id: str | None,
    created_at: str | None,
    modified_at: str | None,
    document_provenance: dict[str, Any],
) -> UpsertResult:
    now = _now()
    document_id = _new_ulid()
    created = created_at or now
    position = _document_insert_position(connection, folder_id, None)
    connection.execute(
        """INSERT INTO documents(id, title, status, folder_id, position,
               created_at, updated_at, provenance_json, source_kind, source_external_id,
               sync_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            document_id, title,
            status if status in {"rough", "polished"} else "rough",
            folder_id, position, created, modified_at or created,
            _provenance.dumps({**document_provenance, "imported_at": now}),
            document_provenance["source"], document_provenance["external_id"],
            json.dumps(
                {
                    "title": title,
                    "folder_id": folder_id,
                    "block_ids": [block.external_id for block in _flatten(planned)],
                    "synced_at": now,
                    "checked_at": now,
                }
            ),
        ),
    )
    count = 0

    def insert(blocks: list[_Planned], parent_id: str | None) -> None:
        nonlocal count
        for index, block in enumerate(blocks):
            block_id = _new_ulid()
            _insert_planned(
                connection, document_id, document_provenance["source"], block,
                block_id, parent_id, index, created, None,
            )
            count += 1
            insert(block.children, block_id)

    insert(planned, None)
    _rebuild_document_references(connection, document_id)
    _rebuild_document_assets(connection, document_id)
    return UpsertResult(document_id, "created", 0, blocks_created=count)


def _update(
    connection: sqlite3.Connection,
    document: sqlite3.Row,
    source: str,
    title: str,
    planned: list[_Planned],
    folder_id: str | None,
    modified_at: str | None,
    document_provenance: dict[str, Any],
) -> UpsertResult:
    now = _now()
    document_id = document["id"]
    revision = int(document["revision"]) + 1
    sync = _provenance.loads(document["sync_json"])
    previously_synced = set(sync.get("block_ids") or [])
    rows = connection.execute(
        """SELECT rowid AS row_number, id, parent_id, position, type, text, attrs_json,
                  content_json, provenance_json
             FROM blocks WHERE document_id = ? ORDER BY parent_id, position""",
        (document_id,),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    stored_provenance = {row["id"]: _provenance.loads(row["provenance_json"]) for row in rows}
    by_external: dict[str, str] = {}
    for row in rows:
        value = stored_provenance[row["id"]]
        external = value.get("external_id")
        if external and value.get("source") == source and external not in by_external:
            by_external[external] = row["id"]
    current_children: dict[str | None, list[str]] = {}
    for row in sorted(rows, key=lambda item: item["position"]):
        current_children.setdefault(row["parent_id"], []).append(row["id"])

    result = UpsertResult(document_id, "unchanged", int(document["revision"]))
    layout: dict[str | None, list[str]] = {}
    matched: set[str] = set()
    inserts: list[tuple[str, str | None, _Planned]] = []
    content_updates: dict[str, _Planned] = {}
    provenance_updates: dict[str, dict] = {}

    primary_ids = {block.external_id for block in _flatten(planned)}

    def lookup(block: _Planned) -> str | None:
        block_id = by_external.get(block.external_id)
        if block_id is not None:
            return block_id
        for previous in block.previous_external_ids:
            # A stored id some other source block still uses is that block's.
            candidate = by_external.get(previous)
            if previous not in primary_ids and candidate is not None and candidate not in matched:
                return candidate
        return None

    def plan(blocks: list[_Planned], parent_id: str | None) -> None:
        order: list[str] = []
        for block in blocks:
            block_id = lookup(block)
            if block_id is not None and block_id not in matched:
                matched.add(block_id)
                row = by_id[block_id]
                old = stored_provenance[block_id]
                current_hash = _provenance.content_hash(row["type"], _fingerprint(row))
                base_hash = old.get("source_hash")
                new = _block_provenance(source, block, old)
                if current_hash == base_hash or current_hash == block.hash:
                    # Untouched (or the source caught up with the local edit).
                    new.pop("dismissed_hash", None)
                    if (
                        current_hash != block.hash
                        or json.loads(row["attrs_json"]) != block.attrs
                        or (
                            block.content is not None
                            and json.loads(row["content_json"]) != block.content
                        )
                    ):
                        content_updates[block_id] = block
                else:
                    # Edited here: keep the local text, remember what the source has.
                    new["source_hash"] = base_hash
                    conflict = old.get("conflict") or {}
                    if block.hash not in {base_hash, old.get("dismissed_hash")}:
                        if conflict.get("source_hash") == block.hash:
                            new["conflict"] = conflict
                        else:
                            new["conflict"] = {
                                "detected_at": now,
                                "source_hash": block.hash,
                                "source_type": block.type,
                                "source_markdown": block.markdown,
                                **(
                                    {"source_content": block.content, "source_attrs": block.attrs,
                                     "source_text": block.stored_text}
                                    if block.content is not None else {}
                                ),
                                **({"source_modified_at": block.modified_at} if block.modified_at else {}),
                            }
                            result.conflicts.append({"block_id": block_id, **new["conflict"]})
                    if base_hash is None:
                        new.pop("source_hash", None)
                if new != old:
                    provenance_updates[block_id] = new
                order.append(block_id)
                plan(block.children, block_id)
            elif block.external_id in previously_synced or any(
                previous in previously_synced and previous not in primary_ids
                for previous in block.previous_external_ids
            ):
                result.blocks_skipped += 1
            else:
                block_id = _new_ulid()
                inserts.append((block_id, parent_id, block))
                order.append(block_id)
                plan(block.children, block_id)
        layout[parent_id] = order

    plan(planned, None)

    # Existing blocks the source no longer has (or never had).
    deletable: set[str] = set()

    def can_delete(block_id: str) -> bool:
        value = stored_provenance[block_id]
        if value.get("source") != source or not value.get("external_id"):
            return False  # added here
        row = by_id[block_id]
        if _provenance.content_hash(row["type"], _fingerprint(row)) != value.get("source_hash"):
            return False  # edited here
        return all(
            child in matched or can_delete(child)
            for child in current_children.get(block_id, [])
        )

    for row in rows:
        if row["id"] not in matched and can_delete(row["id"]):
            deletable.add(row["id"])
    kept_local = [row["id"] for row in rows if row["id"] not in matched and row["id"] not in deletable]
    for block_id in kept_local:
        value = stored_provenance[block_id]
        if value.get("source") == source and value.get("external_id") and not value.get("source_removed"):
            provenance_updates[block_id] = {**value, "source_removed": True}

    # Local blocks stay after the sibling they followed.
    final: dict[str | None, list[str]] = {key: list(value) for key, value in layout.items()}
    kept_set = set(kept_local)
    for parent_id in {by_id[block_id]["parent_id"] for block_id in kept_local}:
        siblings = current_children.get(parent_id, [])
        order = final.setdefault(parent_id, [])
        head = 0
        for index, block_id in enumerate(siblings):
            if block_id not in kept_set:
                continue
            anchor = next(
                (sibling for sibling in reversed(siblings[:index]) if sibling in order), None
            )
            if anchor is None:
                order.insert(head, block_id)
                head += 1
            else:
                order.insert(order.index(anchor) + 1, block_id)

    placement = {
        block_id: (parent_id, position)
        for parent_id, order in final.items()
        for position, block_id in enumerate(order)
    }
    moved = [
        block_id
        for block_id in by_id
        if block_id not in deletable
        and placement.get(block_id) != (by_id[block_id]["parent_id"], by_id[block_id]["position"])
    ]
    blocks_changed = bool(inserts or content_updates or provenance_updates or deletable or moved)

    if blocks_changed:
        roots_before = current_children.get(None, [])
        before = [
            {"id": block_id, "snapshot": _block_snapshot(connection, block_id)}
            for block_id in roots_before
        ]
        # Park every block on a unique negative position, add the new blocks (a
        # parent always before its children), then put the old ones in place.
        connection.execute(
            "UPDATE blocks SET position = -rowid WHERE document_id = ?", (document_id,)
        )
        for block_id, parent_id, block in inserts:
            _insert_planned(
                connection, document_id, source, block, block_id,
                parent_id, placement[block_id][1], now, revision,
            )
        for block_id in by_id:
            if block_id in deletable:
                continue
            parent_id, position = placement[block_id]
            connection.execute(
                "UPDATE blocks SET parent_id = ?, position = ? WHERE id = ?",
                (parent_id, position, block_id),
            )
        for block_id, block in content_updates.items():
            connection.execute(
                """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, text = ?,
                          updated_at = ?, revision = ? WHERE id = ?""",
                (
                    block.type, json.dumps(block.attrs), json.dumps(block.stored_content),
                    block.stored_text, block.modified_at or now, revision, block_id,
                ),
            )
            _replace_attrs(connection, block_id, block.attrs)
        for block_id, value in provenance_updates.items():
            connection.execute(
                "UPDATE blocks SET provenance_json = ? WHERE id = ?",
                (_provenance.dumps(value), block_id),
            )
        # Matched children were moved out above; what cascades is deletable too.
        for block_id in deletable:
            connection.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
        roots_after = final.get(None, [])
        after = [
            {"id": block_id, "snapshot": _block_snapshot(connection, block_id)}
            for block_id in roots_after
        ]
        # Two patches make the whole re-import one undo step: the first removes
        # the old top-level blocks, the second puts the new ones in place.
        patches = [
            {"before": before, "after": [{"id": item["id"], "snapshot": None} for item in before]},
            {"before": [{"id": item["id"], "snapshot": None} for item in after], "after": after},
        ]
        connection.execute(
            "DELETE FROM document_history WHERE document_id = ? AND undone = 1", (document_id,)
        )
        connection.execute(
            """INSERT INTO document_history(document_id, revision, patches_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (document_id, revision, json.dumps(patches), now),
        )
        connection.execute(
            """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
               VALUES (?, NULL, ?, ?)""",
            (document_id, json.dumps({"operation": "source_sync", "source": source}), now),
        )
        _rebuild_document_references(connection, document_id)
        _rebuild_document_assets(connection, document_id)
        result.blocks_created = len(inserts)
        result.blocks_updated = len(content_updates)
        result.blocks_deleted = len(deletable)
        result.revision = revision

    document_changed = blocks_changed
    # Title: follows the source unless someone renamed the note here.
    new_title = document["title"]
    synced_title = sync.get("title")
    if title != synced_title:
        if document["title"] == synced_title or synced_title is None and document["title"] == title:
            new_title = title
            sync["title"] = title
            sync.pop("title_conflict", None)
        elif (sync.get("title_conflict") or {}).get("source_title") != title:
            sync["title_conflict"] = {"source_title": title, "detected_at": now}
            result.conflicts.append({"field": "title", **sync["title_conflict"]})
    if new_title != document["title"]:
        document_changed = True
    # Folder: follows the source unless someone moved the note here.
    new_folder = document["folder_id"]
    if folder_id != sync.get("folder_id", document["folder_id"]):
        if document["folder_id"] == sync.get("folder_id", document["folder_id"]):
            new_folder = folder_id
        sync["folder_id"] = folder_id
    if new_folder != document["folder_id"]:
        document_changed = True
        _shift_document_positions(connection, document["folder_id"], document["position"] + 1, -1)
        position = _document_insert_position(connection, new_folder, None, exclude_id=document_id)
        connection.execute(
            "UPDATE documents SET folder_id = ?, position = ? WHERE id = ?",
            (new_folder, position, document_id),
        )
    old_provenance = _provenance.loads(document["provenance_json"])
    new_provenance = {
        # A page the source has again is no longer "removed in the source".
        **{key: value for key, value in old_provenance.items()
           if key not in {"backfilled", "source_removed", "source_removed_at"}},
        **document_provenance,
    }
    new_provenance.setdefault("imported_at", now)
    comparable = lambda value: {k: v for k, v in value.items() if k != "synced_at"}  # noqa: E731
    if comparable(new_provenance) != comparable(old_provenance):
        document_changed = True
    if document_changed:
        new_provenance["synced_at"] = now
        sync["synced_at"] = now
        updated_at = max(str(document["updated_at"]), modified_at or now) if blocks_changed or new_title != document["title"] else document["updated_at"]
        connection.execute(
            """UPDATE documents SET title = ?, title_auto = CASE WHEN ? THEN 0 ELSE title_auto END,
                      updated_at = ?, revision = ?, provenance_json = ?
                WHERE id = ?""",
            (
                new_title, new_title != document["title"], updated_at,
                revision if blocks_changed else document["revision"],
                _provenance.dumps(new_provenance), document_id,
            ),
        )
        result.status = "updated"
    sync["block_ids"] = [block.external_id for block in _flatten(planned)]
    sync["checked_at"] = now
    connection.execute(
        "UPDATE documents SET sync_json = ? WHERE id = ?", (json.dumps(sync), document_id)
    )
    return result


# --- Metadata -----------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


_DOCUMENT_LINK_RE = re.compile(r"\[\[[A-Za-z0-9_-]+(?:\|([^\]]+))?\]\]")
_BLOCK_LINK_RE = re.compile(r"\(\(\s*[A-Za-z0-9_-]+(?:\s+[\"']([^\"']*)[\"'])?\s*\)\)")


def _readable(text: str) -> str:
    """Block text as a reader sees it: links count as their labels, not their IDs."""

    if "[[" in text:
        text = _DOCUMENT_LINK_RE.sub(lambda match: match.group(1) or "", text)
    if "((" in text:
        text = _BLOCK_LINK_RE.sub(lambda match: match.group(1) or "", text)
    return text


def document_info(path: Path, document_id: str) -> dict[str, Any]:
    """Everything an info panel shows about one note, in a fixed number of queries."""

    initialize(path)
    with connection_scope(path) as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        folders = connection.execute(
            """WITH RECURSIVE chain(id, parent_id, name, depth) AS (
                   SELECT id, parent_id, name, 0 FROM folders WHERE id = ?
                   UNION ALL
                   SELECT folders.id, folders.parent_id, folders.name, chain.depth + 1
                     FROM folders JOIN chain ON folders.id = chain.parent_id
               ) SELECT id, name FROM chain ORDER BY depth DESC""",
            (document["folder_id"],),
        ).fetchall()
        blocks = connection.execute(
            """SELECT id, type, text, content_json, created_at, updated_at, provenance_json
                 FROM blocks WHERE document_id = ?""",
            (document_id,),
        ).fetchall()
        outgoing_documents = connection.execute(
            """SELECT COUNT(DISTINCT refs.target_document_id)
                 FROM document_refs AS refs
                 JOIN blocks ON blocks.id = refs.source_block_id
                 JOIN documents AS target ON target.id = refs.target_document_id
                WHERE blocks.document_id = ? AND target.deleted_at IS NULL""",
            (document_id,),
        ).fetchone()[0]
        outgoing_blocks = connection.execute(
            """SELECT COUNT(DISTINCT refs.target_block_id)
                 FROM block_refs AS refs
                 JOIN blocks AS source ON source.id = refs.source_block_id
                 JOIN blocks AS target ON target.id = refs.target_block_id
                 JOIN documents AS target_doc ON target_doc.id = target.document_id
                WHERE source.document_id = ? AND target_doc.deleted_at IS NULL""",
            (document_id,),
        ).fetchone()[0]
        backlinks = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM block_refs AS refs
                    JOIN blocks AS source ON source.id = refs.source_block_id
                    JOIN documents AS source_doc ON source_doc.id = source.document_id
                    JOIN blocks AS target ON target.id = refs.target_block_id
                   WHERE target.document_id = ? AND source.document_id <> ?
                     AND source_doc.deleted_at IS NULL)
               + (SELECT COUNT(*) FROM document_refs AS refs
                    JOIN blocks AS source ON source.id = refs.source_block_id
                    JOIN documents AS source_doc ON source_doc.id = source.document_id
                   WHERE refs.target_document_id = ? AND source.document_id <> ?
                     AND source_doc.deleted_at IS NULL)""",
            (document_id, document_id, document_id, document_id),
        ).fetchone()[0]
        pictures = connection.execute(
            f"""SELECT block_assets.block_id, assets.media_name, assets.mime_type,
                       assets.content_hash, assets.text
                  FROM block_assets
                  JOIN blocks ON blocks.id = block_assets.block_id
                  JOIN assets USING (content_hash, media_name)
                 WHERE blocks.document_id = ?
                   AND assets.mime_type IN ({",".join("?" for _ in _ocr.OCR_TYPES)})
                 ORDER BY blocks.position, assets.media_name""",
            (document_id, *sorted(_ocr.OCR_TYPES)),
        ).fetchall()
        history = connection.execute(
            """SELECT COUNT(*) AS edits,
                      MAX(CASE WHEN undone = 0 THEN 1 ELSE 0 END) AS can_undo,
                      MAX(CASE WHEN undone = 1 THEN 1 ELSE 0 END) AS can_redo
                 FROM document_history WHERE document_id = ?""",
            (document_id,),
        ).fetchone()

    document_provenance = _provenance.document_provenance(
        document["provenance_json"], document["source_kind"]
    )
    words = characters = edited_here = 0
    by_source: dict[str, int] = {}
    conflicts: list[dict[str, Any]] = []
    removed_in_source: list[str] = []
    for block in blocks:
        text = block["text"]
        words += len(_WORD_RE.findall(_readable(text)))
        characters += len(text)
        value = _provenance.effective_block_provenance(block["provenance_json"], document_provenance)
        by_source[value["source"]] = by_source.get(value["source"], 0) + 1
        if value.get("source_hash") and value["source_hash"] != _provenance.content_hash(
            block["type"], _fingerprint(block)
        ):
            edited_here += 1
        if isinstance(value.get("conflict"), dict):
            conflicts.append(
                {
                    "kind": "block",
                    "block_id": block["id"],
                    "local_text": text,
                    **{
                        key: value["conflict"].get(key)
                        for key in ("detected_at", "source_markdown", "source_modified_at")
                    },
                }
            )
        if value.get("source_removed"):
            removed_in_source.append(block["id"])
    sync = _provenance.loads(document["sync_json"])
    if isinstance(sync.get("title_conflict"), dict):
        conflicts.insert(
            0,
            {
                "kind": "title",
                "local_title": document["title"],
                "source_title": sync["title_conflict"].get("source_title"),
                "detected_at": sync["title_conflict"].get("detected_at"),
            },
        )
    return {
        "id": document["id"],
        "title": document["title"],
        "status": document["status"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "last_opened_at": document["last_opened_at"],
        "revision": document["revision"],
        "folder_id": document["folder_id"],
        "folder_path": [{"id": row["id"], "name": row["name"]} for row in folders],
        "provenance": document_provenance,
        "counts": {
            "words": words,
            "characters": characters,
            "blocks": len(blocks),
            "outgoing_links": outgoing_documents + outgoing_blocks,
            "outgoing_document_links": outgoing_documents,
            "outgoing_block_links": outgoing_blocks,
            "backlinks": backlinks,
            "edits": history["edits"],
        },
        "blocks_by_source": dict(sorted(by_source.items())),
        "can_undo": bool(history["can_undo"]),
        "can_redo": bool(history["can_redo"]),
        "sync": None
        if not sync
        else {
            "synced_at": sync.get("synced_at"),
            "checked_at": sync.get("checked_at"),
            "source_title": sync.get("title"),
            "blocks_edited_here": edited_here,
            "blocks_removed_in_source": removed_in_source,
        },
        "conflicts": conflicts,
        "picture_text": _picture_text(path, pictures),
    }


PICTURE_TEXT_CHARS = 4000


def _picture_text(path: Path, rows: list) -> list[dict[str, Any]]:
    """Text read from each picture/PDF of the note (searchable, not in the body)."""

    status = _ocr.asset_status(path, list({row["content_hash"] for row in rows}))
    result = []
    for row in rows:
        state = status.get(row["content_hash"], {})
        text = str(row["text"] or "")
        result.append(
            {
                "block_id": row["block_id"],
                "media_name": row["media_name"],
                "mime_type": row["mime_type"],
                # queued, running (or retrying), done, failed
                "status": state.get("status", "queued"),
                "method": state.get("method") or None,  # ocr, pdf-text, pdf-text+ocr
                "pages": state.get("pages"),
                "error": state.get("error"),
                "model": _ocr.MODEL,
                "read_at": state.get("updated_at"),
                # The text in use; it can predate the current model while that is queued.
                "text": text[:PICTURE_TEXT_CHARS],
                "text_chars": len(text),
            }
        )
    return result


def resolve_block_conflict(path: Path, document_id: str, block_id: str, keep: str) -> dict:
    """Settle a block's re-import conflict; returns the document tree.

    ``keep="source"`` replaces the local text with the source's version (an
    ordinary, undoable edit); ``keep="local"`` keeps the local text and stops
    reporting this source version as a conflict.
    """

    if keep not in {"source", "local"}:
        raise ValueError("keep must be 'source' or 'local'")
    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute(
            """SELECT blocks.provenance_json, documents.revision FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                WHERE blocks.id = ? AND blocks.document_id = ? AND documents.deleted_at IS NULL""",
            (block_id, document_id),
        ).fetchone()
    if row is None:
        raise KeyError("block not found")
    value = _provenance.loads(row["provenance_json"])
    conflict = value.get("conflict")
    if not isinstance(conflict, dict):
        raise ValueError("block has no source conflict")
    resolved = {key: item for key, item in value.items() if key != "conflict"}
    if keep == "source":
        markdown = str(conflict.get("source_markdown", ""))
        structured = isinstance(conflict.get("source_content"), dict)
        apply_transaction(
            path,
            document_id,
            [
                BlockOperation(
                    operation="update",
                    block_id=block_id,
                    type=conflict.get("source_type"),
                    text=conflict.get("source_text") if structured else markdown,
                    content=conflict["source_content"] if structured else {"markdown": markdown},
                    attrs=conflict.get("source_attrs") if structured else None,
                )
            ],
            row["revision"],
        )
        resolved["source_hash"] = conflict.get("source_hash")
        resolved.pop("dismissed_hash", None)
    else:
        resolved["dismissed_hash"] = conflict.get("source_hash")
    with connection_scope(path) as connection:
        connection.execute(
            "UPDATE blocks SET provenance_json = ? WHERE id = ?",
            (_provenance.dumps(resolved), block_id),
        )
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree
