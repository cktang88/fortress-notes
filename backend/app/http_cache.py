"""HTTP validators, conditional GETs and small response caches.

GET responses of the API carry a strong ``ETag`` derived from what they show
(a note's change sequence, the workspace change sequence, ...) and
``Cache-Control: no-cache``: clients may keep them but revalidate every time
with ``If-None-Match``, answered ``304 Not Modified`` without a body. Bodies are
rendered once per validator and kept in small in-memory caches, so a repeated
GET (from another window or after a 304 miss) skips the SQLite work and the
JSON encoding.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from starlette.responses import Response

API_CACHE_CONTROL = "no-cache"


def etag(*parts: object) -> str:
    """A strong validator for whatever ``parts`` describe."""

    digest = hashlib.blake2b("\x1f".join(map(str, parts)).encode(), digest_size=12)
    return f'"{digest.hexdigest()}"'


def body_etag(body: bytes) -> str:
    return f'"{hashlib.blake2b(body, digest_size=12).hexdigest()}"'


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


def conditional(
    request_header: str | None,
    tag: str,
    build: Callable[[], bytes],
    cache: "ResponseCache | None" = None,
    key: object = None,
) -> Response:
    """304 when the client has ``tag``; otherwise the (cached) body."""

    if if_none_match(request_header, tag):
        return not_modified(tag)
    body = cache.get(key, tag) if cache is not None else None
    if body is None:
        body = build()
        if cache is not None:
            cache.put(key, tag, body)
    return json_response(body, tag)


class ResponseCache:
    """A thread-safe LRU of rendered bodies, each valid for one validator."""

    def __init__(self, max_entries: int, max_bytes: int):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[object, tuple[str, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: object, tag: str) -> bytes | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] != tag:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item[1]

    def put(self, key: object, tag: str, body: bytes) -> None:
        if len(body) > self.max_bytes // 4:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old[1])
            self._items[key] = (tag, body)
            self._bytes += len(body)
            while self._items and (
                len(self._items) > self.max_entries or self._bytes > self.max_bytes
            ):
                _, (_, evicted) = self._items.popitem(last=False)
                self._bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
