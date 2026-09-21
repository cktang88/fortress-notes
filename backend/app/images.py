"""Image storage: save pasted images, run VLM caption + OCR, keep a text sidecar.

Each image `<id>.<ext>` gets a sibling `<id>.txt` holding caption + OCR text, which
the search layer folds into the owning note so images are full-text/embedding searchable.
"""

import re
from pathlib import Path

from ulid import ULID

from . import vision
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


def save_bytes(data: bytes, content_type: str) -> tuple[str, Path]:
    """Save image bytes and return (public_url, path). Fast — no model work."""
    ext = _EXT_BY_TYPE.get(content_type, ".png")
    image_id = str(ULID())
    path = get_settings().assets_path / f"{image_id}{ext}"
    path.write_bytes(data)
    return f"/media/{image_id}{ext}", path


def extract_and_store(path: Path) -> str:
    """Run caption + OCR for an image and write the `<id>.txt` sidecar cache.

    Heavy/slow — run in a background thread, not on the request path.
    """
    text = vision.describe_image(path)
    if text:
        path.with_suffix(".txt").write_text(text, encoding="utf-8")
    return text


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
    """Replace each image reference with its caption/OCR text, so an LLM 'sees' the
    image content (used for fact-check, consistency, heal)."""

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
