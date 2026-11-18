"""Rendered document payloads and their one freshness validator (the ETag).

Every route that hands a client a note's tree goes through here: the single
GET, the batch read, and the writes that answer with the tree. One ETag means
one thing everywhere: ``http_cache.etag("document", id, *validator)`` over
``block_store.document_validator`` (the note's own rows plus the newest change
of every note or block it links to). A client that holds a body under an ETag
can send it back as ``If-None-Match`` or as a batch ``known`` entry.

Bodies are rendered at most once per ETag and shared by all of those routes.
"""

from __future__ import annotations

from pathlib import Path

from . import block_store, http_cache
from .config import get_settings


def db_path() -> Path:
    return get_settings().block_db_path


# Rendered note trees by ETag: a repeated read skips SQLite and JSON encoding.
_documents = http_cache.ResponseCache(max_entries=256, max_bytes=64 * 1024 * 1024)
# Other cached responses (navigation, trash, info, backlinks), keyed by route.
responses = http_cache.ResponseCache(max_entries=256, max_bytes=32 * 1024 * 1024)


def clear_caches() -> None:
    _documents.clear()
    responses.clear()


def etag_for(document_id: str, validator: tuple) -> str:
    return http_cache.etag("document", document_id, *validator)


def current_etag(document_id: str) -> str | None:
    """The ETag the note has now (None if it is missing or trashed). Reads, never writes."""

    validator = block_store.document_validator(db_path(), document_id)
    return None if validator is None else etag_for(document_id, validator)


def body(document_id: str, tag: str | None = None) -> tuple[str, bytes] | None:
    """(ETag, rendered tree) of a note as it is now; None if missing or trashed.

    ``tag``: the current ETag when the caller just read it, so a cached body is
    served without opening a read snapshot. Otherwise the tree and its
    validator are read from one snapshot and the body is cached under its ETag.
    """

    path = db_path()
    key = (path, document_id)
    if tag is not None:
        cached = _documents.get(key, tag)
        if cached is not None:
            return tag, cached
    result = block_store.document_tree_with_validator(path, document_id)
    if result is None:
        return None
    validator, tree = result
    tag = etag_for(document_id, validator)
    rendered = http_cache.render(tree)
    _documents.put(key, tag, rendered)
    return tag, rendered


def remember(document_id: str, tree: dict, validator: tuple, cache: bool = True) -> tuple[str, bytes]:
    """Render a tree a write just produced (with the validator read in that write).

    Cached for the reads that follow unless ``cache`` is false (a payload
    without ``children`` is not the note's body).
    """

    tag = etag_for(document_id, validator)
    rendered = http_cache.render(tree)
    if cache:
        _documents.put((db_path(), document_id), tag, rendered)
    return tag, rendered


def batch(ids: list[str], known: dict[str, str]) -> bytes:
    """Several notes in one answer, for prefetching.

    ``known`` maps ids to the ETags the client holds: those that still match
    come back in ``unchanged`` (no body). The rest come back in ``documents``
    with their ETags in ``etags``; ``missing`` lists notes that are gone.
    """

    bodies: list[bytes] = []
    etags: dict[str, str] = {}
    unchanged: list[str] = []
    missing: list[str] = []
    for document_id in dict.fromkeys(ids):
        tag = current_etag(document_id)
        if tag is None:
            missing.append(document_id)
            continue
        # As If-None-Match compares (a gzip variant's "-gzip" ETag matches too).
        if http_cache.if_none_match(known.get(document_id), tag):
            unchanged.append(document_id)
            continue
        rendered = body(document_id, tag)
        if rendered is None:
            missing.append(document_id)
            continue
        etags[document_id] = rendered[0]
        bodies.append(rendered[1])
    return b"".join([
        b'{"documents":[', b",".join(bodies), b'],"etags":', http_cache.render(etags),
        b',"unchanged":', http_cache.render(unchanged),
        b',"missing":', http_cache.render(missing), b"}",
    ])
