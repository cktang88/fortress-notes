"""Image storage: save pasted images, run VLM caption + OCR, keep a text sidecar.

Each image `<id>.<ext>` gets a sibling `<id>.txt` holding caption + OCR text, which
the search layer folds into the owning note so images are full-text/embedding searchable.
"""

import re
from threading import Lock
from pathlib import Path

from . import assets, block_store, vision
from .config import get_settings

_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# Matches /media/<id>.<ext> references inside a note body.
_ASSET_RE = re.compile(r"/media/([A-Za-z0-9]+)\.(png|jpe?g|gif|webp)")
_extraction_lock = Lock()


def save_bytes(data: bytes, content_type: str) -> tuple[str, Path]:
    """Save image bytes and return (public_url, path). Fast — no model work."""
    ext = _EXT_BY_TYPE.get(content_type, ".png")
    storage_type = content_type if content_type in _EXT_BY_TYPE else "image/png"
    url, _, path = assets.save_upload(data, f"image{ext}", storage_type)
    return url, path


def extract_and_store(path: Path) -> str:
    """Reuse or compute caption + OCR and write per-image and content-hash caches.

    Heavy/slow — run in a background thread, not on the request path.
    """
    content_hash = assets.content_hash(path.read_bytes())
    settings = get_settings()
    image_sidecar = path.with_suffix(".txt")
    hash_sidecar = path.with_name(f"{content_hash}.txt")

    # Model instances are shared between background upload threads. Keeping cache
    # lookup and inference under one lock also prevents duplicate work for same bytes.
    with _extraction_lock:
        text = _read_cached_text(hash_sidecar, settings.vlm_caption_enabled)
        if not text:
            text = _read_cached_text(image_sidecar, settings.vlm_caption_enabled)
        if not text:
            text = vision.describe_image(path)
        if not text.strip():
            return ""

        _write_cache(image_sidecar, text)
        _write_cache(hash_sidecar, text)
        if settings.block_db_enabled:
            block_store.store_asset_text(settings.block_db_path, content_hash, text)
    return text


def _read_cached_text(path: Path, caption_enabled: bool) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    if not text.strip():
        return ""
    # Older OCR-only sidecars use this prefix. Recompute them after captions are enabled.
    if caption_enabled and text.lstrip().startswith("Text in image:"):
        return ""
    return text


def _write_cache(path: Path, text: str) -> None:
    if path.is_symlink():
        return
    path.write_text(text, encoding="utf-8")


def _sidecar_text(image_id: str) -> str:
    sidecar = get_settings().assets_path / f"{image_id}.txt"
    return sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""


def collect_image_texts(body: str) -> dict[str, str]:
    """Map of image id -> cached caption/OCR text, for every image in the body.

    Read from the compute-once sidecar cache; this is mirrored into note frontmatter
    on save so the models never re-run for an image.
    """
    texts: dict[str, str] = {}
    for image_id, _ext in _ASSET_RE.findall(body):
        text = _sidecar_text(image_id)
        if text:
            texts[image_id] = text
    return texts


_IMG_HTML_RE = re.compile(
    r"<img\b[^>]*?/media/([A-Za-z0-9]+)\.(?:png|jpe?g|gif|webp)[^>]*>", re.IGNORECASE
)
_IMG_MD_RE = re.compile(
    r"!\[[^\]]*\]\(/media/([A-Za-z0-9]+)\.(?:png|jpe?g|gif|webp)[^)]*\)", re.IGNORECASE
)


def inline_for_llm(body: str) -> str:
    """Replace each image reference with its caption/OCR text for AI review."""

    def repl(match: re.Match) -> str:
        text = _sidecar_text(match.group(1)).strip()
        return f"[image: {text}]" if text else "[image]"

    return _IMG_MD_RE.sub(repl, _IMG_HTML_RE.sub(repl, body))


def note_search_text(title: str, body: str) -> str:
    """Title + body + image caption/OCR text (read from the sidecar cache, so it's
    always fresh even before the frontmatter mirror is updated). Used everywhere a note
    is indexed so full-text and embedding search both cover image content."""
    base = f"{title}\n\n{body}"
    extra = "\n".join(collect_image_texts(body).values())
    return f"{base}\n\n{extra}" if extra else base
