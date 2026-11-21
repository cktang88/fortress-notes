"""HTTP validators and JSON responses, including every response with a note's tree.

API responses carry ``Cache-Control: no-cache``. A note's GET also carries a
strong ``ETag``: a client that keeps the note revalidates it with
``If-None-Match`` and gets ``304 Not Modified`` without a body. One ETag means
one thing everywhere (the GET and the writes that answer with the tree):
``etag("document", id, *validator)`` over ``block_store.document_validator``
(the note's own rows plus the newest change of every note or block it links to).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import Response

from . import block_store
from .config import get_settings

API_CACHE_CONTROL = "no-cache"


def etag(*parts: object) -> str:
    """A strong validator for whatever ``parts`` describe."""

    digest = hashlib.blake2b("\x1f".join(map(str, parts)).encode(), digest_size=12)
    return f'"{digest.hexdigest()}"'


def if_none_match(header: str | None, tag: str) -> bool:
    """Whether an ``If-None-Match`` header matches ``tag`` (weak comparison)."""

    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = tag.strip('"')
    for candidate in header.split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:]
        value = value.strip('"')
        if value == wanted:
            return True
    return False


def render(payload: Any) -> bytes:
    """JSON exactly as FastAPI's JSONResponse writes it."""

    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
    ).encode("utf-8")


def not_modified(tag: str) -> Response:
    return Response(status_code=304, headers={"ETag": tag, "Cache-Control": API_CACHE_CONTROL})


def json_response(body: bytes, tag: str | None = None, status_code: int = 200) -> Response:
    headers = {"Cache-Control": API_CACHE_CONTROL}
    if tag:
        headers["ETag"] = tag
    return Response(body, status_code=status_code, media_type="application/json", headers=headers)



def json_payload(payload: Any) -> Response:
    """Large payloads (the tree, the trash) encoded directly, skipping FastAPI's
    field-by-field ``jsonable_encoder`` walk."""

    return json_response(render(payload))


# --- A note's tree ---------------------------------------------------------------------


def _document_etag(document_id: str, validator: tuple) -> str:
    return etag("document", document_id, *validator)


def _current_body(document_id: str) -> tuple[str, bytes]:
    """(ETag, rendered tree) of a note as it is now, read from one snapshot; 404 if
    it is missing or trashed."""

    result = block_store.document_tree_with_validator(get_settings().block_db_path, document_id)
    if result is None:
        raise HTTPException(404, "Block document not found")
    validator, tree = result
    return _document_etag(document_id, validator), render(tree)


def document_response(request: Request, document_id: str) -> Response:
    """The note's tree, or 304 when ``If-None-Match`` has its ETag. Never writes."""

    validator = block_store.document_validator(get_settings().block_db_path, document_id)
    if validator is None:
        raise HTTPException(404, "Block document not found")
    tag = _document_etag(document_id, validator)
    if if_none_match(request.headers.get("if-none-match"), tag):
        return not_modified(tag)
    tag, body = _current_body(document_id)
    return json_response(body, tag)


def written_response(document_id: str, status_code: int = 200) -> Response:
    """The note as it is now, after a write, with ``X-Document-ETag``.

    The body and its ETag come from one read snapshot (never a tree from the
    write paired with a validator read later), so a client may keep the body
    under that ETag and revalidate it with ``If-None-Match``.
    """

    tag, body = _current_body(document_id)
    response = json_response(body, status_code=status_code)
    response.headers["X-Document-ETag"] = tag
    return response


def tree_response(document_id: str, tree: dict, validator: tuple) -> Response:
    """A tree (or metadata payload) a write returned with the validator it read.

    ``X-Document-ETag`` is the ETag ``GET /api/block-documents/{id}`` now
    answers 304 to.
    """

    response = json_response(render(tree))
    response.headers["X-Document-ETag"] = _document_etag(document_id, validator)
    return response
