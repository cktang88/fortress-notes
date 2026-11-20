"""Markdown export of one note, and reading uploaded Markdown files.

Export is one way (the same text as the note's vault mirror, for other tools);
uploads are read as plain CommonMark/GFM/Obsidian Markdown into new notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from . import vault
from .block_markdown import parse_markdown
from .block_store import (
    connection_scope,
    create_document,
    derive_title as _derive_title,
    document_tree,
    markdown_import_provenance,
    render_markdown as _render_markdown,
)


def export_document_markdown(database_path: Path, document_id: str) -> str:
    """A note as one portable Markdown file, the same text as its vault mirror.

    Note links name notes as the vault does (``[[Title]]``); only blocks that
    something refers to carry an id (``^id``). Stored files keep their
    ``/media/`` URLs (a single file has no ``assets`` folder beside it).
    """

    document = document_tree(database_path, document_id)
    if document is None:
        raise KeyError("document not found")
    with connection_scope(database_path) as connection:
        layout = vault.workspace_layout(connection)
        anchors = vault.referenced_blocks(connection, document_id).get(document_id, set())
    resolved = (document.get("resolved_links") or {}).get("blocks") or {}
    links = layout.render_links(document_id, anchors, resolved.get)
    links.media_prefix = None
    body = _render_markdown(
        document["children"], links=links,
    )
    stem = Path(layout.paths.get(document_id, "x.md")).stem
    return vault.dump(vault.front_matter(document, stem, None), body)


WELCOME_NOTE = Path(__file__).with_name("welcome.md")


def seed_welcome_note(database_path: Path) -> None:
    """Add the bundled Welcome note (a new workspace's first note)."""

    post = frontmatter.loads(WELCOME_NOTE.read_text(encoding="utf-8"))
    note = vault.read_front_matter(post.metadata)
    now = datetime.now(timezone.utc).isoformat()
    create_document(database_path, "welcome", note.title or "Welcome", post.content,
                    now, now, provenance={"source": "manual"})


# --- Download filenames / uploaded import ----------------------------------

_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name: str, fallback: str = "Untitled") -> str:
    """Turn a title into a filename that is valid on every desktop OS."""

    cleaned = _UNSAFE_NAME.sub(" ", name).strip().strip(".")
    cleaned = " ".join(cleaned.split())[:100]
    return cleaned or fallback


@dataclass(frozen=True)
class UploadedNote:
    title: str
    body: str
    created_at: object | None = None
    provenance: dict | None = None
    # What wikilinks in other files of the same upload may call this note.
    names: tuple[str, ...] = ()


def parse_uploaded_markdown(filename: str, raw: bytes) -> UploadedNote:
    """Read one uploaded Markdown file; raises ValueError for unusable input."""

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not UTF-8 text") from exc
    try:
        post = frontmatter.loads(text)
    except Exception as exc:  # Malformed YAML is user data, not a server error.
        raise ValueError(f"could not read front matter: {exc}") from exc
    note = vault.read_front_matter(post.metadata)
    stem = Path(filename).stem.strip()
    title = (note.title or "").strip() or stem or _derive_title(post.content)
    # A `tags:` line from an old export is ignored: tags are no longer a feature.
    return UploadedNote(
        title=title[:200],
        body=post.content,
        created_at=note.created,
        provenance=markdown_import_provenance(
            post.metadata, Path(filename).name, datetime.now(timezone.utc).isoformat()
        ),
        names=tuple(name for name in (filename, stem, note.title, *note.aliases) if name),
    )


def parse_uploaded_batch(
    database_path: Path, notes: list[UploadedNote], new_id
) -> list[tuple[str, list]]:
    """``(document id, blocks)`` for notes uploaded together, wikilinks resolved among them.

    Every note gets a new id; links name the other files by file name, title or
    alias, then notes already in the workspace.
    """

    with connection_scope(database_path) as connection:
        links = vault.ImportLinks(vault.workspace_names(connection))
    ids: list[str] = []
    for note in notes:
        document_id = new_id()
        ids.append(document_id)
        links.add(document_id, *note.names)
    result = []
    for note, document_id in zip(notes, ids):
        links.document_id = document_id
        result.append((document_id, parse_markdown(note.body, links)))
    links.finish([spec for _, specs in result for spec in specs])
    return result
