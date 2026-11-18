"""Provenance: where a note, or a block inside it, came from.

Every document stores one provenance object (``documents.provenance_json``), and
every block may store its own (``blocks.provenance_json``). A block without one
inherits its document's: the blocks a note was created with came from the same
place as the note. Blocks typed later, pasted, imported or folded in from another
note carry their own.

The JSON shape (all keys but ``source`` are optional)::

    {
      "source": "onenote_import",          # kind, see SOURCE_KINDS (any [a-z0-9_] word)
      "external_id": "{GUID}",             # id in the source (page/object GUID, ...)
      "source_path": "Notebook › Section › Page",   # or a file path
      "source_url": "https://...",          # pasted/web content
      "source_created_at": "...",           # timestamps from the source
      "source_modified_at": "...",
      "imported_at": "...",                 # first import into Fortress
      "synced_at": "...",                   # last re-import that changed something
      "importer": {"name": "onenote", "version": "1.0"},
      "origin": {"operation": "move_into" | "duplicate" | "split",
                 "document_id": "...", "document_title": "...", "block_id": "...",
                 "at": "..."},             # derived from another note/block
      "via": {"source": "markdown_import", "source_path": "x.md", "at": "..."},
      "extra": {...},                       # importer-specific details
      # set by re-imports (blocks only):
      "source_hash": "...",                 # hash of the content the source last wrote
      "conflict": {...},                    # source changed a locally edited block
      "source_removed": true,               # gone from the source; kept (edited locally)
      "backfilled": true                    # guessed by the schema migration
    }

Provenance is never indexed for search and never part of a block's text.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Known kinds. Any lower-case word is accepted, so new importers need no change here.
SOURCE_KINDS = (
    "manual",
    "capture",
    "markdown_import",
    "onenote_import",
    "paste",
    "web",
    "ai",
    "unknown",
)
DEFAULT_SOURCE = "manual"

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_STRING_FIELDS = {
    "external_id": 512,
    "source_path": 2000,
    "source_url": 2000,
    "source_created_at": 64,
    "source_modified_at": 64,
    "imported_at": 64,
    "synced_at": 64,
}
_ORIGIN_OPERATIONS = {"move_into", "duplicate", "split"}
# Written only by the server (re-imports and migrations), never accepted from clients.
INTERNAL_FIELDS = ("source_hash", "conflict", "source_removed", "backfilled")
_MAX_EXTRA_BYTES = 16_384

# A block's provenance in exported Markdown: one comment line right before the block.
COMMENT_PREFIX = "<!-- fortress:block "
_COMMENT_RE = re.compile(r"^<!-- fortress:block (\{.*\}) -->$")


class ProvenanceError(ValueError):
    pass


def normalize(value: object, *, trusted: bool = False) -> dict[str, Any]:
    """A clean provenance object; raises ProvenanceError for unusable input.

    ``trusted`` keeps the server-maintained fields (re-import state); input from
    API clients is never trusted.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProvenanceError("provenance must be an object")
    result: dict[str, Any] = {}
    source = value.get("source", DEFAULT_SOURCE)
    if not isinstance(source, str) or not _KIND_RE.match(source):
        raise ProvenanceError("provenance.source must be a lower-case word")
    result["source"] = source
    for name, limit in _STRING_FIELDS.items():
        item = value.get(name)
        if item is None or item == "":
            continue
        if not isinstance(item, str):
            item = str(item)
        if len(item) > limit:
            raise ProvenanceError(f"provenance.{name} is too long")
        result[name] = item
    url = result.get("source_url")
    if url is not None and not re.match(r"^(https?|file|onenote):", url, re.IGNORECASE):
        raise ProvenanceError("provenance.source_url must be an http(s) URL")
    importer = value.get("importer")
    if isinstance(importer, dict):
        clean = {
            key: str(importer[key])[:100]
            for key in ("name", "version")
            if importer.get(key) not in (None, "")
        }
        if clean:
            result["importer"] = clean
    elif importer is not None:
        raise ProvenanceError("provenance.importer must be an object")
    origin = value.get("origin")
    if isinstance(origin, dict):
        operation = origin.get("operation")
        if operation not in _ORIGIN_OPERATIONS:
            raise ProvenanceError("provenance.origin.operation is unknown")
        result["origin"] = {
            key: str(origin[key])[:512]
            for key in ("operation", "document_id", "document_title", "block_id", "at")
            if origin.get(key) not in (None, "")
        }
    elif origin is not None:
        raise ProvenanceError("provenance.origin must be an object")
    via = value.get("via")
    if isinstance(via, dict):
        result["via"] = {
            key: str(via[key])[:2000]
            for key in ("source", "source_path", "at")
            if via.get(key) not in (None, "")
        }
    for name in ("extra",) + (INTERNAL_FIELDS if trusted else ()):
        item = value.get(name)
        if item is None:
            continue
        try:
            encoded = json.dumps(item)
        except (TypeError, ValueError) as exc:
            raise ProvenanceError(f"provenance.{name} must be JSON") from exc
        if len(encoded) > _MAX_EXTRA_BYTES:
            raise ProvenanceError(f"provenance.{name} is too large")
        result[name] = item
    return result


def loads(raw: object) -> dict[str, Any]:
    """Stored provenance JSON (or NULL) as a dict; damaged values read as empty."""

    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def dumps(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def document_provenance(raw: object, source_kind: object = None) -> dict[str, Any]:
    value = loads(raw)
    value.setdefault("source", str(source_kind or DEFAULT_SOURCE))
    return value


def effective_block_provenance(
    block_raw: object, document_provenance_value: dict[str, Any]
) -> dict[str, Any]:
    """A block's own provenance, or its document's (marked ``inherited``)."""

    own = loads(block_raw)
    if own:
        own.setdefault("source", DEFAULT_SOURCE)
        return own
    inherited = {
        key: value
        for key, value in document_provenance_value.items()
        # The document's source id names the document, not this block.
        if key not in {"external_id", "origin"} and key not in INTERNAL_FIELDS
    }
    inherited.setdefault("source", DEFAULT_SOURCE)
    inherited["inherited"] = True
    return inherited


def materialize(
    block_raw: object, document_provenance_value: dict[str, Any]
) -> dict[str, Any]:
    """The block's provenance as stored data (before it leaves its document)."""

    value = effective_block_provenance(block_raw, document_provenance_value)
    value.pop("inherited", None)
    return value


def derived_copy(value: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    """Provenance for a copy of a block: same source, but not the source's block."""

    copy = {
        key: item
        for key, item in value.items()
        if key not in {"external_id", "inherited", "origin"} and key not in INTERNAL_FIELDS
    }
    copy.setdefault("source", DEFAULT_SOURCE)
    copy["origin"] = {key: item for key, item in origin.items() if item not in (None, "")}
    return copy


def content_hash(block_type: str, text: str) -> str:
    """What a re-import compares to tell untouched blocks from locally edited ones."""

    digest = hashlib.sha256(f"{block_type}\n{text}".encode("utf-8")).hexdigest()
    return digest[:32]


# --- Markdown representation -------------------------------------------------


def comment(value: dict[str, Any]) -> str:
    """One-line HTML comment carrying a block's provenance (survives Markdown tools)."""

    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    # JSON never needs a raw '>' outside strings; escaping it means '-->' can't occur.
    encoded = encoded.replace(">", "\\u003e")
    return f"{COMMENT_PREFIX}{encoded} -->"


def parse_comment(source: str) -> dict[str, Any] | None:
    match = _COMMENT_RE.match(source.strip())
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    try:
        return normalize(value, trusted=True)
    except ProvenanceError:
        return None


def exportable_block_provenance(
    block_raw: object, document_provenance_value: dict[str, Any]
) -> dict[str, Any] | None:
    """What a Markdown export writes for a block: only what the document doesn't say."""

    own = loads(block_raw)
    if not own:
        return None
    if own == {"source": document_provenance_value.get("source", DEFAULT_SOURCE)}:
        return None
    return own


def front_matter(value: dict[str, Any]) -> dict[str, Any]:
    """Document provenance as YAML front matter (plain types only)."""

    return {key: item for key, item in value.items() if item not in (None, "", {}, [])}
