"""HTTP validators and JSON responses.

API responses carry ``Cache-Control: no-cache``. A note's GET also carries a
strong ``ETag`` (see ``document_service``): a client that keeps the note
revalidates it with ``If-None-Match`` and gets ``304 Not Modified`` without a body.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from starlette.responses import Response

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

