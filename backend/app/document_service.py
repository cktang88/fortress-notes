"""Rendered document payloads and their one freshness validator (the ETag).

Every route that hands a client a note's tree goes through here: the GET and
the writes that answer with the tree. One ETag means one thing everywhere:
``http_cache.etag("document", id, *validator)`` over
``block_store.document_validator`` (the note's own rows plus the newest change
of every note or block it links to). A client that holds a body under an ETag
sends it back as ``If-None-Match`` and gets a 304.
"""

from __future__ import annotations

from pathlib import Path

from . import block_store, http_cache
from .config import get_settings


def db_path() -> Path:
    return get_settings().block_db_path


def etag_for(document_id: str, validator: tuple) -> str:
    return http_cache.etag("document", document_id, *validator)


def current_etag(document_id: str) -> str | None:
    """The ETag the note has now (None if it is missing or trashed). Reads, never writes."""

    validator = block_store.document_validator(db_path(), document_id)
    return None if validator is None else etag_for(document_id, validator)


def body(document_id: str) -> tuple[str, bytes] | None:
    """(ETag, rendered tree) of a note as it is now, read from one snapshot;
    None if missing or trashed."""

    result = block_store.document_tree_with_validator(db_path(), document_id)
    if result is None:
        return None
    validator, tree = result
    return etag_for(document_id, validator), http_cache.render(tree)


def rendered(document_id: str, tree: dict, validator: tuple) -> tuple[str, bytes]:
    """(ETag, body) of a tree a write just produced, with the validator read in that write."""

    return etag_for(document_id, validator), http_cache.render(tree)
