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

    ``markdown`` is the block's own Markdown (one paragraph, heading, list item,
    code fence, ...); nested content goes in ``children``. ``type`` defaults to
    what the Markdown parses as. ``provenance`` holds extra per-block fields such
    as ``source_path`` or ``extra``.
    """

    markdown: str
    external_id: str | None = None
    type: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["SourceBlock"] = field(default_factory=list)
    created_at: str | None = None
    modified_at: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


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


# --- Folders ------------------------------------------------------------------


def ensure_folder_path(
    path: Path, source: str, segments: Iterable[tuple[str, str] | dict[str, str]]
) -> str | None:
    """Find or create a folder chain by source ids; returns the last folder's id.

    ``segments`` run from the top, e.g. ``[(notebook_guid, "Work"),
    (section_guid, "Meetings")]``. A folder is found again by its source id even
    after the user renamed or moved it (both are kept); a folder still carrying
    the name the source gave it follows the source's renames.
    """

    initialize(path)
    parent_id: str | None = None
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

    @property
    def hash(self) -> str:
        return _provenance.content_hash(self.type, self.markdown)


def _prepare(blocks: list[SourceBlock] | str) -> list[_Planned]:
    if isinstance(blocks, str):
        blocks = _from_markdown(blocks)
    seen: dict[str, int] = {}

    def convert(block: SourceBlock) -> _Planned:
        block_type, markdown, attrs = block.type, block.markdown, dict(block.attrs)
        extra_children: list[SourceBlock] = []
        if block_type is None:
            parsed = _parse_blocks(markdown)
            if parsed:
                first = parsed[0]
                block_type = first.type
                attrs = {**first.attrs, **attrs}
                if len(parsed) == 1:
                    markdown = first.source
                    extra_children = _from_imported(first.children)
            else:
                block_type = "paragraph"
        children = [convert(child) for child in [*extra_children, *block.children]]
        base_id = block.external_id or "~" + _provenance.content_hash(block_type, markdown)
        seen[base_id] = seen.get(base_id, 0) + 1
        external_id = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        return _Planned(
            external_id, block_type, markdown, attrs, children,
            block.created_at, block.modified_at, dict(block.provenance),
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
            json.dumps(block.attrs), json.dumps({"markdown": block.markdown}),
            block.markdown, created, block.modified_at or created,
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
        """INSERT INTO documents(id, title, status, tags_json, folder_id, position,
               created_at, updated_at, provenance_json, source_kind, source_external_id,
               sync_json)
           VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                  provenance_json
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

    def plan(blocks: list[_Planned], parent_id: str | None) -> None:
        order: list[str] = []
        for block in blocks:
            block_id = by_external.get(block.external_id)
            if block_id is not None and block_id not in matched:
                matched.add(block_id)
                row = by_id[block_id]
                old = stored_provenance[block_id]
                current_hash = _provenance.content_hash(row["type"], row["text"])
                base_hash = old.get("source_hash")
                new = _block_provenance(source, block, old)
                if current_hash == base_hash or current_hash == block.hash:
                    # Untouched (or the source caught up with the local edit).
                    new.pop("dismissed_hash", None)
                    if current_hash != block.hash or json.loads(row["attrs_json"]) != block.attrs:
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
                                **({"source_modified_at": block.modified_at} if block.modified_at else {}),
                            }
                            result.conflicts.append({"block_id": block_id, **new["conflict"]})
                    if base_hash is None:
                        new.pop("source_hash", None)
                if new != old:
                    provenance_updates[block_id] = new
                order.append(block_id)
                plan(block.children, block_id)
            elif block.external_id in previously_synced:
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
        if _provenance.content_hash(row["type"], row["text"]) != value.get("source_hash"):
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
                    block.type, json.dumps(block.attrs), json.dumps({"markdown": block.markdown}),
                    block.markdown, block.modified_at or now, revision, block_id,
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
        **{key: value for key, value in old_provenance.items() if key not in {"backfilled"}},
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
            """SELECT id, type, text, created_at, updated_at, provenance_json
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
            block["type"], text
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
    }


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
        apply_transaction(
            path,
            document_id,
            [
                BlockOperation(
                    operation="update",
                    block_id=block_id,
                    type=conflict.get("source_type"),
                    text=markdown,
                    content={"markdown": markdown},
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
