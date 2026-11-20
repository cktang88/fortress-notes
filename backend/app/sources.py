"""Living documents: notes that came from an outside source and stay in step with it.

Importers (OneNote, Markdown, ...) call :func:`upsert_document_from_source` once
per source page. The call is an idempotent upsert keyed by ``(source kind,
external id)``: the first call creates a note, later calls update that same note
in place, so its Fortress ID, links, backlinks and history survive.

Conflict policy (simple on purpose):

* **Blocks are matched by their source id** (``SourceBlock.external_id``). A
  block without one gets ``<parent's id>/<index>`` under a parent that has one,
  else a content-derived id, so an unchanged block still matches.
* **Untouched blocks follow the source.** A block whose canonical content
  (type, attrs, inline content, payload) is still what the last import wrote is
  updated to the source's new content.
* **Local edits win.** A block edited in Fortress keeps the local content. If
  the source changed it too, the source's version is recorded as the block's
  sync conflict (listed by ``document_info``), to settle later with
  :func:`resolve_block_conflict`.
* **Local additions stay** where they were, after the block they followed.
* **Local deletions stick.** A source block that was imported before and is no
  longer in the note (deleted, or moved to another note) is not re-created.
* **Blocks gone from the source** are deleted if untouched; an edited one is
  kept and marked ``source_removed``.
* **Structure follows the source**: source blocks take the source's order and
  nesting.
* **Title** follows the source unless renamed locally (then the source's title
  is recorded as a title conflict). **Folder** follows the source unless the note
  was moved locally.
* A note the user **trashed** is left alone (``skipped_deleted``); a purged one is
  imported afresh.

Re-import state lives in the ``block_sync`` table and ``documents.sync_json``,
never in provenance. A whole re-import is one undoable step in the note's history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import ocr as _ocr
from . import block_schema as schema
from . import order_keys
from . import provenance as _provenance
from .block_markdown import BlockSpec, legalize, parse_markdown
from .block_schema import BlockShapeError
from .block_store import (
    apply_transaction,
    ChangeRecorder,
    connection_scope,
    document_insert_position,
    document_retitled,
    document_tree,
    initialize,
    new_id,
    rebuild_document_derived,
    shift_document_positions,
    utc_now,
    write_block_sync,
)
from .models import BlockOperation


@dataclass
class SourceBlock:
    """One block as the source has it.

    Either ``markdown`` (the block's own Markdown; it may read as several
    blocks) or a ``type`` with canonical ``attrs``, ``content`` (inline nodes)
    and ``payload`` (docs/block-model.md); nothing else is read. Nested blocks go in
    ``children`` (a table's are ``table_row`` blocks, a ``columns`` block's are
    ``column`` blocks). ``provenance`` holds extra per-block fields such as
    ``source_path`` or ``extra``. :func:`source_block` builds one from a plain
    dict.
    """

    markdown: str = ""
    external_id: str | None = None
    type: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["SourceBlock"] = field(default_factory=list)
    created_at: str | None = None
    modified_at: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None
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


def source_block(data: dict[str, Any] | str) -> SourceBlock:
    """A :class:`SourceBlock` from a plain dict in the canonical shape
    (docs/block-model.md): ``type``, ``attrs``, ``content`` (inline nodes, text
    types), ``payload`` (html) and ``children`` (a table's are
    ``table_row`` blocks, whose children are ``table_cell`` blocks; a
    ``columns`` block's are ``column`` blocks). Source keys: ``id`` (the
    source's id for this block), ``previous_ids``, ``created_at``,
    ``modified_at``, ``provenance``. ``markdown`` instead of ``type`` is the
    block's own Markdown. A bare string is Markdown.
    """

    if isinstance(data, str):
        return SourceBlock(markdown=data)
    if not isinstance(data, dict):
        raise BlockShapeError("a source block must be a dict or a Markdown string")
    content = data.get("content")
    if content is not None and not isinstance(content, list):
        raise BlockShapeError("a source block's content must be a list of inline nodes")
    return SourceBlock(
        markdown=str(data.get("markdown") or ""),
        external_id=data.get("id", data.get("external_id")),
        type=data.get("type"),
        attrs=dict(data.get("attrs") or {}),
        children=[source_block(child) for child in data.get("children") or []],
        created_at=data.get("created_at"),
        modified_at=data.get("modified_at"),
        provenance=dict(data.get("provenance") or {}),
        content=content,
        payload=data.get("payload"),
        previous_external_ids=[str(item) for item in data.get("previous_ids") or [] if item],
    )


def source_blocks(items: Iterable[dict[str, Any] | str]) -> list[SourceBlock]:
    """:func:`source_block` for each item."""

    return [source_block(item) for item in items]


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
                        (name, name, utc_now(), row["id"]),
                    )
                parent_id = row["id"]
                continue
            now = utc_now()
            folder_id = new_id()
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
class _Planned(BlockSpec):
    """A source block resolved to a canonical block, with its source id."""

    external_id: str = ""
    created_at: str | None = None
    modified_at: str | None = None
    source_provenance: dict[str, Any] = field(default_factory=dict)
    previous_external_ids: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return schema.canonical_hash(self.block())


def _resolve(block: SourceBlock) -> list[BlockSpec]:
    """Canonical blocks for one source block (a Markdown string may hold several)."""

    if block.type is None:
        return parse_markdown(block.markdown) or [BlockSpec("paragraph", content=[])]
    return [BlockSpec(**_fields(schema.normalize_block(block.type, block.attrs, block.content, block.payload)))]


def _fields(block: schema.Block) -> dict[str, Any]:
    return {"type": block.type, "attrs": block.attrs, "content": block.content, "payload": block.payload}


def _prepare(blocks: list[SourceBlock] | str) -> list[_Planned]:
    if isinstance(blocks, str):
        blocks = [SourceBlock(markdown=blocks)]

    def convert(block: SourceBlock) -> list[BlockSpec]:
        resolved = _resolve(block)
        first = resolved[0]
        planned = _Planned(
            first.type, first.attrs, first.content, first.payload,
            first.children + [child for item in block.children for child in convert(item)],
            provenance=first.provenance,
            external_id=block.external_id or "",
            created_at=block.created_at,
            modified_at=block.modified_at,
            source_provenance=dict(block.provenance),
            previous_external_ids=list(block.previous_external_ids),
        )
        return [planned, *resolved[1:]]

    roots = legalize([spec for block in blocks for spec in convert(block)])
    seen: dict[str, int] = {}

    def planned(spec: BlockSpec, parent: _Planned | None, index: int) -> _Planned:
        if not isinstance(spec, _Planned):
            spec = _Planned(spec.type, spec.attrs, spec.content, spec.payload, spec.children,
                            provenance=spec.provenance)
        if not spec.external_id:
            if parent is not None:
                # Named by position under its parent, so it follows the parent's
                # id, including the ids the parent was known by before.
                spec.external_id = f"{parent.external_id}/{index}"
                spec.previous_external_ids = [
                    f"{previous}/{index}" for previous in parent.previous_external_ids
                ]
            else:
                base = "~" + spec.hash
                seen[base] = seen.get(base, 0) + 1
                spec.external_id = base if seen[base] == 1 else f"{base}#{seen[base]}"
        spec.children = [planned(child, spec, number) for number, child in enumerate(spec.children)]
        return spec

    return [planned(spec, None, index) for index, spec in enumerate(roots)]


def _from_markdown(body: str) -> list[SourceBlock]:
    """Markdown as source blocks (ids and provenance from its fortress comments)."""

    def wrap(spec: BlockSpec) -> SourceBlock:
        restored = spec.provenance or {}
        return SourceBlock(
            type=spec.type, attrs=spec.attrs, content=spec.content, payload=spec.payload,
            children=[wrap(child) for child in spec.children],
            external_id=restored.get("external_id"),
            provenance={key: value for key, value in restored.items()
                        if key in {"source_path", "source_url", "extra"}},
        )

    return [wrap(spec) for spec in parse_markdown(body)]


def _block_provenance(source: str, block: _Planned, previous: dict | None = None) -> dict:
    value = dict(previous or {})
    value.pop("inherited", None)
    value.update(_provenance.normalize({**block.source_provenance, "source": source}))
    value["source"] = source
    value["external_id"] = block.external_id
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

    Sets ``source_removed_at`` in the note's sync state (kept from an earlier
    run if already marked); the next upsert of the note clears it. Returns
    ``{"document_id", "title", "source_path", "source_removed_at",
    "newly_removed"}`` per note of ``source`` that is not in the trash.
    """

    now = at or utc_now()
    result: list[dict[str, Any]] = []
    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        for document_id in document_ids:
            row = connection.execute(
                """SELECT id, title, provenance_json, sync_json FROM documents
                    WHERE id = ? AND source_kind = ? AND deleted_at IS NULL""",
                (document_id, source),
            ).fetchone()
            if row is None:
                continue
            sync = _provenance.loads(row["sync_json"])
            newly = not sync.get("source_removed_at")
            if newly:
                sync["source_removed_at"] = now
                connection.execute(
                    "UPDATE documents SET sync_json = ? WHERE id = ?",
                    (json.dumps(sync), document_id),
                )
            result.append({
                "document_id": row["id"],
                "title": row["title"],
                "source_path": _provenance.loads(row["provenance_json"]).get("source_path"),
                "source_removed_at": sync.get("source_removed_at") or now,
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
    order_key: str,
    default_created: str,
    revision: int | None,
) -> None:
    created = block.created_at or default_created
    connection.execute(
        """INSERT INTO blocks(id, document_id, parent_id, order_key, type, attrs_json,
               content_json, payload_json, text, created_at, updated_at, provenance_json,
               revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
        (
            block_id, document_id, parent_id, order_key, block.type,
            _dumps(block.attrs), _dumps_nullable(block.content), _dumps_nullable(block.payload),
            created, block.modified_at or created,
            _provenance.dumps(_block_provenance(source, block)), revision,
        ),
    )
    write_block_sync(connection, block_id, {"source_hash": block.hash})


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dumps_nullable(value: object) -> str | None:
    return None if value is None else _dumps(value)


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
    now = utc_now()
    document_id = new_id()
    created = created_at or now
    position = document_insert_position(connection, folder_id, None)
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
        for block, key in zip(blocks, order_keys.keys_between(None, None, len(blocks))):
            block_id = new_id()
            _insert_planned(
                connection, document_id, document_provenance["source"], block,
                block_id, parent_id, key, created, None,
            )
            count += 1
            insert(block.children, block_id)

    insert(planned, None)
    rebuild_document_derived(connection, document_id)
    return UpsertResult(document_id, "created", 0, blocks_created=count)


def _row_block(row: sqlite3.Row) -> schema.Block:
    return schema.Block(
        row["type"], json.loads(row["attrs_json"]),
        None if row["content_json"] is None else json.loads(row["content_json"]),
        None if row["payload_json"] is None else json.loads(row["payload_json"]),
    )


def _source_version(block: _Planned) -> dict[str, Any]:
    return {"type": block.type, "attrs": block.attrs, "content": block.content,
            "payload": block.payload}


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
    now = utc_now()
    document_id = document["id"]
    revision = int(document["revision"]) + 1
    sync = _provenance.loads(document["sync_json"])
    previously_synced = set(sync.get("block_ids") or [])
    rows = connection.execute(
        """SELECT blocks.*, block_sync.source_hash, block_sync.dismissed_hash,
                  block_sync.conflict_json, block_sync.source_removed
             FROM blocks LEFT JOIN block_sync ON block_sync.block_id = blocks.id
            WHERE blocks.document_id = ? ORDER BY blocks.order_key""",
        (document_id,),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    stored_provenance = {row["id"]: _provenance.loads(row["provenance_json"]) for row in rows}
    current_hash = {row["id"]: schema.canonical_hash(_row_block(row)) for row in rows}
    by_external: dict[str, str] = {}
    for row in rows:
        value = stored_provenance[row["id"]]
        external = value.get("external_id")
        if external and value.get("source") == source and external not in by_external:
            by_external[external] = row["id"]
    current_children: dict[str | None, list[str]] = {}
    for row in rows:
        current_children.setdefault(row["parent_id"], []).append(row["id"])

    result = UpsertResult(document_id, "unchanged", int(document["revision"]))
    layout: dict[str | None, list[str]] = {}
    matched: set[str] = set()
    inserts: dict[str, tuple[str | None, _Planned]] = {}
    content_updates: dict[str, _Planned] = {}
    provenance_updates: dict[str, dict] = {}
    sync_updates: dict[str, dict] = {}

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
                old_sync = {
                    "source_hash": row["source_hash"], "dismissed_hash": row["dismissed_hash"],
                    "conflict_json": row["conflict_json"], "source_removed": row["source_removed"] or 0,
                }
                new_sync = {**old_sync, "source_removed": 0}
                base_hash = row["source_hash"]
                if current_hash[block_id] in (base_hash, block.hash):
                    # Untouched (or the source caught up with the local edit).
                    new_sync.update(source_hash=block.hash, dismissed_hash=None, conflict_json=None)
                    if current_hash[block_id] != block.hash:
                        content_updates[block_id] = block
                elif block.hash not in (base_hash, row["dismissed_hash"]):
                    # Edited here: keep the local content, remember what the source has.
                    conflict = json.loads(row["conflict_json"]) if row["conflict_json"] else {}
                    if conflict.get("source_hash") != block.hash:
                        conflict = {
                            "detected_at": now,
                            "source_hash": block.hash,
                            "source": _source_version(block),
                            **({"source_modified_at": block.modified_at} if block.modified_at else {}),
                        }
                        result.conflicts.append({"block_id": block_id, **conflict})
                    new_sync["conflict_json"] = json.dumps(conflict, sort_keys=True)
                if new_sync != old_sync:
                    sync_updates[block_id] = new_sync
                old = stored_provenance[block_id]
                new = _block_provenance(source, block, old)
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
                block_id = new_id()
                inserts[block_id] = (parent_id, block)
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
        if current_hash[block_id] != by_id[block_id]["source_hash"]:
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
        if value.get("source") == source and value.get("external_id") and not by_id[block_id]["source_removed"]:
            sync_updates[block_id] = {
                "source_hash": by_id[block_id]["source_hash"],
                "dismissed_hash": by_id[block_id]["dismissed_hash"],
                "conflict_json": by_id[block_id]["conflict_json"],
                "source_removed": 1,
            }

    # Local blocks stay after the sibling they followed (a parent that is
    # deleted takes its local children along only if they were deletable too).
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

    parent_of = {
        block_id: parent_id for parent_id, order in final.items() for block_id in order
    }
    reordered = {
        parent_id for parent_id, order in final.items()
        if [block_id for block_id in current_children.get(parent_id, []) if block_id not in deletable]
        != order
    }
    moved = [
        block_id for block_id in by_id
        if block_id in parent_of and parent_of[block_id] != by_id[block_id]["parent_id"]
    ]
    blocks_changed = bool(
        inserts or content_updates or provenance_updates or sync_updates or deletable or reordered
        or moved
    )

    if blocks_changed:
        changes = ChangeRecorder(connection)
        for block_id in deletable:
            changes.touch_subtree(block_id)
        for parent_id in reordered:
            for block_id in final[parent_id]:
                if block_id in by_id:
                    changes.touch(block_id)
        for block_id in [*content_updates, *provenance_updates, *sync_updates, *moved]:
            changes.touch(block_id)
        for block_id in inserts:
            changes.touch(block_id)
        # Park the blocks being placed, and those going away (they are deleted
        # last: matched children move out of them first), on unique temporary
        # keys; then give every reordered sibling list fresh keys (parents
        # before children).
        parked = [*deletable, *(
            block_id for parent_id in reordered for block_id in final[parent_id] if block_id in by_id
        )]
        for block_id in parked:
            connection.execute("UPDATE blocks SET order_key = '~' || id WHERE id = ?", (block_id,))
        placed: set[str] = set()

        def place(parent_id: str | None) -> None:
            order = final.get(parent_id, [])
            keys = order_keys.keys_between(None, None, len(order)) if parent_id in reordered else []
            for index, block_id in enumerate(order):
                if block_id in placed:
                    continue
                placed.add(block_id)
                if block_id in inserts:
                    _insert_planned(
                        connection, document_id, source, inserts[block_id][1], block_id,
                        parent_id, keys[index], now, revision,
                    )
                elif parent_id in reordered:
                    connection.execute(
                        "UPDATE blocks SET parent_id = ?, order_key = ? WHERE id = ?",
                        (parent_id, keys[index], block_id),
                    )
                place(block_id)

        place(None)
        for block_id, block in content_updates.items():
            connection.execute(
                """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, payload_json = ?,
                          updated_at = ?, revision = ? WHERE id = ?""",
                (
                    block.type, _dumps(block.attrs), _dumps_nullable(block.content),
                    _dumps_nullable(block.payload), block.modified_at or now, revision, block_id,
                ),
            )
        for block_id, value in provenance_updates.items():
            connection.execute(
                "UPDATE blocks SET provenance_json = ? WHERE id = ?",
                (_provenance.dumps(value), block_id),
            )
        for block_id, value in sync_updates.items():
            write_block_sync(connection, block_id, value)
        # Matched children were moved out above; what cascades is deletable too.
        for block_id in deletable:
            connection.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
        changes.record(document_id, revision, now)
        rebuild_document_derived(connection, document_id)
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
        shift_document_positions(connection, document["folder_id"], document["position"] + 1, -1)
        position = document_insert_position(connection, new_folder, None, exclude_id=document_id)
        connection.execute(
            "UPDATE documents SET folder_id = ?, position = ? WHERE id = ?",
            (new_folder, position, document_id),
        )
    if sync.pop("source_removed_at", None):
        # A page the source has again is no longer "removed in the source".
        document_changed = True
    old_provenance = _provenance.loads(document["provenance_json"])
    new_provenance = {
        **{key: value for key, value in old_provenance.items() if key != "backfilled"},
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
        if new_title != document["title"]:
            document_retitled(connection, document_id)
        result.status = "updated"
    sync["block_ids"] = [block.external_id for block in _flatten(planned)]
    sync["checked_at"] = now
    connection.execute(
        "UPDATE documents SET sync_json = ? WHERE id = ?", (json.dumps(sync), document_id)
    )
    return result


# --- Metadata -----------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


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
            """SELECT blocks.id, blocks.type, blocks.attrs_json, blocks.content_json,
                      blocks.payload_json, blocks.text, blocks.provenance_json,
                      block_sync.source_hash, block_sync.conflict_json, block_sync.source_removed
                 FROM blocks LEFT JOIN block_sync ON block_sync.block_id = blocks.id
                WHERE blocks.document_id = ?""",
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
                 ORDER BY blocks.order_key, assets.media_name""",
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
        words += len(_WORD_RE.findall(text))
        characters += len(text)
        value = _provenance.effective_block_provenance(block["provenance_json"], document_provenance)
        by_source[value["source"]] = by_source.get(value["source"], 0) + 1
        if block["source_hash"] and block["source_hash"] != schema.canonical_hash(_row_block(block)):
            edited_here += 1
        if block["conflict_json"]:
            conflict = json.loads(block["conflict_json"])
            source_version = conflict.get("source") or {}
            source_text = ""
            try:
                source_text = schema.derive_text(schema.normalize_block(
                    source_version.get("type", "paragraph"), source_version.get("attrs"),
                    source_version.get("content"), source_version.get("payload"),
                ))
            except BlockShapeError:
                pass
            conflicts.append(
                {
                    "kind": "block",
                    "block_id": block["id"],
                    "local_text": text,
                    "source_text": source_text,
                    "source": source_version,
                    "detected_at": conflict.get("detected_at"),
                    "source_modified_at": conflict.get("source_modified_at"),
                }
            )
        if block["source_removed"]:
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
            "source_removed_at": sync.get("source_removed_at"),
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
                "content_hash": row["content_hash"],
                "mime_type": row["mime_type"],
                # queued, running (or retrying), done, failed
                "status": state.get("status", "queued"),
                "method": state.get("method") or None,  # ocr, pdf-text, pdf-text+ocr
                "pages": state.get("pages"),
                "error": state.get("error"),
                # The reading whose text is used (apple-vision/… from the Mac app, or
                # this process's model), or the model that is to read it.
                "model": state.get("model") or _ocr.MODEL,
                "read_at": state.get("updated_at"),
                # The text in use; it can predate the current model while that is queued.
                "text": text[:PICTURE_TEXT_CHARS],
                "text_chars": len(text),
            }
        )
    return result


def resolve_block_conflict(path: Path, document_id: str, block_id: str, keep: str) -> dict:
    """Settle a block's re-import conflict; returns the document tree.

    ``keep="source"`` replaces the local content with the source's version (an
    ordinary, undoable edit); ``keep="local"`` keeps the local content and stops
    reporting this source version as a conflict.
    """

    if keep not in {"source", "local"}:
        raise ValueError("keep must be 'source' or 'local'")
    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute(
            """SELECT blocks.type, blocks.attrs_json, block_sync.*, documents.revision
                 FROM blocks JOIN documents ON documents.id = blocks.document_id
                 LEFT JOIN block_sync ON block_sync.block_id = blocks.id
                WHERE blocks.id = ? AND blocks.document_id = ? AND documents.deleted_at IS NULL""",
            (block_id, document_id),
        ).fetchone()
    if row is None:
        raise KeyError("block not found")
    if not row["conflict_json"]:
        raise ValueError("block has no source conflict")
    conflict = json.loads(row["conflict_json"])
    resolved = {
        "source_hash": row["source_hash"], "dismissed_hash": row["dismissed_hash"],
        "conflict_json": None, "source_removed": row["source_removed"],
    }
    if keep == "source":
        version = conflict.get("source") or {}
        new_type = version.get("type") or row["type"]
        current_attrs = json.loads(row["attrs_json"])
        patch = {key: None for key in current_attrs}
        patch.update(version.get("attrs") or {})
        operation = {"operation": "update", "block_id": block_id, "type": new_type, "attrs": patch}
        if version.get("content") is not None:
            operation["content"] = version["content"]
        if version.get("payload") is not None:
            operation["payload"] = version["payload"]
        apply_transaction(path, document_id, [BlockOperation(**operation)], row["revision"])
        resolved["source_hash"] = conflict.get("source_hash")
        resolved["dismissed_hash"] = None
    else:
        resolved["dismissed_hash"] = conflict.get("source_hash")
    with connection_scope(path) as connection:
        write_block_sync(connection, block_id, resolved)
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree
