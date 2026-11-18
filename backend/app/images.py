"""Text read from the pictures and PDFs a note embeds (see ``ocr.py``), by media URL.

The text lives in ``assets.text``; these helpers fold it into the views of a note
that are not the block index: the Markdown mirror's front matter.
"""

import re

from . import block_store
from .config import get_settings

# /media/<name> references inside a note body.
_MEDIA_RE = re.compile(r"/media/([A-Za-z0-9_-]+\.[A-Za-z0-9]+)")


def _texts(media_names: list[str]) -> dict[str, str]:
    if not media_names:
        return {}
    db_path = get_settings().block_db_path
    block_store.initialize(db_path)
    placeholders = ",".join("?" for _ in media_names)
    with block_store.connection_scope(db_path) as connection:
        rows = connection.execute(
            f"""SELECT media_name, text FROM assets
                 WHERE media_name IN ({placeholders}) AND trim(text) <> ''""",
            media_names,
        ).fetchall()
    return {row["media_name"]: row["text"] for row in rows}


def collect_image_texts(body: str) -> dict[str, str]:
    """Media name -> text read from it, for every picture/PDF the body references."""

    return _texts(list(dict.fromkeys(_MEDIA_RE.findall(body))))
