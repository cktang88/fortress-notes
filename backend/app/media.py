"""Serving stored files at ``/media/<name>`` with long-lived HTTP caching.

Uploads are stored under a fresh ULID and imports under their SHA-256, so a
media URL never changes content: such files are served ``immutable`` for a
year. Their validator is the SHA-256 for content-addressed names (the same
bytes always have the same ETag), otherwise the file's size and modification
time. Range requests (seeking in audio and video, resuming big attachments)
and ``If-None-Match`` / ``If-Modified-Since`` come from Starlette's
``FileResponse``. Text sidecars (``<id>.txt``, left by older versions'
OCR) are revalidated instead.
"""

from __future__ import annotations

import mimetypes
import os
import re

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
# Explicit types: the platform's mimetypes table may not know these (or may
# call .m4a/.webm something else), and files are served with nosniff.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".bin": "application/octet-stream",
}


def content_type(name: str) -> str:
    suffix = os.path.splitext(name)[1].lower()
    return (
        CONTENT_TYPES.get(suffix)
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )


def is_immutable(name: str) -> bool:
    stem, suffix = os.path.splitext(name)
    return suffix.lower() != ".txt" and bool(_SHA256_RE.match(stem) or _ULID_RE.match(stem))


class MediaFiles(StaticFiles):
    def file_response(
        self,
        full_path,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        name = os.path.basename(str(full_path))
        stem = os.path.splitext(name)[0]
        headers = {"Cache-Control": IMMUTABLE if is_immutable(name) else REVALIDATE}
        if _SHA256_RE.match(stem):
            headers["ETag"] = f'"{stem}"'
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            media_type=content_type(name),
            headers=headers,
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
