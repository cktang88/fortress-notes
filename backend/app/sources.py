"""Notes imported from an outside source, refreshed when it is imported again.

Importers (OneNote, ...) call :func:`upsert_document_from_source` once per
source page, keyed by ``(source kind, external id)``. The first call creates a
note; later calls find that same note again:

* **Source unchanged** (same title and blocks as the last import): nothing
  happens (``unchanged``).
* **Edited here since the last import** (the note's revision moved on): the note
  is left alone and reported (``skipped_edited``), never overwritten.
* **Otherwise the note is refreshed**: its blocks are replaced by the source's,
  and its title follows the source unless the user renamed the note (a rename
  does not move the revision, so it is told apart by the imported title). The note keeps its id and folder, and a
  block whose source id matches a block of the last import keeps its Fortress
  id, so links and references into it survive.
* A note the user **trashed** is left alone (``skipped_deleted``); a purged one
  is imported afresh.

What the importer says about the page (``provenance``: e.g. its source path,
URL and importer-specific ``extra``) is kept in ``documents.provenance_json``,
written here only and read with :func:`import_details`; it is never searched
and not part of the note's payload.

What a refresh needs lives in ``documents.sync_json``: the note's revision
after the last import, a hash of what the source had, the title it was given
(``title``), and which block each
source id (and each extra ``anchors`` id, such as an id links use) became.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import block_schema as schema
from . import order_keys
from .block_markdown import BlockSpec, legalize
from .block_schema import BlockShapeError
from .block_store import (
    connection_scope,
    document_insert_position,
    document_retitled,
    initialize,
    insert_block_row,
    new_id,
    rebuild_document_derived,
    refresh_derived,
    stored_block,
    update_block_row,
    utc_now,
)


@dataclass
class SourceBlock:
    """One block as the source has it: a canonical ``type`` with ``attrs``,
    ``content`` (inline nodes) and ``payload`` (docs/block-model.md). Nested
    blocks go in ``children`` (a table's are ``table_row`` blocks, a ``columns``
    block's are ``column`` blocks). :func:`source_block` builds one from a plain dict.
    """

    type: str
    attrs: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None
    children: list["SourceBlock"] = field(default_factory=list)
    external_id: str | None = None
    # Source ids this block had in earlier imports (e.g. a content hash before
    # the source exposed a stable id): the block imported under one of them is
    # this block, and takes ``external_id`` from now on.
    previous_external_ids: list[str] = field(default_factory=list)
    # Other source ids that name this block (e.g. the ids links point at); see
    # ``sync_state(...)["anchors"]``.
    anchors: list[str] = field(default_factory=list)


@dataclass
class UpsertResult:
    document_id: str
    # "created" | "updated" | "unchanged" | "skipped_edited" | "skipped_deleted"
    status: str
    revision: int = 0
    blocks_created: int = 0
    blocks_updated: int = 0
    blocks_deleted: int = 0


def _json_object(raw: object) -> dict[str, Any]:
    """Stored JSON (or NULL) as a dict; damaged values read as empty."""

    try:
        value = json.loads(str(raw)) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def import_details(raw: object) -> dict[str, Any]:
    """A note's ``provenance_json``: what its importer recorded (``source_path``,
    ``source_url``, ``importer``, ``source_created_at`` / ``source_modified_at``,
    ``extra``). Empty for notes that were not imported."""

    return _json_object(raw)


def source_url(path: Path, document_id: str) -> str | None:
    """The web page an imported note says it came from (OneNote's "Pasted from")."""

    with connection_scope(path) as connection:
        row = connection.execute(
            "SELECT provenance_json FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    url = import_details(row[0]).get("source_url") if row is not None else None
    return url if isinstance(url, str) else None


# --- Building source blocks from plain dicts ------------------------------------------


def source_block(data: dict[str, Any]) -> SourceBlock:
    """A :class:`SourceBlock` from a plain dict in the canonical shape
    (docs/block-model.md): ``type``, ``attrs``, ``content`` (inline nodes, text
    types), ``payload`` (html) and ``children``. Source keys: ``id`` (the
    source's id for this block), ``previous_ids``, ``anchors``.
    """

    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise BlockShapeError("a source block must be a dict with a type")
    content = data.get("content")
    if content is not None and not isinstance(content, list):
        raise BlockShapeError("a source block's content must be a list of inline nodes")
    return SourceBlock(
        type=data["type"],
        attrs=dict(data.get("attrs") or {}),
        content=content,
        payload=data.get("payload"),
        children=[source_block(child) for child in data.get("children") or []],
        external_id=data.get("id"),
        previous_external_ids=[str(item) for item in data.get("previous_ids") or [] if item],
        anchors=[str(item) for item in data.get("anchors") or [] if item],
    )


def source_blocks(items: Iterable[dict[str, Any]]) -> list[SourceBlock]:
    """:func:`source_block` for each item."""

    return [source_block(item) for item in items]


# --- Folders ------------------------------------------------------------------


def ensure_folder_path(
    path: Path,
    source: str,
    segments: Iterable[tuple[str, str]],
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
        for external_id, name in segments:
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
    """A source block resolved to a canonical block, with its source ids."""

    external_id: str = ""
    previous_external_ids: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return schema.canonical_hash(self.block())


def _prepare(blocks: list[SourceBlock]) -> list[_Planned]:
    def convert(block: SourceBlock) -> _Planned:
        value = schema.normalize_block(block.type, block.attrs, block.content, block.payload)
        return _Planned(
            value.type, value.attrs, value.content, value.payload,
            [convert(child) for child in block.children],
            external_id=block.external_id or "",
            previous_external_ids=list(block.previous_external_ids),
            anchors=list(block.anchors),
        )

    roots = legalize([convert(block) for block in blocks])
    seen: dict[str, int] = {}

    def planned(spec: BlockSpec, parent: _Planned | None, index: int) -> _Planned:
        if not isinstance(spec, _Planned):
            spec = _Planned(spec.type, spec.attrs, spec.content, spec.payload, spec.children)
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


def _flatten(blocks: list[_Planned]) -> list[_Planned]:
    result = []
    for block in blocks:
        result.append(block)
        result.extend(_flatten(block.children))
    return result


def _source_hash(title: str, planned: list[_Planned]) -> str:
    """What the source had: a refresh is due when this changes."""

    def node(block: _Planned) -> list[Any]:
        return [block.external_id, block.anchors, block.type, block.attrs, block.content,
                block.payload, [node(child) for child in block.children]]

    encoded = json.dumps([title, [node(block) for block in planned]],
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def upsert_document_from_source(
    path: Path,
    *,
    source: str,
    external_id: str,
    title: str,
    blocks: list[SourceBlock],
    folder_id: str | None = None,
    folder_path: Iterable[tuple[str, str]] | None = None,
    created_at: str | None = None,
    modified_at: str | None = None,
    provenance: dict[str, Any] | None = None,
    importer: dict[str, str] | None = None,
) -> UpsertResult:
    """Create, refresh or leave alone the note for one source page (see the module docstring).

    ``provenance`` is what the importer records about the page (see
    :func:`import_details`): ``source_path`` ("Notebook › Section › Page"),
    ``source_url`` (http(s) only), ``extra``. ``folder_path`` (source-id,
    name pairs) is resolved with :func:`ensure_folder_path`; otherwise
    ``folder_id`` is used (None: top level). Both only place a new note.
    """

    if not external_id:
        raise ValueError("external_id is required")
    title = " ".join(str(title).split()) or "Untitled"
    planned = _prepare(blocks)
    source_hash = _source_hash(title, planned)
    document_provenance = {
        key: value for key, value in {
            **(provenance or {}),
            "importer": importer,
            "source_created_at": created_at,
            "source_modified_at": modified_at,
        }.items() if value not in (None, "", {})
    }
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
            return _create(connection, source, external_id, title, planned, folder_id,
                           created_at, modified_at, document_provenance, source_hash)
        if row["deleted_at"] is not None:
            return UpsertResult(row["id"], "skipped_deleted", row["revision"])
        sync = _json_object(row["sync_json"])
        if sync.get("hash") != source_hash and sync.get("revision") == row["revision"]:
            return _refresh(connection, row, title, planned, modified_at,
                            document_provenance, source_hash, sync)
        if sync.pop("source_removed_at", None):
            # A page the source has again is no longer "removed in the source".
            _save_sync(connection, row["id"], sync)
        status = "unchanged" if sync.get("hash") == source_hash else "skipped_edited"
        return UpsertResult(row["id"], status, row["revision"])


def _save_sync(connection: sqlite3.Connection, document_id: str, sync: dict[str, Any]) -> None:
    connection.execute(
        "UPDATE documents SET sync_json = ? WHERE id = ?",
        (json.dumps(sync, ensure_ascii=False), document_id),
    )


def sync_state(path: Path, document_id: str) -> dict[str, Any]:
    """A note's re-import state: ``blocks`` and ``anchors`` map source ids to block ids."""

    with connection_scope(path) as connection:
        row = connection.execute(
            "SELECT sync_json FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return _json_object(row["sync_json"]) if row is not None else {}


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
            sync = _json_object(row["sync_json"])
            newly = not sync.get("source_removed_at")
            if newly:
                sync["source_removed_at"] = now
                _save_sync(connection, document_id, sync)
            result.append({
                "document_id": row["id"],
                "title": row["title"],
                "source_path": import_details(row["provenance_json"]).get("source_path"),
                "source_removed_at": sync.get("source_removed_at") or now,
                "newly_removed": newly,
            })
    return result


def _write_blocks(
    connection: sqlite3.Connection,
    document_id: str,
    blocks: list[_Planned],
    parent_id: str | None,
    now: str,
    revision: int | None,
    stored: dict[str, sqlite3.Row],
    counts: UpsertResult,
) -> None:
    """Insert ``blocks`` (or update the stored ones whose id they took) under ``parent_id``."""

    for block, key in zip(blocks, order_keys.keys_between(None, None, len(blocks))):
        if block.id is None:
            block.id = new_id()
            insert_block_row(connection, block.id, document_id, parent_id, key, block,
                             now, now, revision)
            counts.blocks_created += 1
        elif schema.canonical_hash(stored_block(stored[block.id])) != block.hash:
            update_block_row(connection, block.id, parent_id, key, block, now, revision)
            counts.blocks_updated += 1
        else:
            connection.execute(
                "UPDATE blocks SET parent_id = ?, order_key = ? WHERE id = ?",
                (parent_id, key, block.id),
            )
        _write_blocks(connection, document_id, block.children, block.id, now, revision,
                      stored, counts)


def _new_sync(planned: list[_Planned], source_hash: str, revision: int, now: str,
              imported_at: str | None, title: str) -> dict[str, Any]:
    blocks: dict[str, str] = {}
    anchors: dict[str, str] = {}
    for block in _flatten(planned):
        blocks.setdefault(block.external_id, block.id)
        for anchor in block.anchors:
            anchors.setdefault(anchor, block.id)
    return {"hash": source_hash, "revision": revision, "title": title,
            "imported_at": imported_at or now, "synced_at": now,
            "blocks": blocks, "anchors": anchors}


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _create(
    connection: sqlite3.Connection,
    source: str,
    external_id: str,
    title: str,
    planned: list[_Planned],
    folder_id: str | None,
    created_at: str | None,
    modified_at: str | None,
    document_provenance: dict[str, Any],
    source_hash: str,
) -> UpsertResult:
    now = utc_now()
    document_id = new_id()
    created = created_at or now
    position = document_insert_position(connection, folder_id, None)
    connection.execute(
        """INSERT INTO documents(id, title, folder_id, position,
               created_at, updated_at, provenance_json, source_kind, source_external_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            document_id, title, folder_id, position, created, modified_at or created,
            _dumps(document_provenance), source, external_id,
        ),
    )
    result = UpsertResult(document_id, "created", 0)
    _write_blocks(connection, document_id, planned, None, created, None, {}, result)
    _save_sync(connection, document_id, _new_sync(planned, source_hash, 0, now, None, title))
    rebuild_document_derived(connection, document_id)
    return result


def _refresh(
    connection: sqlite3.Connection,
    document: sqlite3.Row,
    title: str,
    planned: list[_Planned],
    modified_at: str | None,
    document_provenance: dict[str, Any],
    source_hash: str,
    sync: dict[str, Any],
) -> UpsertResult:
    """Replace the note's blocks with the source's, keeping the ids of matching blocks."""

    now = utc_now()
    document_id = document["id"]
    revision = int(document["revision"]) + 1
    stored = {
        row["id"]: row for row in connection.execute(
            "SELECT id, type, attrs_json, content_json, payload_json FROM blocks WHERE document_id = ?",
            (document_id,),
        )
    }
    known = sync.get("blocks") if isinstance(sync.get("blocks"), dict) else {}
    flat = _flatten(planned)
    primary = {block.external_id for block in flat}
    kept: set[str] = set()
    for block in flat:
        # An earlier id some other source block still uses is that block's.
        candidates = [block.external_id] + [
            previous for previous in block.previous_external_ids if previous not in primary
        ]
        block.id = next(
            (known[name] for name in candidates
             if known.get(name) in stored and known[name] not in kept),
            None,
        )
        if block.id is not None:
            kept.add(block.id)
    # Kept blocks wait at the top level on unique keys; everything else goes.
    for block_id in kept:
        connection.execute(
            "UPDATE blocks SET parent_id = NULL, order_key = '~' || id WHERE id = ?", (block_id,)
        )
    removed = [block_id for block_id in stored if block_id not in kept]
    connection.executemany("DELETE FROM blocks WHERE id = ?", [(block_id,) for block_id in removed])
    result = UpsertResult(document_id, "updated", revision, blocks_deleted=len(removed))
    _write_blocks(connection, document_id, planned, None, now, revision, stored, result)

    imported_title = title
    # The title the last import gave; one the user chose since then stays.
    # (Notes imported before this was recorded follow the source, as they did.)
    if sync.get("title", document["title"]) != document["title"]:
        title = document["title"]
    new_provenance = {**import_details(document["provenance_json"]), **document_provenance}
    connection.execute(
        """UPDATE documents SET title = ?, title_auto = CASE WHEN ? THEN 0 ELSE title_auto END,
                  updated_at = ?, revision = ?, provenance_json = ?
            WHERE id = ?""",
        (
            title, title != document["title"],
            max(str(document["updated_at"]), modified_at or now), revision,
            _dumps(new_provenance), document_id,
        ),
    )
    _save_sync(connection, document_id,
               _new_sync(planned, source_hash, revision, now, sync.get("imported_at"),
                         imported_title))
    rebuild_document_derived(connection, document_id)
    if removed:
        # Text elsewhere that showed a removed block reads as broken now.
        refresh_derived(connection, document_id, [], removed=removed)
    if title != document["title"]:
        document_retitled(connection, document_id)
    return result
