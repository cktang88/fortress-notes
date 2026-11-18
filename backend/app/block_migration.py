"""Schema version 14: every stored block becomes canonical (docs/block-model.md §10).

Runs once, inside the migration's transaction, with foreign keys off (the
blocks table is rebuilt). Blocks keep their ids, timestamps, revisions,
provenance and user attributes; blocks that were JSON inside another block
(table cells) or Markdown inside another block (quoted paragraphs) become child
blocks with new ids. Re-import state moves from provenance to ``block_sync``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import block_legacy, order_keys
from . import block_schema as schema
from . import provenance as _provenance
from .block_markdown import BlockSpec, legalize

# Re-import state that older versions kept inside provenance.
_SYNC_FIELDS = ("source_hash", "conflict", "source_removed", "dismissed_hash")
_DOCUMENT_SYNC_FIELDS = ("source_removed", "source_removed_at")

_NEW_BLOCKS = """
CREATE TABLE blocks_new (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
    order_key TEXT NOT NULL,
    type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT,
    payload_json TEXT,
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provenance_json TEXT,
    revision INTEGER
)
"""


def _legacy_hash(block_type: str, text: str) -> str:
    """What older versions hashed a block's source content with."""

    return hashlib.sha256(f"{block_type}\n{text}".encode("utf-8")).hexdigest()[:32]


def _legacy_fingerprint(row: sqlite3.Row) -> str:
    try:
        content = json.loads(row["content_json"])
    except (TypeError, ValueError):
        content = None
    if isinstance(content, dict) and isinstance(content.get("markdown"), str):
        return content["markdown"]
    return str(row["text"])


def migrate(connection: sqlite3.Connection) -> None:
    from . import block_store

    _move_document_sync_state(connection)
    rows = connection.execute("SELECT * FROM blocks ORDER BY document_id, parent_id, position").fetchall()
    children: dict[str | None, list[sqlite3.Row]] = {}
    by_document: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
        if row["parent_id"] is None:
            by_document.setdefault(row["document_id"], []).append(row)
    document_sync = {
        row["id"]: _provenance.loads(row["sync_json"])
        for row in connection.execute("SELECT id, sync_json FROM documents")
    }
    connection.execute(_NEW_BLOCKS)
    syncs: list[tuple[str, dict[str, Any]]] = []
    for document_id, roots in by_document.items():
        writer = _DocumentWriter(connection, document_id, children)
        writer.write(roots)
        syncs.extend(writer.syncs)
        if writer.renamed:
            sync = document_sync.get(document_id, {})
            sync["block_ids"] = [writer.renamed.get(item, item) for item in sync.get("block_ids") or []]
            connection.execute(
                "UPDATE documents SET sync_json = ? WHERE id = ?", (json.dumps(sync), document_id)
            )
    connection.execute("DROP TABLE blocks")
    connection.execute("ALTER TABLE blocks_new RENAME TO blocks")
    connection.execute("CREATE INDEX IF NOT EXISTS blocks_updated_at ON blocks(updated_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS blocks_parent ON blocks(parent_id)")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS blocks_sibling_order
               ON blocks(document_id, COALESCE(parent_id, ''), order_key)"""
    )
    for statement in (
        "DROP TABLE IF EXISTS block_attrs",
        "DROP TABLE IF EXISTS revisions",
        "DROP TABLE IF EXISTS block_refs",
        "DROP TABLE IF EXISTS document_refs",
        """CREATE TABLE block_refs (
               source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
               target_block_id TEXT NOT NULL,
               PRIMARY KEY (source_block_id, target_block_id))""",
        """CREATE TABLE document_refs (
               source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
               target_document_id TEXT NOT NULL,
               PRIMARY KEY (source_block_id, target_document_id))""",
        # Undo steps were snapshots of the old shapes.
        "DELETE FROM document_history",
        "DELETE FROM block_user_attrs WHERE block_id NOT IN (SELECT id FROM blocks)",
        "DELETE FROM block_assets",
    ):
        connection.execute(statement)
    for block_id, sync in syncs:
        block_store._write_sync(connection, block_id, sync)
    for (document_id,) in connection.execute("SELECT id FROM documents").fetchall():
        block_store.refresh_derived(
            connection, document_id,
            [row[0] for row in connection.execute(
                "SELECT id FROM blocks WHERE document_id = ?", (document_id,)
            )],
        )
    block_store._rebuild_fts(connection)


def _move_document_sync_state(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        "SELECT id, provenance_json, sync_json FROM documents"
    ).fetchall():
        value = _provenance.loads(row["provenance_json"])
        if not any(key in value for key in _DOCUMENT_SYNC_FIELDS):
            continue
        sync = _provenance.loads(row["sync_json"])
        if value.get("source_removed"):
            sync["source_removed_at"] = value.get("source_removed_at") or ""
        for key in _DOCUMENT_SYNC_FIELDS:
            value.pop(key, None)
        connection.execute(
            "UPDATE documents SET provenance_json = ?, sync_json = ? WHERE id = ?",
            (_provenance.dumps(value) or "{}", json.dumps(sync), row["id"]),
        )


class _DocumentWriter:
    """Converts and writes one document's blocks."""

    def __init__(self, connection, document_id, children):
        self.connection = connection
        self.document_id = document_id
        self.children = children
        self.rows: dict[str, sqlite3.Row] = {}
        self.syncs: list[tuple[str, dict[str, Any]]] = []
        self.renamed: dict[str, str] = {}

    def write(self, roots: list[sqlite3.Row]) -> None:
        specs = [spec for row in roots for spec in self.convert(row)]
        self.insert(legalize(specs), None, _Parent(None, None, None, False))

    def convert(self, row: sqlite3.Row) -> list[BlockSpec]:
        """The stored block (and its stored children) as canonical specs.

        The first spec keeps the row's id; blocks made from part of the row
        (table rows and cells, a quote's paragraph) have none yet.
        """

        try:
            content = json.loads(row["content_json"])
        except (TypeError, ValueError):
            content = {}
        converted = block_legacy.convert(
            row["type"], _loads(row["attrs_json"]), content, str(row["text"])
        )
        first = converted[0]
        first.id = row["id"]
        self.rows[row["id"]] = row
        for child in self.children.get(row["id"], []):
            first.children.extend(self.convert(child))
        return converted

    def insert(self, specs: list[BlockSpec], parent_id: str | None, parent: "_Parent") -> None:
        keys = order_keys.keys_between(None, None, len(specs))
        for index, (spec, key) in enumerate(zip(specs, keys)):
            row = self.rows.get(spec.id) if spec.id else None
            if row is not None:
                provenance, sync = self.stored_provenance(spec, row)
                stamp = row
            else:
                provenance, sync = self.derived_provenance(spec, parent, index)
                stamp = parent.row
            block_id = spec.id or _new_id()
            self.connection.execute(
                """INSERT INTO blocks_new(id, document_id, parent_id, order_key, type, attrs_json,
                       content_json, payload_json, text, created_at, updated_at,
                       provenance_json, revision)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
                (
                    block_id, self.document_id, parent_id, key, spec.type,
                    _dumps(spec.attrs), _dumps_nullable(spec.content),
                    _dumps_nullable(spec.payload),
                    stamp["created_at"], stamp["updated_at"], _provenance.dumps(provenance),
                    stamp["revision"],
                ),
            )
            untouched = False
            if sync is not None:
                untouched = bool(sync.pop("untouched", False))
                self.syncs.append((block_id, sync))
            self.insert(spec.children, block_id, _Parent(
                stamp, (provenance or {}).get("source"), (provenance or {}).get("external_id"),
                untouched,
            ))

    def derived_provenance(self, spec: BlockSpec, parent: "_Parent", index: int):
        """A block made from part of a stored one: it follows its parent's source.

        Under a block from a living source it gets the positional source id
        that ``sources._prepare`` gives a child without its own.
        """

        if not parent.external_id:
            return None, None
        value = {"source": parent.source, "external_id": f"{parent.external_id}/{index}"}
        block_hash = schema.canonical_hash(spec.block())
        # Under a locally edited block the parts count as edited too.
        sync = {"source_hash": block_hash if parent.untouched else "edited-before-migration",
                "untouched": parent.untouched}
        return value, sync

    def stored_provenance(self, spec: BlockSpec, row: sqlite3.Row):
        """``(provenance, sync)`` of a stored block: re-import state moves out."""

        value = _provenance.loads(row["provenance_json"])
        legacy = {key: value.pop(key) for key in list(value) if key in _SYNC_FIELDS}
        if not legacy:
            return value or None, None
        old_hash = legacy.get("source_hash")
        untouched = old_hash is not None and old_hash == _legacy_hash(row["type"], _legacy_fingerprint(row))
        new_hash = schema.canonical_hash(spec.block())
        external = value.get("external_id")
        if untouched and isinstance(external, str) and external.startswith("~"):
            # A content-derived source id follows the new content hash.
            base, mark, suffix = external.partition("#")
            if base == "~" + old_hash:
                value["external_id"] = "~" + new_hash + mark + suffix
                self.renamed[external] = value["external_id"]
        sync: dict[str, Any] = {
            "source_hash": new_hash if untouched else old_hash,
            "dismissed_hash": legacy.get("dismissed_hash"),
            "source_removed": 1 if legacy.get("source_removed") else 0,
            "untouched": untouched,
        }
        conflict = legacy.get("conflict")
        if isinstance(conflict, dict):
            sync["conflict_json"] = json.dumps(_conflict(conflict), sort_keys=True)
        return value, sync


@dataclass(frozen=True)
class _Parent:
    row: sqlite3.Row | None
    source: str | None
    external_id: str | None
    untouched: bool


def _new_id() -> str:
    from . import block_store

    return block_store._new_ulid()


def _conflict(conflict: dict[str, Any]) -> dict[str, Any]:
    """An older conflict record in the canonical form."""

    content = conflict.get("source_content")
    if not isinstance(content, dict):
        content = {"markdown": str(conflict.get("source_markdown", ""))}
    converted = block_legacy.convert(
        str(conflict.get("source_type") or "paragraph"), conflict.get("source_attrs") or {},
        content, str(conflict.get("source_text") or ""),
    )[0]
    result = {
        "detected_at": conflict.get("detected_at"),
        "source_hash": conflict.get("source_hash"),
        "source": {"type": converted.type, "attrs": converted.attrs,
                   "content": converted.content, "payload": converted.payload},
    }
    if conflict.get("source_modified_at"):
        result["source_modified_at"] = conflict["source_modified_at"]
    return result


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dumps_nullable(value: object) -> str | None:
    return None if value is None else _dumps(value)
