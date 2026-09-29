"""Portable Markdown import and export for the canonical block store.

The functions here deliberately do not install routes or write compatibility
mirrors.  Export is pure, and import is insert-only: a source file is copied to
a timestamped backup before it is read, while an existing SQLite document is
never replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from .block_store import connection_scope, create_document, document_tree, navigation


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

    @property
    def conflict_count(self) -> int:
        return sum(item.status == "conflict" for item in self.items)


@dataclass(frozen=True)
class ImportPreview:
    """A read-only summary of what a directory import would do."""

    items: tuple[ImportItem, ...]

    @property
    def ready_count(self) -> int:
        return sum(item.status == "ready" for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def error_count(self) -> int:
        return sum(item.status == "error" for item in self.items)

    @property
    def conflict_count(self) -> int:
        return sum(item.status == "conflict" for item in self.items)


def preview_markdown_directory(database_path: Path, source_directory: Path) -> ImportPreview:
    """Inspect top-level Markdown files without writing backups or documents."""

    items: list[ImportItem] = []
    for source_path in sorted(source_directory.glob("*.md")):
        document_id = source_path.stem
        try:
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
            _read_import_source(source_path)
        except Exception as error:  # File data is an untrusted import boundary.
            items.append(ImportItem(source_path, document_id, "error", str(error)))
        else:
            items.append(ImportItem(source_path, document_id, "ready"))
    return ImportPreview(tuple(items))


def export_document_markdown(database_path: Path, document_id: str) -> str:
    """Return a document's portable Markdown without exposing SQLite block IDs."""

    document = document_tree(database_path, document_id)
    if document is None:
        raise KeyError("document not found")
    post = frontmatter.Post(
        _render_markdown(document["children"]),
        title=document["title"],
        status=document["status"],
        tags=document["tags"],
        created_at=document["created_at"],
        updated_at=document["updated_at"],
    )
    return frontmatter.dumps(post)


def export_all_markdown(database_path: Path) -> dict[str, str]:
    """Return every active document keyed by its stable SQLite document ID.

    This function intentionally returns data instead of writing files, so an
    integration layer can choose an explicit destination and overwrite policy.
    """

    return {
        document_id: export_document_markdown(database_path, document_id)
        for document_id in _navigation_document_ids(navigation(database_path))
    }


def import_markdown_directory(database_path: Path, source_directory: Path) -> ImportReport:
    """Back up and insert top-level Markdown files that SQLite does not yet own.

    A repeat import reports existing IDs as skipped.  Source files are never
    changed, including when decoding or YAML parsing fails.
    """

    backup_directory = _create_backup_directory(source_directory)
    items: list[ImportItem] = []
    for source_path in sorted(source_directory.glob("*.md")):
        document_id = source_path.stem
        try:
            raw = source_path.read_bytes()
            (backup_directory / source_path.name).write_bytes(raw)
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
            post, title, status, tags, created_at, updated_at = _read_import_source(source_path, raw)
            create_document(
                database_path,
                document_id,
                title,
                status,
                [str(tag) for tag in tags],
                post.content,
                created_at,
                updated_at,
            )
        except Exception as error:  # File data is an untrusted import boundary.
            items.append(ImportItem(source_path, document_id, "error", str(error)))
        else:
            items.append(ImportItem(source_path, document_id, "imported"))
    return ImportReport(backup_directory, tuple(items))


def _read_import_source(source_path: Path, raw: bytes | None = None):
    if raw is None:
        raw = source_path.read_bytes()
    post = frontmatter.loads(raw.decode("utf-8"))
    metadata = post.metadata
    title = str(metadata.get("title") or _derive_title(post.content))
    status = metadata.get("status", "rough")
    status = status if status in {"rough", "polished"} else "rough"
    tags = metadata.get("tags", []) or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    now = datetime.now(timezone.utc).isoformat()
    return (
        post,
        title,
        status,
        [str(tag) for tag in tags],
        metadata.get("created_at", now),
        metadata.get("updated_at", now),
    )


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


def _navigation_document_ids(data: dict[str, object]) -> list[str]:
    document_ids: list[str] = []

    def visit(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("kind") == "document" and isinstance(node.get("id"), str):
                document_ids.append(node["id"])
            elif node.get("kind") == "folder":
                visit(node.get("children"))

    visit(data.get("items"))
    return document_ids


def _render_markdown(nodes: object, depth: int = 0) -> str:
    if not isinstance(nodes, list):
        return ""
    fragments: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        source = _node_markdown(node)
        if node.get("type") == "divider" and not source:
            source = "---"
        if depth and source:
            prefix = "  " * depth
            source = "\n".join(f"{prefix}{line}" if line else line for line in source.splitlines())
        if source:
            fragments.append(source)
        children = _render_markdown(node.get("children"), depth + 1)
        if children:
            fragments.append(children)
    return "\n\n".join(fragments)


def _node_markdown(node: dict[str, object]) -> str:
    content = node.get("content")
    if isinstance(content, dict):
        markdown = content.get("markdown")
        if isinstance(markdown, str):
            return markdown
        blocknote = content.get("blocknote")
        if isinstance(blocknote, str):
            return blocknote
    text = node.get("text")
    return text if isinstance(text, str) else ""


def _derive_title(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return "Untitled"
