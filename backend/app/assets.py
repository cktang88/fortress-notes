"""Local storage for uploaded files (image and file blocks)."""

from pathlib import Path
from hashlib import sha256

from ulid import ULID

from .config import get_settings

_EXTENSIONS_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}


def safe_filename(filename: str | None) -> str:
    """Keep a display filename while removing path and control characters."""

    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if character.isprintable()).strip()
    return cleaned if cleaned not in {"", ".", ".."} else "file"


def is_supported_image_type(content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type in _IMAGE_TYPES


def upload_path(content_type: str | None, extension: str | None = None) -> Path:
    """Where a new upload of this type goes: a fresh ULID name, never reused."""

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    extension = extension or _EXTENSIONS_BY_TYPE.get(media_type, ".bin")
    return get_settings().assets_path / f"{ULID()}{extension}"


def content_hash(data: bytes) -> str:
    """Return the stable SHA-256 key used for asset metadata."""
    return sha256(data).hexdigest()
