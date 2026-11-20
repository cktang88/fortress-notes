"""Portable Markdown import and export for the canonical block store.

The functions here deliberately do not install routes or write compatibility
mirrors.  Export is pure, and import is insert-only: a source file is copied to
a timestamped backup before it is read, while an existing SQLite document is
never replaced.
"""

from __future__ import annotations

import hashlib
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
    create_folder,
    derive_title as _derive_title,
    document_tree,
    markdown_import_provenance,
    render_markdown as _render_markdown,
)


@dataclass(frozen=True)
class ImportItem:
    """The result for one source Markdown file."""

    source_path: Path
    document_id: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ImportReport:
    """Results of one non-destructive directory import."""

    backup_directory: Path
    items: tuple[ImportItem, ...]

    @property
    def imported_count(self) -> int:
        return sum(item.status == "imported" for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def error_count(self) -> int:
        return sum(item.status == "error" for item in self.items)


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
        document["children"],
        document_provenance=document.get("provenance") or {"source": "manual"},
        links=links,
    )
    stem = Path(layout.paths.get(document_id, "x.md")).stem
    return vault.dump(vault.front_matter(document, stem, None), body)


def import_markdown_directory(database_path: Path, source_directory: Path) -> ImportReport:
    """Back up and insert the Markdown files of a folder (a Fortress or Obsidian vault).

    Files in subfolders go into folders of the same names. A note keeps the
    ``id`` its front matter gives (else a file named like an id keeps that
    name; else its id is made from its path), so a repeat import reports the
    notes it already made as skipped. Wikilinks resolve across all the files
    (by name, path, title or alias) and to notes already in the workspace.
    Source files are never changed, including when decoding or YAML parsing fails.
    """

    backup_directory = _create_backup_directory(source_directory)
    items: list[ImportItem] = []
    now = datetime.now(timezone.utc).isoformat()
    document_tree(database_path, "")  # a brand-new database is initialized
    sources: list[tuple[Path, str, str, frontmatter.Post, vault.NoteMetadata]] = []
    for source_path in _markdown_files(source_directory):
        relative = source_path.relative_to(source_directory).as_posix()
        document_id = _fallback_id(relative)
        try:
            raw = source_path.read_bytes()
            backup = backup_directory / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(raw)
            post = frontmatter.loads(raw.decode("utf-8"))
            note = vault.read_front_matter(post.metadata)
            if note.id and _ID_RE.match(note.id):
                document_id = note.id
            state = _document_state(database_path, document_id)
            if state == "active":
                items.append(ImportItem(source_path, document_id, "skipped", "document exists"))
                continue
            if state == "deleted":
                items.append(
                    ImportItem(
                        source_path,
                        document_id,
                        "conflict",
                        "document is soft-deleted; restore or purge it before importing",
                    )
                )
                continue
        except Exception as error:  # File data is an untrusted import boundary.
            items.append(ImportItem(source_path, document_id, "error", str(error)))
            continue
        sources.append((source_path, relative, document_id, post, note))

    with connection_scope(database_path) as connection:
        links = vault.ImportLinks(vault.workspace_names(connection))
    for source_path, relative, document_id, post, note in sources:
        links.add(document_id, relative, Path(relative).stem, note.title, *note.aliases, note.id)
    parsed: list[list] = []
    for _source_path, _relative, document_id, post, _note in sources:
        links.document_id = document_id
        try:
            parsed.append(parse_markdown(post.content, links))
        except Exception as error:  # noqa: BLE001 - reported per file below
            parsed.append([error])
    links.finish([spec for specs in parsed for spec in specs if not isinstance(spec, Exception)])

    folders: dict[tuple[str, ...], str | None] = {(): None}
    for (source_path, relative, document_id, post, note), specs in zip(sources, parsed):
        try:
            if specs and isinstance(specs[0], Exception):
                raise specs[0]
            folder_id = _folder_for(database_path, Path(relative).parent.parts, folders)
            create_document(
                database_path,
                document_id,
                note.title or Path(relative).stem or _derive_title(post.content),
                note.status,
                post.content,
                note.created or now,
                note.updated or now,
                folder_id=folder_id,
                provenance=markdown_import_provenance(post.metadata, relative, now),
                specs=specs,
            )
        except Exception as error:  # File data is an untrusted import boundary.
            items.append(ImportItem(source_path, document_id, "error", str(error)))
        else:
            items.append(ImportItem(source_path, document_id, "imported"))
    return ImportReport(backup_directory, tuple(items))


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _markdown_files(directory: Path) -> list[Path]:
    """The Markdown files of a vault (not in hidden folders, nor in ``assets``)."""

    found = []
    for path in directory.rglob("*.md"):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") for part in parts):
            continue
        if len(parts) > 1 and parts[0] in vault.RESERVED_TOP_LEVEL:
            continue
        if path.is_file():
            found.append(path)
    return sorted(found, key=lambda path: path.relative_to(directory).as_posix())


def _fallback_id(relative: str) -> str:
    """The id of a note whose front matter has none: its file name if that is
    id-shaped (files written by older versions), else one made from its path."""

    stem = Path(relative).stem
    if "/" not in relative and re.fullmatch(r"[a-z0-9_-]{1,128}|[0-7][0-9A-HJKMNP-TV-Z]{25}", stem):
        return stem
    return "md-" + hashlib.sha1(relative.encode("utf-8")).hexdigest()[:24]


def _folder_for(database_path: Path, parts: tuple[str, ...],
                known: dict[tuple[str, ...], str | None]) -> str | None:
    """The folder for a file's directory, found by name or created (parents first)."""

    if parts in known:
        return known[parts]
    parent_id = _folder_for(database_path, parts[:-1], known)
    with connection_scope(database_path) as connection:
        row = connection.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS ? ORDER BY position LIMIT 1",
            (parts[-1], parent_id),
        ).fetchone()
    folder_id = str(row["id"]) if row else str(create_folder(database_path, parts[-1], parent_id)["id"])
    known[parts] = folder_id
    return folder_id


def _document_state(database_path: Path, document_id: str) -> str | None:
    # document_tree initializes a brand-new database through its public contract.
    document_tree(database_path, document_id)
    with connection_scope(database_path) as connection:
        row = connection.execute(
            "SELECT deleted_at FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    if row is None:
        return None
    return "deleted" if row["deleted_at"] is not None else "active"


def import_markdown_if_empty(database_path: Path, source_directory: Path) -> ImportReport | None:
    """Run the safe initial import only when the canonical store has no documents."""

    document_tree(database_path, "")
    with connection_scope(database_path) as connection:
        has_documents = connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
    if has_documents is not None:
        return None
    return import_markdown_directory(database_path, source_directory)


def _create_backup_directory(source_directory: Path) -> Path:
    root = source_directory / ".fortress-import-backups"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = root / timestamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{timestamp}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


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
    status: str
    body: str
    created_at: object | None = None
    provenance: dict | None = None
    # What wikilinks in other files of the same upload may call this note.
    id: str | None = None
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
        status=note.status,
        body=post.content,
        created_at=note.created,
        provenance=markdown_import_provenance(
            post.metadata, Path(filename).name, datetime.now(timezone.utc).isoformat()
        ),
        id=note.id if note.id and _ID_RE.match(note.id) else None,
        names=tuple(name for name in (filename, stem, note.title, *note.aliases) if name),
    )


def parse_uploaded_batch(
    database_path: Path, notes: list[UploadedNote], new_id
) -> list[tuple[str, list]]:
    """``(document id, blocks)`` for notes uploaded together, wikilinks resolved among them.

    A note keeps the id its front matter gives when no note has it yet (our own
    export read back); links name the other files by file name, title or alias,
    then notes already in the workspace.
    """

    from .block_store import document_exists_any

    with connection_scope(database_path) as connection:
        links = vault.ImportLinks(vault.workspace_names(connection))
    ids: list[str] = []
    for note in notes:
        document_id = note.id if note.id and not document_exists_any(database_path, note.id) \
            and note.id not in ids else new_id()
        ids.append(document_id)
        links.add(document_id, *note.names, note.id)
    result = []
    for note, document_id in zip(notes, ids):
        links.document_id = document_id
        result.append((document_id, parse_markdown(note.body, links)))
    links.finish([spec for _, specs in result for spec in specs])
    return result
