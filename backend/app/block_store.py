"""SQLite foundation for the block-centric workspace.

The current note API still reads Markdown. This module creates the new canonical
shape and imports existing Markdown once, so later API/UI work can switch over
without making the first migration destructive.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from markdown_it import MarkdownIt
from markdown_it.token import Token
from ulid import ULID

from . import block_types as _types
from . import provenance as _provenance
from .models import BlockOperation, BlockReviewContextKind

SCHEMA_VERSION = 13
DB_FILENAME = ".fortress.sqlite3"

_TASK_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+\[([ xX])\]\s+")
_BLOCK_REF_RE = re.compile(r"\(\(\s*([A-Za-z0-9_-]+)(?:\s+[\"']([^\"']*)[\"'])?\s*\)\)")
_DOCUMENT_REF_RE = re.compile(r"\[\[([A-Za-z0-9_-]+)(?:\|([^\]]+))?\]\]")
_MEDIA_REF_RE = re.compile(r"/media/([A-Za-z0-9_-]+\.[A-Za-z0-9]+)")
_UPLOAD_ID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
}


def _new_ulid() -> str:
    """Generate a stable-format ID for records created by the block store."""

    return str(ULID())


@dataclass(frozen=True)
class ImportedBlock:
    id: str
    type: str
    source: str
    attrs: dict[str, object] = field(default_factory=dict)
    children: list["ImportedBlock"] = field(default_factory=list)
    # Restored from a ``<!-- fortress:block {...} -->`` line in imported Markdown.
    provenance: dict[str, object] | None = None
    # Structured content (tables, images, rich text, ...) and its searchable text;
    # None means the plain ``{"markdown": source}`` with ``source`` as the text.
    content: dict[str, object] | None = None
    text: str | None = None

    def stored_content(self) -> dict[str, object]:
        return self.content if self.content is not None else {"markdown": self.source}

    def stored_text(self) -> str:
        return self.text if self.text is not None else self.source


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_kind TEXT,
    source_external_id TEXT,
    source_name TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('rough', 'polished')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    folder_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    title_auto INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    source_external_id TEXT,
    sync_json TEXT NOT NULL DEFAULT '{}',
    last_opened_at TEXT
);

CREATE INDEX IF NOT EXISTS folders_parent_position ON folders(parent_id, position);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provenance_json TEXT,
    revision INTEGER,
    UNIQUE (document_id, parent_id, position)
);

CREATE INDEX IF NOT EXISTS blocks_document_position
    ON blocks(document_id, parent_id, position);

-- The search corpus fingerprint reads MAX(updated_at) on every search: an index
-- makes that a lookup instead of a scan of every block.
CREATE INDEX IF NOT EXISTS blocks_updated_at ON blocks(updated_at);

CREATE TABLE IF NOT EXISTS block_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);

CREATE TABLE IF NOT EXISTS block_user_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);

CREATE TABLE IF NOT EXISTS block_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_block_id, label)
);

CREATE TABLE IF NOT EXISTS document_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_block_id, target_document_id, label)
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_id TEXT,
    operation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    patches_json TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0 CHECK (undone IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_history_order
    ON document_history(document_id, id);

CREATE TABLE IF NOT EXISTS assets (
    content_hash TEXT NOT NULL,
    media_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_updated_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, media_name)
);

CREATE TABLE IF NOT EXISTS block_assets (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    media_name TEXT NOT NULL,
    PRIMARY KEY (block_id, content_hash, media_name),
    FOREIGN KEY (content_hash, media_name)
        REFERENCES assets(content_hash, media_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transaction_receipts (
    transaction_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_check_cache (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'text',
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    block_id UNINDEXED,
    document_id UNINDEXED,
    text
);
"""

_MIGRATIONS = {
    2: """
    CREATE TABLE IF NOT EXISTS document_refs (
        source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
        target_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        label TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source_block_id, target_document_id, label)
    );
    """,
    3: """
    CREATE TABLE IF NOT EXISTS folders (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS folders_parent_position ON folders(parent_id, position);
    CREATE INDEX IF NOT EXISTS documents_folder_position ON documents(folder_id, position);
    """,
    4: "",
    5: """
    CREATE TABLE IF NOT EXISTS block_user_attrs (
        block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (block_id, name)
    );
    """,
    6: """
    CREATE TABLE IF NOT EXISTS assets (
        content_hash TEXT NOT NULL,
        media_name TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        file_path TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (content_hash, media_name)
    );
    CREATE TABLE IF NOT EXISTS block_assets (
        block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
        content_hash TEXT NOT NULL,
        media_name TEXT NOT NULL,
        PRIMARY KEY (block_id, content_hash, media_name),
        FOREIGN KEY (content_hash, media_name)
            REFERENCES assets(content_hash, media_name) ON DELETE CASCADE
    );
    """,
    7: "",
    8: """
    CREATE TABLE IF NOT EXISTS document_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        patches_json TEXT NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0 CHECK (undone IN (0, 1)),
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS document_history_order
        ON document_history(document_id, id);
    """,
    9: """
    CREATE TABLE IF NOT EXISTS transaction_receipts (
        transaction_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        request_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    10: """
    CREATE TABLE IF NOT EXISTS link_check_cache (
        url TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        checked_at TEXT NOT NULL
    );
    """,
    12: "",
    13: "",
    11: """
    CREATE TABLE IF NOT EXISTS saved_searches (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        query TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'text',
        filters_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    """,
}

_ready_paths: set[Path] = set()
# Creating or migrating a database is check-then-write; concurrent first use from
# several threads would otherwise race (duplicate version rows, lock errors).
_initialize_lock = threading.Lock()
UNSET = object()


def database_path(notes_path: Path) -> Path:
    return notes_path / DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # Switching modes needs an exclusive lock and fails at once ("database is
        # locked") while another connection is open, so only switch when needed.
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def connection_scope(path: Path, immediate: bool = False):
    """One connection and transaction; ``immediate`` takes the write lock up front.

    Writes that read sibling positions before shifting them need ``immediate``:
    otherwise two requests can read the same positions before either writes.
    """

    connection = connect(path)
    try:
        with connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        connection.close()


def initialize(path: Path) -> None:
    with _initialize_lock:
        _initialize(path)
    _ready_paths.add(path.resolve())


def _initialize(path: Path) -> None:
    with connection_scope(path) as connection:
        connection.executescript(_SCHEMA)
        row = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )
        elif row["version"] > SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported block database version {row['version']} (expected {SCHEMA_VERSION})"
            )
        else:
            current = int(row["version"])
            while current < SCHEMA_VERSION:
                current += 1
                _apply_migration(connection, current)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (current, _now()),
                )
        # Migration 3 creates this index; _SCHEMA can't (older documents tables have
        # no folder_id until that migration runs), so fresh databases get it here.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS documents_folder_position ON documents(folder_id, position)"
        )
        # Re-imports find "the note this source page became" by its source key.
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS documents_source_key
                   ON documents(source_kind, source_external_id)
                WHERE source_external_id IS NOT NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS folders_source_key
                   ON folders(source_kind, source_external_id)
                WHERE source_external_id IS NOT NULL"""
        )


def register_asset(db_path: Path, asset_path: Path, content_hash: str, mime_type: str) -> None:
    """Record an uploaded asset before a block references it."""
    initialize(db_path)
    with connection_scope(db_path) as connection:
        connection.execute(
            """INSERT INTO assets(content_hash, media_name, mime_type, file_path, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, media_name) DO UPDATE SET
                 mime_type = excluded.mime_type, file_path = excluded.file_path""",
            (content_hash, asset_path.name, mime_type, str(asset_path), _now()),
        )


def store_asset_text(path: Path, content_hash: str, text: str) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            "UPDATE assets SET text = ?, text_updated_at = ? "
            "WHERE content_hash = ? AND text <> ?",
            (text, _now(), content_hash, text),
        )
        if cursor.rowcount == 0:
            return
        rows = connection.execute(
            """SELECT DISTINCT blocks.id, blocks.document_id, blocks.text
                 FROM blocks JOIN block_assets ON block_assets.block_id = blocks.id
                 JOIN documents ON documents.id = blocks.document_id
                WHERE block_assets.content_hash = ? AND documents.deleted_at IS NULL""",
            (content_hash,),
        ).fetchall()
        for row in rows:
            _refresh_fts(connection, row["id"], row["document_id"], row["text"])


def get_link_check_cache(path: Path, urls: list[str]) -> dict[str, dict[str, str]]:
    if not urls:
        return {}
    initialize(path)
    placeholders = ",".join("?" for _ in urls)
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"SELECT url, status, checked_at FROM link_check_cache WHERE url IN ({placeholders})",
            urls,
        ).fetchall()
    return {
        row["url"]: {"status": row["status"], "checked_at": row["checked_at"]}
        for row in rows
    }


def store_link_check(path: Path, url: str, status: str, checked_at: str) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        connection.execute(
            """INSERT INTO link_check_cache(url, status, checked_at) VALUES (?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET status = excluded.status,
                                              checked_at = excluded.checked_at""",
            (url, status, checked_at),
        )


def backfill_assets(db_path: Path, assets_path: Path) -> int:
    """Register older generated media files and migrate their text sidecars.

    Only ULID-named upload files with a supported media extension are considered.
    Existing asset rows and hash sidecars are preserved, making startup retries safe.
    """
    initialize(db_path)
    if not assets_path.is_dir():
        return 0
    registered = 0
    with connection_scope(db_path) as connection:
        for asset_path in sorted(assets_path.iterdir()):
            mime_type = _MIME_BY_EXTENSION.get(asset_path.suffix.lower())
            if (
                not mime_type
                or not _UPLOAD_ID_RE.fullmatch(asset_path.stem)
                or asset_path.is_symlink()
                or not asset_path.is_file()
            ):
                continue
            try:
                data = asset_path.read_bytes()
            except OSError:
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            old_sidecar = asset_path.with_suffix(".txt")
            try:
                text = (
                    old_sidecar.read_text(encoding="utf-8")
                    if old_sidecar.is_file() and not old_sidecar.is_symlink()
                    else ""
                )
            except (OSError, UnicodeError):
                text = ""
            registered_at = _now()
            connection.execute(
                """INSERT INTO assets(
                       content_hash, media_name, mime_type, file_path, text,
                       text_updated_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(content_hash, media_name) DO UPDATE SET
                     mime_type = excluded.mime_type, file_path = excluded.file_path,
                     text = CASE WHEN assets.text = '' THEN excluded.text ELSE assets.text END,
                     text_updated_at = CASE WHEN assets.text = '' AND excluded.text <> ''
                                            THEN excluded.text_updated_at
                                            ELSE assets.text_updated_at END""",
                (
                    content_hash,
                    asset_path.name,
                    mime_type,
                    str(asset_path),
                    text,
                    registered_at if text else None,
                    registered_at,
                ),
            )
            if text:
                hash_sidecar = assets_path / f"{content_hash}.txt"
                if not hash_sidecar.exists() and not hash_sidecar.is_symlink():
                    try:
                        hash_sidecar.write_text(text, encoding="utf-8")
                    except OSError:
                        pass
            registered += 1
        document_ids = connection.execute("SELECT id FROM documents").fetchall()
        for row in document_ids:
            _rebuild_document_assets(connection, row["id"])
    rebuild_fts(db_path)
    return registered


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    if version == 3:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "folder_id" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN folder_id TEXT REFERENCES folders(id) ON DELETE RESTRICT"
            )
        if "position" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
        if "deleted_at" not in columns:
            connection.execute("ALTER TABLE documents ADD COLUMN deleted_at TEXT")
        connection.execute(
            """WITH ordered AS (
                   SELECT id,
                          ROW_NUMBER() OVER (
                              PARTITION BY folder_id ORDER BY updated_at ASC, id ASC
                          ) - 1 AS sibling_position
                     FROM documents
                    WHERE deleted_at IS NULL
               )
               UPDATE documents
                  SET position = (
                      SELECT sibling_position FROM ordered WHERE ordered.id = documents.id
                  )
                WHERE deleted_at IS NULL"""
        )
    if version == 4:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "revision" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
    if version == 7:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(assets)").fetchall()
        }
        if "text_updated_at" not in columns:
            connection.execute("ALTER TABLE assets ADD COLUMN text_updated_at TEXT")
        connection.execute(
            "UPDATE assets SET text_updated_at = created_at "
            "WHERE text <> '' AND text_updated_at IS NULL"
        )
    if version == 12:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "title_auto" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN title_auto INTEGER NOT NULL DEFAULT 0"
            )
    if version == 13:
        _migrate_provenance(connection)
    connection.executescript(_MIGRATIONS[version])


def _add_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_provenance(connection: sqlite3.Connection) -> None:
    """Version 13: provenance on documents, blocks and folders, plus block revisions.

    Existing notes are backfilled with the best guess available (marked
    ``backfilled``): notes in the top-level Uncategorized folder came from quick
    capture, notes whose file sits in an import backup came from Markdown import,
    and everything else was written by hand. Blocks keep NULL provenance, which
    means "same as the document".
    """

    _add_columns(
        connection,
        "documents",
        {
            "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            "source_kind": "TEXT NOT NULL DEFAULT 'manual'",
            "source_external_id": "TEXT",
            "sync_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_opened_at": "TEXT",
        },
    )
    _add_columns(connection, "blocks", {"provenance_json": "TEXT", "revision": "INTEGER"})
    _add_columns(
        connection,
        "folders",
        {"source_kind": "TEXT", "source_external_id": "TEXT", "source_name": "TEXT"},
    )
    imported_ids: set[str] = set()
    database_file = next(
        (row["file"] for row in connection.execute("PRAGMA database_list") if row["name"] == "main"),
        "",
    )
    if database_file:
        backups_root = Path(database_file).parent / ".fortress-import-backups"
        if backups_root.is_dir():
            imported_ids = {path.stem for path in backups_root.glob("*/*.md")}
    capture_folders = {
        row["id"]
        for row in connection.execute(
            "SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Uncategorized'"
        )
    }
    rows = connection.execute(
        "SELECT id, folder_id, created_at FROM documents WHERE provenance_json = '{}'"
    ).fetchall()
    for row in rows:
        if row["folder_id"] in capture_folders:
            kind = "capture"
        elif row["id"] in imported_ids:
            kind = "markdown_import"
        else:
            kind = "manual"
        connection.execute(
            "UPDATE documents SET provenance_json = ?, source_kind = ? WHERE id = ?",
            (_provenance.dumps({"source": kind, "backfilled": True}), kind, row["id"]),
        )


def is_ready(path: Path) -> bool:
    return path.resolve() in _ready_paths


def bootstrap_markdown(notes_path: Path, path: Path) -> int:
    """Import Markdown files that are not already represented in SQLite.

    This is intentionally insert-only. Editing or deleting existing Markdown does
    not mutate the new store until the block API owns writes in a later phase.
    """

    initialize(path)
    imported = 0
    with connection_scope(path) as connection:
        for note_path in sorted(notes_path.glob("*.md")):
            note_id = note_path.stem
            if connection.execute(
                "SELECT 1 FROM documents WHERE id = ?", (note_id,)
            ).fetchone():
                continue
            post = frontmatter.load(note_path)
            now = _now()
            created_at = _iso(post.metadata.get("created_at", now))
            updated_at = _iso(post.metadata.get("updated_at", now))
            title = str(post.metadata.get("title") or derive_title(post.content))
            status = post.metadata.get("status", "rough")
            if status not in {"rough", "polished"}:
                status = "rough"
            tags = post.metadata.get("tags", []) or []
            _insert_document(
                connection,
                note_id,
                title,
                status,
                tags,
                post.content,
                created_at,
                updated_at,
                provenance=_markdown_import_provenance(
                    post.metadata, note_path.name, now
                ),
            )
            imported += 1
    return imported


MARKDOWN_IMPORTER = {"name": "fortress-markdown", "version": "1"}


def _markdown_import_provenance(
    metadata: dict, source_path: str, now: str
) -> dict[str, object]:
    """Provenance for a note read from a Markdown file.

    Our own export writes the note's provenance as front matter; it is restored
    as it was (so export -> import -> export is stable). Any other file is a
    ``markdown_import``.
    """

    exported = metadata.get("provenance")
    if isinstance(exported, dict):
        try:
            restored = _provenance.normalize(exported, trusted=True)
        except _provenance.ProvenanceError:
            restored = {}
        if restored:
            return restored
    value: dict[str, object] = {
        "source": "markdown_import",
        "source_path": source_path,
        "imported_at": now,
        "importer": dict(MARKDOWN_IMPORTER),
    }
    for key, name in (("created_at", "source_created_at"), ("updated_at", "source_modified_at")):
        if metadata.get(key) is not None:
            value[name] = _iso(metadata[key])
    return value


def create_document(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: object,
    updated_at: object,
    ensure_empty_paragraph: bool = False,
    folder_id: str | None = None,
    provenance: dict[str, object] | None = None,
) -> None:
    """Create the block representation for a newly created compatibility note."""

    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        _validate_folder_parent(connection, folder_id)
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone():
            return
        _insert_document(
            connection,
            document_id,
            title,
            status,
            tags,
            body,
            _iso(created_at),
            _iso(updated_at),
            ensure_empty_paragraph,
            provenance,
        )
        if folder_id is not None:
            position = _document_insert_position(connection, folder_id, None)
            connection.execute(
                "UPDATE documents SET folder_id = ?, position = ? WHERE id = ?",
                (folder_id, position, document_id),
            )


def mark_opened(path: Path, document_id: str) -> None:
    """Remember when a note was last opened (metadata only: no revision, no edit time)."""

    initialize(path)
    with connection_scope(path) as connection:
        connection.execute(
            "UPDATE documents SET last_opened_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_now(), document_id),
        )


def ensure_editable_block(path: Path, document_id: str) -> bool:
    """Give a live document with no blocks one empty paragraph; True if one was added.

    The editor always shows a paragraph to type into, so it needs a stable ID the
    server knows (an empty import, or a note emptied by move-into and restored,
    has none). The content is unchanged - an empty paragraph exports as nothing -
    so the revision and history stay as they are.
    """

    initialize(path)
    with connection_scope(path) as connection:
        if not _needs_editable_block(connection, document_id):
            return False
    with connection_scope(path, immediate=True) as connection:
        if not _needs_editable_block(connection, document_id):
            return False
        now = _now()
        _insert_blocks(connection, document_id, "", now, now, ensure_empty_paragraph=True)
    return True


def _needs_editable_block(connection: sqlite3.Connection, document_id: str) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM documents
                WHERE id = ? AND deleted_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM blocks WHERE blocks.document_id = documents.id)""",
            (document_id,),
        ).fetchone()
        is not None
    )


def list_documents(path: Path, ids: list[str] | None = None) -> list[dict[str, object]]:
    """Active documents, newest first; only ``ids`` when given (one query either way)."""

    initialize(path)
    only = "" if ids is None else f"AND id IN ({','.join('?' for _ in ids)})"
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"""SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((SELECT text FROM blocks WHERE blocks.document_id = documents.id
                                ORDER BY parent_id, position, id LIMIT 1), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL {only}
                ORDER BY updated_at DESC, id ASC""",
            [] if ids is None else list(ids),
        ).fetchall()
    return [_navigation_document(row) for row in rows]


def create_block_document(
    path: Path, title: str, status: str, tags: list[str], body: str,
    folder_id: str | None = None,
    created_at: object | None = None,
    title_auto: bool = False,
    provenance: dict[str, object] | None = None,
) -> dict:
    """Create a document; with ``title_auto`` its title follows its first line."""

    document_id = _new_ulid()
    now = _now()
    create_document(
        path, document_id, title, status, tags, body,
        _iso(created_at) if created_at is not None else now, now,
        ensure_empty_paragraph=True,
        folder_id=folder_id,
        provenance=provenance,
    )
    if title_auto:
        with connection_scope(path) as connection:
            connection.execute(
                "UPDATE documents SET title_auto = 1 WHERE id = ?", (document_id,)
            )
            _refresh_auto_title(connection, document_id)
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree


def rename_document(path: Path, document_id: str, title: str) -> dict:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            """UPDATE documents SET title = ?, title_auto = 0, updated_at = ?
                 WHERE id = ? AND deleted_at IS NULL""",
            (title, _now(), document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree


def update_document_metadata(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    updated_at: object,
    title_chosen: bool = False,
) -> None:
    """Update metadata; ``title_chosen`` means a person named it (auto titles stop)."""

    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            """UPDATE documents
               SET title = ?, status = ?, tags_json = ?, updated_at = ?,
                   title_auto = CASE WHEN ? THEN 0 ELSE title_auto END
               WHERE id = ? AND deleted_at IS NULL""",
            (title, status, json.dumps(tags), _iso(updated_at), title_chosen, document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")


def replace_document_from_markdown(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: object,
    updated_at: object,
) -> None:
    """Re-import the explicit raw/compatibility edit into the block store."""

    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        existing = connection.execute(
            "SELECT created_at, deleted_at FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if existing is not None and existing["deleted_at"] is not None:
            raise KeyError("document not found")
        if existing is None:
            _insert_document(
                connection,
                document_id,
                title,
                status,
                tags,
                body,
                _iso(created_at),
                _iso(updated_at),
            )
            return
        connection.execute(
            """UPDATE documents
               SET title = ?, status = ?, tags_json = ?, updated_at = ?,
                   revision = revision + 1
               WHERE id = ?""",
            (title, status, json.dumps(tags), _iso(updated_at), document_id),
        )
        # A whole-document Markdown replacement has no block-level inverse patch.
        # Treat it as a new revision and discard old undo/redo entries so they
        # cannot restore a tree from before this compatibility edit.
        connection.execute(
            "DELETE FROM document_history WHERE document_id = ?", (document_id,)
        )
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM blocks WHERE document_id = ?", (document_id,))
        _insert_blocks(connection, document_id, body, existing["created_at"], _iso(updated_at))


def delete_document(path: Path, document_id: str) -> None:
    """Move a document to the trash: hidden everywhere, recoverable until purged."""

    initialize(path)
    now = _now()
    with connection_scope(path, immediate=True) as connection:
        row = connection.execute(
            "SELECT folder_id, position FROM documents WHERE id = ? AND deleted_at IS NULL",
            (document_id,),
        ).fetchone()
        if row is None:
            raise KeyError("document not found")
        connection.execute(
            "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, document_id),
        )
        connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
        _shift_document_positions(connection, row["folder_id"], row["position"] + 1, -1)


def create_folder(
    path: Path, name: str, parent_id: str | None = None, position: int | None = None
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path, immediate=True) as connection:
        _validate_folder_parent(connection, parent_id)
        target_position = _folder_insert_position(connection, parent_id, position)
        folder_id = _new_ulid()
        connection.execute(
            """INSERT INTO folders(id, parent_id, name, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (folder_id, parent_id, name, target_position, now, now),
        )
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def rename_folder(path: Path, folder_id: str, name: str) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        cursor = connection.execute(
            "UPDATE folders SET name = ?, updated_at = ? WHERE id = ?",
            (name, now, folder_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("folder not found")
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def move_folder(
    path: Path,
    folder_id: str,
    parent_id: str | None | object = UNSET,
    position: int | None = None,
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path, immediate=True) as connection:
        folder = _folder_row(connection, folder_id)
        target_parent = folder["parent_id"] if parent_id is UNSET else parent_id
        _validate_folder_parent(connection, target_parent)
        if target_parent == folder_id or _folder_is_descendant(connection, folder_id, target_parent):
            raise ValueError("cannot move a folder into itself or its descendant")
        _shift_folder_positions(connection, folder["parent_id"], folder["position"] + 1, -1)
        target_position = _folder_insert_position(
            connection, target_parent, position, exclude_id=folder_id
        )
        connection.execute(
            """UPDATE folders
                  SET parent_id = ?, position = ?, updated_at = ?
                WHERE id = ?""",
            (target_parent, target_position, now, folder_id),
        )
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def delete_folder(path: Path, folder_id: str) -> None:
    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        _folder_row(connection, folder_id)
        if connection.execute(
            "SELECT 1 FROM folders WHERE parent_id = ? LIMIT 1", (folder_id,)
        ).fetchone() or connection.execute(
            "SELECT 1 FROM documents WHERE folder_id = ? AND deleted_at IS NULL LIMIT 1",
            (folder_id,),
        ).fetchone():
            raise ValueError("folder is not empty")
        folder = _folder_row(connection, folder_id)
        connection.execute(
            "UPDATE documents SET folder_id = NULL WHERE folder_id = ? AND deleted_at IS NOT NULL",
            (folder_id,),
        )
        connection.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        _shift_folder_positions(connection, folder["parent_id"], folder["position"] + 1, -1)


def move_document(
    path: Path,
    document_id: str,
    folder_id: str | None | object = UNSET,
    position: int | None = None,
) -> dict[str, object]:
    initialize(path)
    now = _now()
    with connection_scope(path, immediate=True) as connection:
        document = connection.execute(
            """SELECT id, folder_id, position FROM documents
                 WHERE id = ? AND deleted_at IS NULL""",
            (document_id,),
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        target_folder = document["folder_id"] if folder_id is UNSET else folder_id
        _validate_folder_parent(connection, target_folder)
        _shift_document_positions(
            connection, document["folder_id"], document["position"] + 1, -1
        )
        target_position = _document_insert_position(
            connection, target_folder, position, exclude_id=document_id
        )
        connection.execute(
            """UPDATE documents
                  SET folder_id = ?, position = ?, updated_at = ?
                WHERE id = ?""",
            (target_folder, target_position, now, document_id),
        )
        row = connection.execute(
            "SELECT id, folder_id, position FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return {"id": row["id"], "folder_id": row["folder_id"], "position": row["position"]}


def navigation(path: Path, recent_limit: int = 10) -> dict[str, object]:
    """Return the canonical folder tree and recently edited documents."""

    initialize(path)
    with connection_scope(path) as connection:
        folders = connection.execute(
            """SELECT id, parent_id, name, position, created_at, updated_at
                 FROM folders
                ORDER BY parent_id, position, id"""
        ).fetchall()
        documents = connection.execute(
            """SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id
                           ORDER BY parent_id, position, id LIMIT 1
                      ), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY folder_id, position, id"""
        ).fetchall()
        recent = connection.execute(
            """SELECT id, title, status, tags_json, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id
                           ORDER BY parent_id, position, id LIMIT 1
                      ), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC, id ASC
                LIMIT ?""",
            (max(1, min(recent_limit, 50)),),
        ).fetchall()

    documents_by_folder: dict[str | None, list[dict[str, object]]] = {}
    for row in documents:
        item = _navigation_document(row)
        documents_by_folder.setdefault(row["folder_id"], []).append(item)

    folders_by_parent: dict[str | None, list[sqlite3.Row]] = {}
    for row in folders:
        folders_by_parent.setdefault(row["parent_id"], []).append(row)

    def children(parent_id: str | None) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for folder in folders_by_parent.get(parent_id, []):
            items.append(
                {
                    "kind": "folder",
                    "id": folder["id"],
                    "parent_id": folder["parent_id"],
                    "name": folder["name"],
                    "position": folder["position"],
                    "created_at": folder["created_at"],
                    "updated_at": folder["updated_at"],
                    "children": children(folder["id"]),
                }
            )
        items.extend(documents_by_folder.get(parent_id, []))
        return items

    return {
        "items": children(None),
        "recent": [_navigation_document(row) for row in recent],
    }


def folder_exists(path: Path, folder_id: str) -> bool:
    initialize(path)
    with connection_scope(path) as connection:
        return (
            connection.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone()
            is not None
        )


def _folder_row(connection: sqlite3.Connection, folder_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise KeyError("folder not found")
    return row


def _folder_data(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_folder_parent(connection: sqlite3.Connection, parent_id: str | None) -> None:
    if parent_id is not None:
        _folder_row(connection, parent_id)


def _folder_is_descendant(
    connection: sqlite3.Connection, folder_id: str, candidate_parent_id: str | None
) -> bool:
    parent_id = candidate_parent_id
    while parent_id is not None:
        if parent_id == folder_id:
            return True
        parent_id = _folder_row(connection, parent_id)["parent_id"]
    return False


def _folder_insert_position(
    connection: sqlite3.Connection,
    parent_id: str | None,
    position: int | None,
    exclude_id: str | None = None,
) -> int:
    count = connection.execute(
        "SELECT COUNT(*) FROM folders WHERE parent_id IS ? AND id IS NOT ?",
        (parent_id, exclude_id),
    ).fetchone()[0]
    target_position = count if position is None else min(position, count)
    _shift_folder_positions(connection, parent_id, target_position, 1)
    return target_position


def _shift_folder_positions(
    connection: sqlite3.Connection, parent_id: str | None, from_position: int, delta: int
) -> None:
    connection.execute(
        """UPDATE folders SET position = position + ?
           WHERE parent_id IS ? AND position >= ?""",
        (delta, parent_id, from_position),
    )


def _document_insert_position(
    connection: sqlite3.Connection,
    folder_id: str | None,
    position: int | None,
    exclude_id: str | None = None,
) -> int:
    count = connection.execute(
        """SELECT COUNT(*) FROM documents
           WHERE folder_id IS ? AND deleted_at IS NULL AND id IS NOT ?""",
        (folder_id, exclude_id),
    ).fetchone()[0]
    target_position = count if position is None else min(position, count)
    _shift_document_positions(connection, folder_id, target_position, 1)
    return target_position


def _shift_document_positions(
    connection: sqlite3.Connection, folder_id: str | None, from_position: int, delta: int
) -> None:
    connection.execute(
        """UPDATE documents SET position = position + ?
           WHERE folder_id IS ? AND deleted_at IS NULL AND position >= ?""",
        (delta, folder_id, from_position),
    )


def backup_database(path: Path, destination: Path) -> None:
    """Create a consistent live snapshot using Python's SQLite backup API."""

    initialize(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connection_scope(path) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        target.commit()


def check_integrity(path: Path) -> bool:
    initialize(path)
    with connection_scope(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    return result is not None and result[0] == "ok"


def fts_is_consistent(path: Path) -> bool:
    """Check that FTS contains exactly the active canonical blocks."""

    initialize(path)
    with connection_scope(path) as connection:
        missing = connection.execute(
            """SELECT 1
                 FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                 LEFT JOIN blocks_fts ON blocks_fts.block_id = blocks.id
                WHERE documents.deleted_at IS NULL AND blocks_fts.block_id IS NULL
                LIMIT 1"""
        ).fetchone()
        stale = connection.execute(
            """SELECT 1
                 FROM blocks_fts
                 LEFT JOIN blocks ON blocks.id = blocks_fts.block_id
                 LEFT JOIN documents ON documents.id = blocks_fts.document_id
                WHERE blocks.id IS NULL OR documents.deleted_at IS NOT NULL
                LIMIT 1"""
        ).fetchone()
    return missing is None and stale is None


def rebuild_fts(path: Path) -> None:
    """Rebuild the disposable FTS rows from active canonical blocks."""

    initialize(path)
    with connection_scope(path) as connection:
        connection.execute("DELETE FROM blocks_fts")
        connection.execute(
            """INSERT INTO blocks_fts(block_id, document_id, text)
                 SELECT blocks.id, blocks.document_id,
                        blocks.text || CASE WHEN COALESCE((
                            SELECT group_concat(assets.text, char(10))
                              FROM block_assets
                              JOIN assets USING (content_hash, media_name)
                             WHERE block_assets.block_id = blocks.id
                        ), '') = '' THEN '' ELSE char(10) || (
                            SELECT group_concat(assets.text, char(10))
                              FROM block_assets
                              JOIN assets USING (content_hash, media_name)
                             WHERE block_assets.block_id = blocks.id
                        ) END
                   FROM blocks
                   JOIN documents ON documents.id = blocks.document_id
                  WHERE documents.deleted_at IS NULL"""
        )


_corpus_cache: dict[Path, tuple[tuple, list[tuple[str, str, str, str]]]] = {}


def _corpus_fingerprint(connection: sqlite3.Connection) -> tuple:
    """Changes whenever any block, document or asset text changes (a few ms).

    Every block edit bumps its document's revision, so the revision total catches
    edits that the timestamp maxima miss (e.g. another note dated in the future).
    """

    return tuple(
        connection.execute(
            """SELECT (SELECT COUNT(*) FROM blocks),
                      (SELECT MAX(updated_at) FROM blocks),
                      (SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL),
                      (SELECT MAX(updated_at) FROM documents),
                      -- Every block edit bumps a revision; timestamps alone miss
                      -- edits when an imported note carries a later timestamp.
                      (SELECT TOTAL(revision) FROM documents),
                      (SELECT MAX(text_updated_at) FROM assets),
                      (SELECT COUNT(*) FROM block_assets)"""
        ).fetchone()
    )


def embedding_documents(path: Path) -> list[tuple[str, str, str, str]]:
    """Active blocks as (block_id, document_id, version, text), cached until notes change."""

    initialize(path)
    key = path.resolve()
    with connection_scope(path) as connection:
        fingerprint = _corpus_fingerprint(connection)
    cached = _corpus_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    corpus = _load_embedding_documents(path)
    _corpus_cache[key] = (fingerprint, corpus)
    return corpus


def _load_embedding_documents(path: Path) -> list[tuple[str, str, str, str]]:
    """Return active canonical blocks as embedding inputs.

    The final document ID keeps the compatibility search layer able to fold
    block scores back into note-shaped responses.
    """

    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT blocks.id, blocks.document_id,
                      blocks.updated_at || '|' || COALESCE((
                          SELECT group_concat(asset_version.version, '|')
                            FROM (
                                SELECT block_assets.media_name || ':' ||
                                       COALESCE(assets.text_updated_at, '') AS version
                                  FROM block_assets
                                  JOIN assets USING (content_hash, media_name)
                                 WHERE block_assets.block_id = blocks.id
                                 ORDER BY block_assets.media_name
                            ) AS asset_version
                      ), '') AS updated_at,
                      blocks.text,
                      (SELECT group_concat(assets.text, char(10))
                         FROM block_assets
                         JOIN assets USING (content_hash, media_name)
                        WHERE block_assets.block_id = blocks.id AND trim(assets.text) <> '')
                          AS asset_text
                 FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                WHERE documents.deleted_at IS NULL
                  AND (trim(blocks.text) <> '' OR EXISTS (
                      SELECT 1 FROM block_assets
                      JOIN assets USING (content_hash, media_name)
                      WHERE block_assets.block_id = blocks.id AND trim(assets.text) <> ''
                  ))
                ORDER BY blocks.document_id, blocks.position, blocks.id"""
        ).fetchall()
    return [
        (
            str(row["id"]),
            str(row["document_id"]),
            str(row["updated_at"]),
            "\n\n".join(
                part for part in (str(row["text"]), row["asset_text"] or "") if part
            ),
        )
        for row in rows
    ]


def ensure_fts_integrity(path: Path) -> bool:
    """Repair FTS if needed and report whether the index is consistent afterward."""

    if not fts_is_consistent(path):
        rebuild_fts(path)
    return fts_is_consistent(path)


def search_blocks(path: Path, query: str, limit: int = 50) -> list[dict[str, object]]:
    """Search canonical block text with SQLite FTS5 and return block context."""

    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not terms:
        return []
    match = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT blocks_fts.block_id, blocks_fts.document_id, documents.title,
                      blocks.type, blocks_fts.text, bm25(blocks_fts) AS rank
                FROM blocks_fts
                 JOIN blocks ON blocks.id = blocks_fts.block_id
                 JOIN documents ON documents.id = blocks_fts.document_id
                WHERE blocks_fts MATCH ? AND documents.deleted_at IS NULL
                ORDER BY rank, blocks.updated_at DESC
                LIMIT ?""",
            (match, max(1, min(limit, 200))),
        ).fetchall()
    return [
        {
            "block_id": row["block_id"],
            "document_id": row["document_id"],
            "document_title": row["title"],
            "block_type": row["type"],
            "text": row["text"],
            "score": -float(row["rank"]),
        }
        for row in rows
    ]


def block_link_targets(path: Path, query: str = "", limit: int = 50) -> list[dict[str, object]]:
    """Return stable block IDs suitable for reference autocomplete."""

    initialize(path)
    normalized_query = query.strip()
    if not normalized_query:
        return []
    escaped_query = (
        normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    needle = f"%{escaped_query}%"
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT blocks.id, blocks.document_id, documents.title,
                      blocks.type, blocks.text
                 FROM blocks
                 JOIN documents ON documents.id = blocks.document_id
                WHERE documents.deleted_at IS NULL
                  AND (
                    blocks.id = ?
                   OR blocks.text LIKE ? COLLATE NOCASE ESCAPE '\\'
                   OR (
                        (documents.title LIKE ? COLLATE NOCASE ESCAPE '\\'
                         OR documents.id LIKE ? COLLATE NOCASE ESCAPE '\\')
                        AND blocks.id = (
                            SELECT first_block.id
                              FROM blocks AS first_block
                             WHERE first_block.document_id = documents.id
                               AND first_block.parent_id IS NULL
                             ORDER BY first_block.position ASC, first_block.id ASC
                             LIMIT 1
                        )
                   )
                  )
                -- An exact block ID (a reference preview looks one up) comes first.
                ORDER BY blocks.id = ? DESC, documents.updated_at DESC, blocks.updated_at DESC,
                         blocks.position ASC, documents.id ASC, blocks.id ASC
                LIMIT ?""",
            (
                normalized_query,
                needle,
                needle,
                needle,
                normalized_query,
                max(1, min(limit, 200)),
            ),
        ).fetchall()
    return [
        {
            "block_id": row["id"],
            "document_id": row["document_id"],
            "document_title": row["title"],
            "block_type": row["type"],
            "text": row["text"],
        }
        for row in rows
    ]


def document_backlinks(path: Path, document_id: str, limit: int = 100) -> list[dict[str, object]]:
    """Return block and document references pointing into one document."""

    initialize(path)
    with connection_scope(path) as connection:
        rows = connection.execute(
            """SELECT source_block_id, source_document_id, source_document_title,
                      source_text, label, target_block_id
                 FROM (
                    SELECT refs.source_block_id, source.document_id AS source_document_id,
                           source_doc.title AS source_document_title, source.text AS source_text,
                           refs.label, refs.target_block_id, source.updated_at AS updated_at
                      FROM block_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                      JOIN documents AS source_doc ON source_doc.id = source.document_id
                      JOIN blocks AS target ON target.id = refs.target_block_id
                      JOIN documents AS target_doc ON target_doc.id = target.document_id
                     WHERE target.document_id = ?
                       AND source_doc.deleted_at IS NULL
                       AND target_doc.deleted_at IS NULL
                     UNION ALL
                    SELECT refs.source_block_id, source.document_id AS source_document_id,
                           source_doc.title AS source_document_title, source.text AS source_text,
                           refs.label, NULL AS target_block_id, source.updated_at AS updated_at
                      FROM document_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                      JOIN documents AS source_doc ON source_doc.id = source.document_id
                      JOIN documents AS target_doc ON target_doc.id = refs.target_document_id
                     WHERE refs.target_document_id = ?
                       AND source_doc.deleted_at IS NULL
                       AND target_doc.deleted_at IS NULL
                 )
                ORDER BY updated_at DESC
                LIMIT ?""",
            (document_id, document_id, max(1, min(limit, 500))),
        ).fetchall()
    return [
        {
            "source_block_id": row["source_block_id"],
            "source_document_id": row["source_document_id"],
            "source_document_title": row["source_document_title"],
            "source_text": row["source_text"],
            "label": row["label"],
            "target_block_id": row["target_block_id"],
        }
        for row in rows
    ]


def _insert_document(
    connection: sqlite3.Connection,
    document_id: str,
    title: str,
    status: str,
    tags: list[str],
    body: str,
    created_at: str,
    updated_at: str,
    ensure_empty_paragraph: bool = False,
    provenance: dict[str, object] | None = None,
) -> None:
    position = connection.execute(
        """SELECT COALESCE(MAX(position) + 1, 0)
             FROM documents
            WHERE folder_id IS NULL AND deleted_at IS NULL"""
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO documents(
                   id, title, status, tags_json, folder_id, position, deleted_at,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)""",
        (document_id, title, status, json.dumps(tags), position, created_at, updated_at),
    )
    _set_document_provenance(
        connection, document_id, _unclaimed_source_key(connection, provenance or {"source": "manual"})
    )
    _insert_blocks(
        connection, document_id, body, created_at, updated_at, ensure_empty_paragraph
    )


def _unclaimed_source_key(
    connection: sqlite3.Connection, value: dict[str, object]
) -> dict[str, object]:
    """``value``, minus its source id when another note already holds that key.

    Importing a copy (e.g. our own Markdown export of a OneNote page, into the
    same workspace) must not take over the original's identity for re-imports.
    """

    external_id = value.get("external_id")
    if not external_id or connection.execute(
        "SELECT 1 FROM documents WHERE source_kind = ? AND source_external_id = ?",
        (value.get("source", _provenance.DEFAULT_SOURCE), external_id),
    ).fetchone() is None:
        return value
    copy = {key: item for key, item in value.items() if key != "external_id"}
    extra = copy.get("extra") if isinstance(copy.get("extra"), dict) else {}
    copy["extra"] = {**extra, "copy_of_external_id": external_id}
    return copy


def _set_document_provenance(
    connection: sqlite3.Connection, document_id: str, value: dict[str, object]
) -> None:
    """Store a document's provenance and the source key re-imports look it up by."""

    value = {**value}
    value.setdefault("source", _provenance.DEFAULT_SOURCE)
    connection.execute(
        """UPDATE documents SET provenance_json = ?, source_kind = ?, source_external_id = ?
            WHERE id = ?""",
        (
            _provenance.dumps(value),
            value["source"],
            value.get("external_id") or None,
            document_id,
        ),
    )


def _navigation_document(row: sqlite3.Row) -> dict[str, object]:
    return {
        "kind": "document",
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "tags": json.loads(row["tags_json"]),
        "folder_id": row["folder_id"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "snippet": row["snippet"][:160],
        # Where the note came from ("manual", "capture", "onenote_import", ...).
        "source": row["source_kind"] if "source_kind" in row.keys() else "manual",
    }


def _insert_blocks(
    connection: sqlite3.Connection,
    document_id: str,
    body: str,
    created_at: str,
    updated_at: str,
    ensure_empty_paragraph: bool = False,
) -> None:
    def insert(block: ImportedBlock, parent_id: str | None, position: int) -> None:
        connection.execute(
            """INSERT INTO blocks(
                   id, document_id, parent_id, position, type, attrs_json,
                   content_json, text, created_at, updated_at, provenance_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                block.id,
                document_id,
                parent_id,
                position,
                block.type,
                json.dumps(block.attrs),
                json.dumps(block.stored_content()),
                block.stored_text(),
                created_at,
                updated_at,
                _provenance.dumps(block.provenance),
            ),
        )
        _replace_attrs(connection, block.id, block.attrs)
        connection.execute(
            "INSERT INTO blocks_fts(block_id, document_id, text) VALUES (?, ?, ?)",
            (block.id, document_id, block.stored_text()),
        )
        for child_position, child in enumerate(block.children):
            insert(child, block.id, child_position)

    blocks = _parse_blocks(body)
    if not blocks and ensure_empty_paragraph:
        # BlockNote edits a paragraph even when the document is initially blank.
        # Persist that block so the first keystroke updates a real stable ID.
        blocks = [ImportedBlock(_new_ulid(), "paragraph", "")]
    for position, block in enumerate(blocks):
        insert(block, None, position)
    _rebuild_document_references(connection, document_id)
    _rebuild_document_assets(connection, document_id)


def _rebuild_document_assets(connection: sqlite3.Connection, document_id: str) -> None:
    """Refresh links from each persisted block to uploaded assets it contains."""
    connection.execute(
        """DELETE FROM block_assets WHERE block_id IN
           (SELECT id FROM blocks WHERE document_id = ?)""",
        (document_id,),
    )
    rows = connection.execute(
        "SELECT id, attrs_json, content_json, text FROM blocks WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    # blocks_fts.block_id is UNINDEXED, so one document-wide delete is far cheaper
    # than a table scan per block.
    connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
    for row in rows:
        serialized = f"{row['attrs_json']}\n{row['content_json']}"
        media_names = set(_MEDIA_REF_RE.findall(serialized))
        for media_name in media_names:
            connection.execute(
                """INSERT OR IGNORE INTO block_assets(block_id, content_hash, media_name)
                   SELECT ?, content_hash, media_name FROM assets WHERE media_name = ?""",
                (row["id"], media_name),
            )
        _refresh_fts(connection, row["id"], document_id, row["text"], replace=False)


def _rebuild_document_references(connection: sqlite3.Connection, document_id: str) -> None:
    source_ids = connection.execute(
        "SELECT id FROM blocks WHERE document_id = ?", (document_id,)
    ).fetchall()
    for row in source_ids:
        _replace_references(connection, row["id"], document_id)


def _rebuild_inbound_block_references(
    connection: sqlite3.Connection, target_block_ids: set[str]
) -> None:
    for target_id in target_block_ids:
        rows = connection.execute(
            """SELECT id, document_id FROM blocks
               WHERE instr(text, ?) > 0 OR instr(content_json, ?) > 0""",
            (target_id, target_id),
        ).fetchall()
        for row in rows:
            _replace_references(connection, row["id"], row["document_id"])


def _replace_references(
    connection: sqlite3.Connection, source_block_id: str, document_id: str
) -> None:
    row = connection.execute(
        "SELECT content_json, text FROM blocks WHERE id = ? AND document_id = ?",
        (source_block_id, document_id),
    ).fetchone()
    if row is None:
        return
    connection.execute("DELETE FROM block_refs WHERE source_block_id = ?", (source_block_id,))
    connection.execute("DELETE FROM document_refs WHERE source_block_id = ?", (source_block_id,))
    haystack = f"{row['text']}\n{row['content_json']}"
    block_targets = {
        match.group(1): match.group(2) or "" for match in _BLOCK_REF_RE.finditer(haystack)
    }
    document_targets = {
        match.group(1): match.group(2) or "" for match in _DOCUMENT_REF_RE.finditer(haystack)
    }
    for target_id, label in block_targets.items():
        if target_id == source_block_id:
            continue
        if connection.execute(
            """SELECT 1 FROM blocks
                JOIN documents ON documents.id = blocks.document_id
               WHERE blocks.id = ? AND documents.deleted_at IS NULL""",
            (target_id,),
        ).fetchone():
            connection.execute(
                "INSERT OR IGNORE INTO block_refs(source_block_id, target_block_id, label) VALUES (?, ?, ?)",
                (source_block_id, target_id, label),
            )
    for target_id, label in document_targets.items():
        if connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NULL", (target_id,)
        ).fetchone():
            connection.execute(
                "INSERT OR IGNORE INTO document_refs(source_block_id, target_document_id, label) VALUES (?, ?, ?)",
                (source_block_id, target_id, label),
            )


def render_markdown(
    nodes: list[dict], depth: int = 0, document_provenance: dict | None = None
) -> str:
    """Render a block tree as portable Markdown (block IDs are not included).

    With ``document_provenance``, a block whose provenance differs from the
    document's gets it on a ``<!-- fortress:block {...} -->`` line right before it,
    which Markdown import reads back. A ``columns`` layout is written as its
    columns one after another between ``<!-- fortress:columns -->`` markers.
    """

    fragments: list[str] = []
    number = 0
    for node in nodes:
        # Each item is written as "1. "; number consecutive ordered items, since a
        # provenance comment between items starts a new list in Markdown viewers.
        number = number + 1 if _is_ordered_item(node) else 0
        fragments.extend(_render_node(node, depth, document_provenance, number))
    return "\n\n".join(fragments)


def _is_ordered_item(node: dict) -> bool:
    attrs = node.get("attrs") or {}
    return node.get("type") == "numberedListItem" or (
        node.get("type") == "list" and bool(attrs.get("ordered")) and not isinstance(attrs.get("checked"), bool)
    )


def _number_item(source: str, number: int) -> str:
    if number > 1 and source.startswith("1. "):
        return f"{number}. " + source[3:]
    return source


def _render_node(
    node: dict, depth: int, document_provenance: dict | None, number: int = 0
) -> list[str]:
    def placed(source: str, block: dict) -> str:
        if source and document_provenance is not None:
            own = block.get("provenance")
            if isinstance(own, dict) and own and not own.get("inherited"):
                if own != {"source": document_provenance.get("source", "manual")}:
                    source = f"{_provenance.comment(own)}\n{source}"
        if depth and source:
            prefix = "  " * depth
            source = "\n".join(
                f"{prefix}{line}" if line else line for line in source.split("\n")
            )
        return source

    children = node.get("children", [])
    if node["type"] == "columns":
        fragments = [placed(_types.marker("columns", node.get("attrs") or {}), node)]
        for child in children:
            if child["type"] != "column":
                fragments.extend(_render_node(child, depth, document_provenance))
                continue
            fragments.append(placed(_types.marker("column", child.get("attrs") or {}), child))
            number = 0
            for grandchild in child.get("children", []):
                number = number + 1 if _is_ordered_item(grandchild) else 0
                fragments.extend(_render_node(grandchild, depth, document_provenance, number))
        fragments.append(placed("<!-- /fortress:columns -->", {}))
        return fragments
    source = _number_item(_node_markdown(node), number)
    if node["type"] == "divider" and not source:
        source = "---"
    fragments = []
    source = placed(source, node)
    if source:
        fragments.append(source)
    if children:
        rendered_children = render_markdown(children, depth + 1, document_provenance)
        if rendered_children:
            fragments.append(rendered_children)
    return fragments


def _node_markdown(node: dict) -> str:
    content = node.get("content", {})
    if isinstance(content, dict):
        markdown = content.get("markdown")
        native = content.get("blocknote")
        if (
            isinstance(markdown, str)
            and node.get("type") in _types.TEXT_TYPES
            and _types.has_extended_styles(native)
            and not _types.has_inline_mark_html(markdown)
        ):
            # Saved by the web editor before marks were exported: its Markdown
            # dropped colours/underline, so write the runs instead.
            return _types.text_block_markdown(node["type"], node.get("attrs") or {}, native)
        if isinstance(markdown, str):
            return markdown
        if isinstance(native, str):
            return native
        try:
            if node.get("type") in _types.STRUCTURED_TYPES or isinstance(native, list):
                return _types.block_markdown(node["type"], node.get("attrs") or {}, content)
        except (_types.BlockShapeError, KeyError, TypeError):
            pass
        if isinstance(native, list):
            return _inline_text(native)
    return str(node.get("text", ""))


def _inline_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(_inline_text(item.get("content")))
    return "".join(parts)


def document_tree(path: Path, document_id: str) -> dict | None:
    initialize(path)
    with connection_scope(path) as connection:
        return _document_tree(connection, document_id)


def _document_tree(connection: sqlite3.Connection, document_id: str) -> dict | None:
    document = connection.execute(
        "SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
    ).fetchone()
    if document is None:
        return None
    history_state = connection.execute(
        """SELECT MAX(CASE WHEN undone = 0 THEN 1 ELSE 0 END) AS can_undo,
                  MAX(CASE WHEN undone = 1 THEN 1 ELSE 0 END) AS can_redo
             FROM document_history WHERE document_id = ?""",
        (document_id,),
    ).fetchone()
    rows = connection.execute(
        """SELECT id, document_id, parent_id, position, type, attrs_json,
                  content_json, text, created_at, updated_at, provenance_json, revision
             FROM blocks WHERE document_id = ?
             ORDER BY parent_id, position""",
        (document_id,),
    ).fetchall()
    document_provenance = _provenance.document_provenance(
        document["provenance_json"], document["source_kind"]
    )
    links = _resolved_links(connection, document_id)
    user_attr_rows = connection.execute(
        """SELECT attrs.block_id, attrs.name, attrs.value
             FROM block_user_attrs AS attrs
             JOIN blocks ON blocks.id = attrs.block_id
            WHERE blocks.document_id = ?""",
        (document_id,),
    ).fetchall()
    user_attrs_by_block: dict[str, dict[str, object]] = {}
    for row in user_attr_rows:
        user_attrs_by_block.setdefault(row["block_id"], {})[row["name"]] = json.loads(
            row["value"]
        )
    nodes = {
        row["id"]: {
            "id": row["id"],
            "document_id": row["document_id"],
            "parent_id": row["parent_id"],
            "position": row["position"],
            "type": row["type"],
            "attrs": json.loads(row["attrs_json"]),
            "user_attrs": user_attrs_by_block.get(row["id"], {}),
            "content": json.loads(row["content_json"]),
            "text": row["text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            # Where this block came from; ``inherited`` when it is the document's.
            "provenance": _provenance.effective_block_provenance(
                row["provenance_json"], document_provenance
            ),
            # The document revision that last changed this block (null: never since
            # it was created or imported).
            "revision": row["revision"],
            "children": [],
        }
        for row in rows
    }
    if links["documents"]:
        for node in nodes.values():
            _refresh_link_labels(node, links)
    roots: list[dict] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return {
        "id": document["id"],
        "title": document["title"],
        "status": document["status"],
        "tags": json.loads(document["tags_json"]),
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "revision": document["revision"],
        "title_auto": bool(document["title_auto"]),
        "can_undo": bool(history_state["can_undo"]),
        "can_redo": bool(history_state["can_redo"]),
        "provenance": document_provenance,
        "last_opened_at": document["last_opened_at"],
        # Current titles/text of every note and block this document links to.
        "resolved_links": links,
        "children": roots,
    }


def _resolved_links(connection: sqlite3.Connection, document_id: str) -> dict[str, dict]:
    """Live link targets of a document's blocks, keyed by ID (two queries)."""

    documents = {
        row["id"]: {"title": row["title"]}
        for row in connection.execute(
            """SELECT DISTINCT target.id, target.title
                 FROM document_refs AS refs
                 JOIN blocks AS source ON source.id = refs.source_block_id
                 JOIN documents AS target ON target.id = refs.target_document_id
                WHERE source.document_id = ? AND target.deleted_at IS NULL""",
            (document_id,),
        )
    }
    blocks = {
        row["id"]: {
            "document_id": row["document_id"],
            "document_title": row["title"],
            "type": row["type"],
            "text": row["text"],
        }
        for row in connection.execute(
            """SELECT DISTINCT target.id, target.document_id, target.type, target.text,
                             target_doc.title
                 FROM block_refs AS refs
                 JOIN blocks AS source ON source.id = refs.source_block_id
                 JOIN blocks AS target ON target.id = refs.target_block_id
                 JOIN documents AS target_doc ON target_doc.id = target.document_id
                WHERE source.document_id = ? AND target_doc.deleted_at IS NULL""",
            (document_id,),
        )
    }
    return {"documents": documents, "blocks": blocks}


def _link_label(title: str) -> str:
    """A title as the label of a ``[[id|label]]`` link (same rule as the editor)."""

    return re.sub(r"\s+", " ", re.sub(r"[|\]]", " ", title)).strip()


def _refresh_link_labels(node: dict, links: dict[str, dict]) -> None:
    """Show links with their targets' current titles (stored labels go stale on rename).

    Only the served copy changes: ``[[id|Old]]`` becomes ``[[id|Current]]``, and a
    link's visible text is updated when it was the old label (a custom link text is
    kept). Block references keep their stored label, which may be custom; their
    current text is in ``resolved_links["blocks"]``.
    """

    documents = links["documents"]

    def document_label(match: re.Match) -> str:
        target = documents.get(match.group(1))
        if target is None or match.group(2) is None:
            return match.group(0)
        label = _link_label(str(target["title"]))
        return f"[[{match.group(1)}|{label}]]" if label else match.group(0)

    def rewrite(value: str) -> str:
        if "[[" in value:
            value = _DOCUMENT_REF_RE.sub(document_label, value)
        return value

    def visit(value: object) -> object:
        if isinstance(value, str):
            return rewrite(value)
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "link" and isinstance(value.get("href"), str):
            old_href = value["href"]
            match = _DOCUMENT_REF_RE.fullmatch(old_href.strip())
            new_href = rewrite(old_href)
            if match and new_href != old_href:
                old_label = (match.group(2) or "").strip()
                new_match = _DOCUMENT_REF_RE.fullmatch(new_href.strip())
                new_label = new_match.group(2) if new_match else old_label
                content = value.get("content")
                if isinstance(content, str) and content.strip() == old_label:
                    content = new_label
                elif (
                    isinstance(content, list)
                    and len(content) == 1
                    and isinstance(content[0], dict)
                    and isinstance(content[0].get("text"), str)
                    and content[0]["text"].strip() == old_label
                ):
                    content = [{**content[0], "text": new_label}]
                return {**value, "href": new_href, "content": content}
            return value
        return {key: visit(item) for key, item in value.items()}

    if "[[" in node["text"]:
        node["text"] = rewrite(node["text"])
    if "[[" in json.dumps(node["content"]):
        node["content"] = visit(node["content"])


def document_subtree(path: Path, document_id: str, block_id: str | None = None) -> dict | None:
    tree = document_tree(path, document_id)
    if tree is None:
        return None

    def find(nodes: list[dict], target: str) -> dict | None:
        for node in nodes:
            if node["id"] == target:
                return node
            found = find(node["children"], target)
            if found is not None:
                return found
        return None

    if block_id is None:
        root = {"id": None, "parent": None, "children": tree["children"]}
        return {"document": tree, "parent": None, "depth": 0, "breadcrumbs": [], "subtree": root}
    node = find(tree["children"], block_id)
    if node is None:
        raise KeyError("block not found")
    by_id: dict[str, dict] = {}
    stack = [(child, None) for child in tree["children"]]
    while stack:
        current, parent = stack.pop()
        by_id[current["id"]] = {"node": current, "parent": parent}
        stack.extend((child, current["id"]) for child in current["children"])
    breadcrumbs = []
    current_id: str | None = block_id
    while current_id is not None:
        current = by_id[current_id]["node"]
        breadcrumbs.append({"id": current["id"], "type": current["type"], "text": current["text"]})
        current_id = by_id[current_id]["parent"]
    breadcrumbs.reverse()
    depth = len(breadcrumbs) - 1
    parent_id = by_id[block_id]["parent"]
    parent = by_id[parent_id]["node"] if parent_id else None
    return {
        "document": tree,
        "parent": parent,
        "children": node["children"],
        "depth": depth,
        "breadcrumbs": breadcrumbs,
        "subtree": node,
    }


def document_review_context(
    path: Path,
    document_id: str,
    context: BlockReviewContextKind,
    block_ids: list[str],
) -> dict | None:
    """Resolve canonical target blocks and direct linked context for a review request."""
    initialize(path)
    with connection_scope(path) as connection:
        tree = _document_tree(connection, document_id)
        if tree is None:
            return None

        document_blocks: dict[str, dict] = {}

        def collect(nodes: list[dict]) -> None:
            for node in nodes:
                document_blocks[node["id"]] = node
                collect(node["children"])

        collect(tree["children"])
        if context == "document":
            target_ids = list(document_blocks)
        else:
            invalid_ids = [block_id for block_id in block_ids if block_id not in document_blocks]
            if invalid_ids:
                raise ValueError("block_ids must belong to the requested document")
            target_ids = block_ids

        targets = [document_blocks[block_id] for block_id in target_ids]
        linked_ids: set[str] = set()
        if context == "linked":
            focus_id = target_ids[0]
            linked_ids = {
                row["block_id"]
                for row in connection.execute(
                    """SELECT target_block_id AS block_id FROM block_refs WHERE source_block_id = ?
                       UNION
                       SELECT source_block_id AS block_id FROM block_refs WHERE target_block_id = ?
                       ORDER BY block_id""",
                    (focus_id, focus_id),
                )
            }

        linked_context: list[dict] = []
        if linked_ids:
            placeholders = ",".join("?" for _ in linked_ids)
            rows = connection.execute(
                f"""SELECT blocks.id, blocks.document_id, blocks.type, blocks.attrs_json,
                           blocks.content_json, blocks.text
                      FROM blocks JOIN documents ON documents.id = blocks.document_id
                     WHERE blocks.id IN ({placeholders}) AND documents.deleted_at IS NULL
                     ORDER BY blocks.document_id, blocks.position, blocks.id""",
                sorted(linked_ids),
            ).fetchall()
            linked_context = [
                {
                    "id": row["id"],
                    "document_id": row["document_id"],
                    "type": row["type"],
                    "attrs": json.loads(row["attrs_json"]),
                    "content": json.loads(row["content_json"]),
                    "text": row["text"],
                }
                for row in rows
            ]
        return {"targets": targets, "linked_context": linked_context}


class DocumentRevisionConflict(RuntimeError):
    """Raised when a transaction was composed from an older document version."""

    def __init__(self, document_id: str, base_revision: int, current_revision: int):
        self.document_id = document_id
        self.base_revision = base_revision
        self.current_revision = current_revision
        super().__init__(
            f"document revision conflict: expected {base_revision}, "
            f"current revision is {current_revision}"
        )

    def detail(self) -> dict[str, int | str]:
        return {
            "code": "document_revision_conflict",
            "document_id": self.document_id,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
        }


def apply_transaction(
    path: Path,
    document_id: str,
    operations: list[BlockOperation],
    base_revision: int,
    transaction_id: str | None = None,
) -> dict:
    """Apply operations for ``base_revision`` atomically and return the new tree."""

    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        # Acquire the SQLite writer lock before reading the version. Without this,
        # two writers could both read the same revision before either starts a write.
        connection.execute("BEGIN IMMEDIATE")
        request_hash = hashlib.sha256(
            json.dumps(
                {"base_revision": base_revision, "operations": [operation.model_dump(mode="json") for operation in operations]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if transaction_id is not None:
            receipt = connection.execute(
                "SELECT document_id, request_hash FROM transaction_receipts WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["document_id"] != document_id or receipt["request_hash"] != request_hash:
                    raise ValueError("transaction_id was already used for a different request")
                tree = _document_tree(connection, document_id)
                if tree is None:
                    raise KeyError("document not found")
                return tree
        document = connection.execute(
            "SELECT revision FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        if base_revision != document["revision"]:
            raise DocumentRevisionConflict(
                document_id, base_revision, document["revision"]
            )
        connection.execute(
            "DELETE FROM document_history WHERE document_id = ? AND undone = 1",
            (document_id,),
        )
        patches: list[dict] = []
        for operation in operations:
            if operation.operation == "insert" and operation.block_id is None:
                operation = operation.model_copy(update={"block_id": _new_ulid()})
            target_ids = _history_target_ids(connection, document_id, operation)
            before = {
                block_id: _block_snapshot(
                    connection, block_id, recursive=operation.operation == "delete"
                )
                for block_id in target_ids
            }
            generated_id = _apply_operation(connection, document_id, operation, now)
            if generated_id is not None and generated_id not in target_ids:
                target_ids.append(generated_id)
                before[generated_id] = None
            patches.append(
                {
                    "before": [
                        {"id": block_id, "snapshot": before[block_id]}
                        for block_id in target_ids
                    ],
                    "after": [
                        {
                            "id": block_id,
                            "snapshot": _block_snapshot(
                                connection,
                                block_id,
                                recursive=operation.operation == "duplicate",
                            ),
                        }
                        for block_id in target_ids
                    ],
                }
            )
        # Every block this transaction wrote carries its timestamp: record the
        # revision that last changed it (also in the redo snapshots).
        connection.execute(
            "UPDATE blocks SET revision = ? WHERE document_id = ? AND updated_at = ?",
            (base_revision + 1, document_id, now),
        )
        for patch in patches:
            for entry in patch["after"]:
                _stamp_snapshot_revision(entry["snapshot"], now, base_revision + 1)
        _rebuild_document_references(connection, document_id)
        _rebuild_document_assets(connection, document_id)
        connection.execute(
            "UPDATE documents SET updated_at = ?, revision = revision + 1 WHERE id = ?",
            (now, document_id),
        )
        _refresh_auto_title(connection, document_id)
        connection.execute(
            """INSERT INTO document_history(document_id, revision, patches_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (document_id, base_revision + 1, json.dumps(patches), now),
        )
        tree = _document_tree(connection, document_id)
        if tree is None:  # pragma: no cover - protected by the transaction above
            raise KeyError("document not found")
        if transaction_id is not None:
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id, document_id, request_hash, created_at) VALUES (?, ?, ?, ?)",
                (transaction_id, document_id, request_hash, now),
            )
    return tree


def _stamp_snapshot_revision(node: dict | None, now: str, revision: int) -> None:
    if node is None:
        return
    if node.get("updated_at") == now:
        node["revision"] = revision
    for child in node.get("children", []):
        _stamp_snapshot_revision(child, now, revision)


def _history_target_ids(
    connection: sqlite3.Connection, document_id: str, operation: BlockOperation
) -> list[str]:
    if operation.operation in {"insert", "duplicate"}:
        return [operation.block_id] if operation.operation == "insert" else []
    if not operation.block_id:
        raise ValueError(f"{operation.operation} requires block_id")
    targets = [operation.block_id]
    if operation.operation == "merge":
        row = connection.execute(
            "SELECT parent_id, position FROM blocks WHERE id = ? AND document_id = ?",
            (operation.block_id, document_id),
        ).fetchone()
        if row is not None:
            next_row = connection.execute(
                "SELECT id FROM blocks WHERE document_id = ? AND parent_id IS ? AND position = ?",
                (document_id, row["parent_id"], row["position"] + 1),
            ).fetchone()
            if next_row is not None:
                targets.append(next_row["id"])
    return targets


def _block_snapshot(
    connection: sqlite3.Connection, block_id: str, recursive: bool = True
) -> dict | None:
    rows = (
        _subtree_rows(connection, block_id)
        if recursive
        else connection.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchall()
    )
    if not rows:
        return None
    user_attrs: dict[str, dict[str, object]] = {}
    if recursive:
        attr_rows = connection.execute(
            """WITH RECURSIVE subtree(id) AS (
                   SELECT id FROM blocks WHERE id = ?
                   UNION ALL SELECT blocks.id FROM blocks JOIN subtree
                     ON blocks.parent_id = subtree.id
               ) SELECT block_user_attrs.block_id, block_user_attrs.name, block_user_attrs.value
                   FROM block_user_attrs JOIN subtree ON subtree.id = block_user_attrs.block_id""",
            (block_id,),
        )
    else:
        attr_rows = connection.execute(
            "SELECT block_id, name, value FROM block_user_attrs WHERE block_id = ?",
            (block_id,),
        )
    for item in attr_rows:
        user_attrs.setdefault(item["block_id"], {})[item["name"]] = json.loads(item["value"])
    nodes = {
        row["id"]: {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "position": row["position"],
            "type": row["type"],
            "attrs": json.loads(row["attrs_json"]),
            "content": json.loads(row["content_json"]),
            "text": row["text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "user_attrs": user_attrs.get(row["id"], {}),
            "provenance": _provenance.loads(row["provenance_json"]) or None,
            "revision": row["revision"],
            "children": [],
        }
        for row in rows
    }
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        if parent is not None:
            parent["children"].append(node)
    root = nodes[block_id]
    root["recursive"] = recursive
    return root


def _restore_history_entries(
    connection: sqlite3.Connection,
    document_id: str,
    entries: list[dict],
    now: str,
) -> set[str]:
    restored_ids: set[str] = set()
    current_rows = []
    for entry in entries:
        row = connection.execute(
            "SELECT * FROM blocks WHERE id = ? AND document_id = ?",
            (entry["id"], document_id),
        ).fetchone()
        snapshot = entry["snapshot"]
        if row is not None and (snapshot is None or snapshot.get("recursive", True)):
            current_rows.append(row)
    current_rows.sort(key=lambda row: (str(row["parent_id"]), -row["position"]))
    for row in current_rows:
        _delete_block(connection, document_id, row)
    for entry in entries:
        snapshot = entry["snapshot"]
        if snapshot is not None:
            current = connection.execute(
                "SELECT * FROM blocks WHERE id = ? AND document_id = ?",
                (entry["id"], document_id),
            ).fetchone()
            if current is not None and not snapshot.get("recursive", True):
                _restore_shallow_snapshot(connection, document_id, current, snapshot, now)
            else:
                _insert_snapshot(connection, document_id, snapshot, now)
                restored_ids.update(_snapshot_block_ids(snapshot))
    return restored_ids


def _snapshot_block_ids(node: dict) -> set[str]:
    ids = {node["id"]}
    for child in node["children"]:
        ids.update(_snapshot_block_ids(child))
    return ids


def _restore_shallow_snapshot(
    connection: sqlite3.Connection,
    document_id: str,
    row: sqlite3.Row,
    node: dict,
    now: str,
) -> None:
    if row["parent_id"] != node["parent_id"] or row["position"] != node["position"]:
        _move_block(
            connection,
            document_id,
            row,
            BlockOperation(
                operation="move",
                block_id=node["id"],
                parent_id=node["parent_id"],
                position=node["position"],
            ),
            now,
        )
    connection.execute(
        """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, text = ?,
           created_at = ?, updated_at = ?, provenance_json = ?, revision = ? WHERE id = ?""",
        (
            node["type"], json.dumps(node["attrs"]), json.dumps(node["content"]),
            node["text"], node["created_at"], node["updated_at"],
            _provenance.dumps(node.get("provenance")), node.get("revision"), node["id"],
        ),
    )
    _replace_attrs(connection, node["id"], node["attrs"])
    _replace_user_attrs(connection, node["id"], node["user_attrs"])
    _refresh_fts(connection, node["id"], document_id, node["text"])


def _insert_snapshot(
    connection: sqlite3.Connection, document_id: str, node: dict, now: str
) -> None:
    parent_id = node["parent_id"]
    _validate_parent(connection, document_id, parent_id)
    position = int(node["position"])
    _shift_positions(connection, document_id, parent_id, position, 1)
    attrs = node["attrs"]
    connection.execute(
        """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
           content_json, text, created_at, updated_at, provenance_json, revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            node["id"], document_id, parent_id, position, node["type"],
            json.dumps(attrs), json.dumps(node["content"]), node["text"],
            node["created_at"], node["updated_at"],
            _provenance.dumps(node.get("provenance")), node.get("revision"),
        ),
    )
    _replace_attrs(connection, node["id"], attrs)
    _replace_user_attrs(connection, node["id"], node["user_attrs"])
    _refresh_fts(connection, node["id"], document_id, node["text"])
    for child in node["children"]:
        _insert_snapshot(connection, document_id, child, now)


def _change_history(
    path: Path, document_id: str, base_revision: int, redo: bool
) -> dict:
    initialize(path)
    now = _now()
    with connection_scope(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        document = connection.execute(
            "SELECT revision FROM documents WHERE id = ? AND deleted_at IS NULL",
            (document_id,),
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        if base_revision != document["revision"]:
            raise DocumentRevisionConflict(document_id, base_revision, document["revision"])
        if redo:
            history = connection.execute(
                """SELECT * FROM document_history WHERE document_id = ? AND undone = 1
                   ORDER BY id LIMIT 1""",
                (document_id,),
            ).fetchone()
        else:
            history = connection.execute(
                """SELECT * FROM document_history WHERE document_id = ? AND undone = 0
                   ORDER BY id DESC LIMIT 1""",
                (document_id,),
            ).fetchone()
        if history is None:
            raise ValueError("nothing to redo" if redo else "nothing to undo")
        patches = json.loads(history["patches_json"])
        restored_ids: set[str] = set()
        if redo:
            for patch in patches:
                restored_ids.update(
                    _restore_history_entries(connection, document_id, patch["after"], now)
                )
        else:
            for patch in reversed(patches):
                restored_ids.update(
                    _restore_history_entries(connection, document_id, patch["before"], now)
                )
        _rebuild_document_references(connection, document_id)
        _rebuild_inbound_block_references(connection, restored_ids)
        _rebuild_document_assets(connection, document_id)
        connection.execute(
            "UPDATE document_history SET undone = ? WHERE id = ?",
            (0 if redo else 1, history["id"]),
        )
        connection.execute(
            "UPDATE documents SET updated_at = ?, revision = revision + 1 WHERE id = ?",
            (now, document_id),
        )
        _refresh_auto_title(connection, document_id)
        connection.execute(
            """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
               VALUES (?, NULL, ?, ?)""",
            (document_id, json.dumps({"operation": "redo" if redo else "undo", "history_id": history["id"]}), now),
        )
    tree = document_tree(path, document_id)
    if tree is None:  # pragma: no cover - protected by the transaction above
        raise KeyError("document not found")
    return tree


def undo_transaction(path: Path, document_id: str, base_revision: int) -> dict:
    return _change_history(path, document_id, base_revision, redo=False)


def redo_transaction(path: Path, document_id: str, base_revision: int) -> dict:
    return _change_history(path, document_id, base_revision, redo=True)


def _apply_operation(
    connection: sqlite3.Connection,
    document_id: str,
    operation: BlockOperation,
    now: str,
) -> str | None:
    if operation.operation == "insert":
        return _insert_block(connection, document_id, operation, now)
    if not operation.block_id:
        raise ValueError(f"{operation.operation} requires block_id")
    row = connection.execute(
        "SELECT * FROM blocks WHERE id = ? AND document_id = ?",
        (operation.block_id, document_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"block not found: {operation.block_id}")
    if (
        operation.expected_updated_at is not None
        and operation.expected_updated_at != row["updated_at"]
    ):
        raise RuntimeError(f"block changed since {operation.expected_updated_at}")
    if operation.operation == "update":
        attrs = operation.attrs if operation.attrs is not None else json.loads(row["attrs_json"])
        content = operation.content if operation.content is not None else json.loads(row["content_json"])
        text = operation.text if operation.text is not None else row["text"]
        if operation.content is None and operation.text is not None and isinstance(content, dict):
            if isinstance(content.get("markdown"), str):
                content = {**content, "markdown": operation.text}
        block_type = operation.type if operation.type is not None else row["type"]
        if operation.type is not None and operation.type != row["type"]:
            _check_layout(connection, document_id, block_type, row["parent_id"], row["id"])
        if (
            operation.text is None
            and operation.content is not None
            and isinstance(content, dict)
            and isinstance(content.get("blocknote"), list)
        ):
            text = None  # derived from the new runs
        if block_type == "table" == row["type"] and isinstance(content, dict):
            old_content = json.loads(row["content_json"])
            content = {**content, "blocknote": _types.keep_cell_blocks(
                old_content.get("blocknote") if isinstance(old_content, dict) else None,
                content.get("blocknote"),
            )}
        attrs, content, text = _types.normalize_block(block_type, attrs, content, text)
        provenance_json = (
            row["provenance_json"]
            if operation.provenance is None
            else _provenance.dumps(_provenance.normalize(operation.provenance))
        )
        connection.execute(
            """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, text = ?,
                      updated_at = ?, provenance_json = ? WHERE id = ?""",
            (
                block_type,
                json.dumps(attrs),
                json.dumps(content),
                text,
                now,
                provenance_json,
                operation.block_id,
            ),
        )
        _replace_attrs(connection, operation.block_id, attrs)
        _refresh_fts(connection, operation.block_id, document_id, text)
    elif operation.operation == "set_attrs":
        attrs = operation.attrs if operation.attrs is not None else {}
        if row["type"] in _types.STRUCTURED_TYPES:
            # An image's size or a file's name changes its Markdown and text too.
            attrs, content, text = _types.normalize_block(
                row["type"], attrs, json.loads(row["content_json"]), row["text"]
            )
            connection.execute(
                "UPDATE blocks SET content_json = ?, text = ? WHERE id = ?",
                (json.dumps(content), text, operation.block_id),
            )
            _refresh_fts(connection, operation.block_id, document_id, text)
        connection.execute(
            "UPDATE blocks SET attrs_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(attrs), now, operation.block_id),
        )
        _replace_attrs(connection, operation.block_id, attrs)
    elif operation.operation == "set_user_attrs":
        _replace_user_attrs(connection, operation.block_id, operation.user_attrs or {})
        connection.execute(
            "UPDATE blocks SET updated_at = ? WHERE id = ?",
            (now, operation.block_id),
        )
    elif operation.operation == "move":
        _move_block(connection, document_id, row, operation, now)
    elif operation.operation == "delete":
        _delete_block(connection, document_id, row)
    elif operation.operation == "duplicate":
        generated_id = _duplicate_block(connection, document_id, row, operation, now)
    elif operation.operation == "split":
        generated_id = _split_block(connection, document_id, row, operation, now)
    elif operation.operation == "merge":
        _merge_block(connection, document_id, row, now)
    else:  # pragma: no cover - Pydantic validates operation values
        raise ValueError(f"unsupported operation: {operation.operation}")
    connection.execute(
        """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (document_id, operation.block_id, operation.model_dump_json(), now),
    )
    return generated_id if operation.operation in {"duplicate", "split"} else None


def _insert_block(
    connection: sqlite3.Connection,
    document_id: str,
    operation: BlockOperation,
    now: str,
) -> str:
    block_id = operation.block_id or _new_ulid()
    if connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
        raise ValueError(f"block already exists: {block_id}")
    parent_id = operation.parent_id
    _validate_parent(connection, document_id, parent_id)
    block_type = operation.type or "paragraph"
    _check_layout(connection, document_id, block_type, parent_id)
    attrs, content, text = _types.normalize_block(
        block_type, operation.attrs or {}, operation.content or {}, operation.text
    )
    position = _next_position(connection, document_id, parent_id)
    if operation.position is not None:
        position = operation.position
        _shift_positions(connection, document_id, parent_id, position, 1)
    # A block added in the editor was written here, unless the client says it was
    # pasted, clipped from the web, written by Ask, ...
    block_provenance = (
        _provenance.normalize(operation.provenance)
        if operation.provenance is not None
        else {"source": "manual"}
    )
    connection.execute(
        """INSERT INTO blocks(
               id, document_id, parent_id, position, type, attrs_json, content_json,
               text, created_at, updated_at, provenance_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            block_id,
            document_id,
            parent_id,
            position,
            block_type,
            json.dumps(attrs),
            json.dumps(content),
            text,
            now,
            now,
            _provenance.dumps(block_provenance),
        ),
    )
    _replace_attrs(connection, block_id, attrs)
    _replace_user_attrs(connection, block_id, operation.user_attrs or {})
    _refresh_fts(connection, block_id, document_id, text)
    connection.execute(
        """INSERT INTO revisions(document_id, block_id, operation_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (document_id, block_id, operation.model_dump_json(), now),
    )
    return block_id


def _move_block(
    connection: sqlite3.Connection,
    document_id: str,
    row: sqlite3.Row,
    operation: BlockOperation,
    now: str,
) -> None:
    parent_id = operation.parent_id
    _validate_parent(connection, document_id, parent_id)
    if parent_id == row["id"] or _is_descendant(connection, row["id"], parent_id):
        raise ValueError("cannot move a block into itself or its descendant")
    _check_layout(connection, document_id, row["type"], parent_id)
    old_parent = row["parent_id"]
    old_position = row["position"]
    # Vacate the unique sibling slot before closing the gap in the old list;
    # otherwise the next sibling would collide with the block being moved.
    connection.execute("UPDATE blocks SET position = -1 WHERE id = ?", (row["id"],))
    _shift_positions(
        connection, document_id, old_parent, old_position + 1, -1,
        exclude_block_id=row["id"],
    )
    position = operation.position
    if position is None:
        # The moved block sits at -1, so it never raises the end of its own list.
        position = _next_position(connection, document_id, parent_id)
    _shift_positions(connection, document_id, parent_id, position, 1)
    connection.execute(
        "UPDATE blocks SET parent_id = ?, position = ?, updated_at = ? WHERE id = ?",
        (parent_id, position, now, row["id"]),
    )


def _delete_block(
    connection: sqlite3.Connection, document_id: str, row: sqlite3.Row
) -> None:
    connection.execute(
        """DELETE FROM blocks_fts WHERE block_id IN (
               WITH RECURSIVE descendants(id) AS (
                   SELECT id FROM blocks WHERE id = ?
                   UNION ALL
                   SELECT blocks.id FROM blocks JOIN descendants
                     ON blocks.parent_id = descendants.id
               ) SELECT id FROM descendants
           )""",
        (row["id"],),
    )
    connection.execute(
        "DELETE FROM blocks WHERE id = ? AND document_id = ?", (row["id"], document_id)
    )
    _shift_positions(connection, document_id, row["parent_id"], row["position"] + 1, -1)


def _subtree_rows(connection: sqlite3.Connection, block_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """WITH RECURSIVE subtree(id, depth) AS (
               SELECT id, 0 FROM blocks WHERE id = ?
               UNION ALL SELECT blocks.id, subtree.depth + 1
                 FROM blocks JOIN subtree ON blocks.parent_id = subtree.id
           ) SELECT blocks.* FROM blocks JOIN subtree ON subtree.id = blocks.id
             ORDER BY subtree.depth, blocks.position, blocks.id""", (block_id,)
    ).fetchall()


def _duplicate_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row,
                     operation: BlockOperation, now: str) -> str:
    parent_id = row["parent_id"]
    position = row["position"] + 1 if operation.position is None else operation.position
    _shift_positions(connection, document_id, parent_id, position, 1)
    id_map: dict[str, str] = {}
    document_provenance = _document_provenance(connection, document_id)
    for source in _subtree_rows(connection, row["id"]):
        new_id = _new_ulid()
        id_map[source["id"]] = new_id
        new_parent = id_map.get(source["parent_id"], parent_id)
        new_position = position if source["id"] == row["id"] else source["position"]
        copied_provenance = _provenance.derived_copy(
            _provenance.materialize(source["provenance_json"], document_provenance),
            {"operation": "duplicate", "document_id": document_id,
             "block_id": source["id"], "at": now},
        )
        connection.execute(
            """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
               content_json, text, created_at, updated_at, provenance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, document_id, new_parent, new_position, source["type"], source["attrs_json"],
             source["content_json"], source["text"], now, now,
             _provenance.dumps(copied_provenance)),
        )
        _replace_attrs(connection, new_id, json.loads(source["attrs_json"]))
        source_user_attrs = connection.execute(
            "SELECT name, value FROM block_user_attrs WHERE block_id = ?", (source["id"],)
        ).fetchall()
        connection.executemany(
            "INSERT INTO block_user_attrs(block_id, name, value) VALUES (?, ?, ?)",
            [(new_id, item["name"], item["value"]) for item in source_user_attrs],
        )
        _refresh_fts(connection, new_id, document_id, source["text"])
    return id_map[row["id"]]


def _split_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row,
                 operation: BlockOperation, now: str) -> str:
    if row["type"] in _types.STRUCTURED_TYPES:
        raise ValueError(f"cannot split a {row['type']} block")
    offset = _code_point_offset(row["text"], operation.split_at)
    if offset is None:
        raise ValueError("split requires split_at within block text")
    if len(_subtree_rows(connection, row["id"])) > 1:
        raise ValueError("cannot split a block with children")
    first, second = row["text"][:offset], row["text"][offset:]
    connection.execute("UPDATE blocks SET text = ?, content_json = ?, updated_at = ? WHERE id = ?",
                       (first, json.dumps({"markdown": first}), now, row["id"]))
    _refresh_fts(connection, row["id"], document_id, first)
    _shift_positions(connection, document_id, row["parent_id"], row["position"] + 1, 1)
    new_id = _new_ulid()
    split_provenance = _provenance.derived_copy(
        _provenance.materialize(row["provenance_json"], _document_provenance(connection, document_id)),
        {"operation": "split", "document_id": document_id, "block_id": row["id"], "at": now},
    )
    connection.execute(
        """INSERT INTO blocks(id, document_id, parent_id, position, type, attrs_json,
           content_json, text, created_at, updated_at, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, document_id, row["parent_id"], row["position"] + 1, row["type"],
         row["attrs_json"], json.dumps({"markdown": second}), second, now, now,
         _provenance.dumps(split_provenance)),
    )
    _replace_attrs(connection, new_id, json.loads(row["attrs_json"]))
    _refresh_fts(connection, new_id, document_id, second)
    return new_id


def _code_point_offset(text: str, utf16_offset: int | None) -> int | None:
    """Python index for a UTF-16 offset (what browsers count), or None if it isn't one.

    An emoji is one Python character but two UTF-16 units; an offset that falls
    between the two halves, or past the end, is rejected.
    """

    if utf16_offset is None or utf16_offset < 0:
        return None
    units = 0
    for index, character in enumerate(text):
        if units == utf16_offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > utf16_offset:
            return None
    return len(text) if units == utf16_offset else None


def _merge_block(connection: sqlite3.Connection, document_id: str, row: sqlite3.Row, now: str) -> None:
    next_row = connection.execute(
        "SELECT * FROM blocks WHERE document_id = ? AND parent_id IS ? AND position = ?",
        (document_id, row["parent_id"], row["position"] + 1),
    ).fetchone()
    if next_row is None:
        raise ValueError("cannot merge the last sibling")
    if row["type"] in _types.STRUCTURED_TYPES or next_row["type"] in _types.STRUCTURED_TYPES:
        raise ValueError("only text blocks can be merged")
    if len(_subtree_rows(connection, row["id"])) > 1 or len(_subtree_rows(connection, next_row["id"])) > 1:
        raise ValueError("cannot merge blocks with children")
    merged = row["text"] + next_row["text"]
    connection.execute("UPDATE blocks SET text = ?, content_json = ?, updated_at = ? WHERE id = ?",
                       (merged, json.dumps({"markdown": merged}), now, row["id"]))
    _refresh_fts(connection, row["id"], document_id, merged)
    _delete_block(connection, document_id, next_row)


def _document_provenance(connection: sqlite3.Connection, document_id: str) -> dict:
    row = connection.execute(
        "SELECT provenance_json, source_kind FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return {"source": _provenance.DEFAULT_SOURCE}
    return _provenance.document_provenance(row["provenance_json"], row["source_kind"])


def _check_layout(
    connection: sqlite3.Connection,
    document_id: str,
    block_type: str,
    parent_id: str | None,
    block_id: str | None = None,
) -> None:
    """A ``columns`` block holds only ``column`` blocks, and a column sits only in one."""

    parent_type = None
    if parent_id is not None:
        row = connection.execute(
            "SELECT type FROM blocks WHERE id = ? AND document_id = ?", (parent_id, document_id)
        ).fetchone()
        parent_type = row["type"] if row else None
    if parent_type == "columns" and block_type != "column":
        raise ValueError("a columns block can only hold column blocks")
    if block_type == "column" and parent_type != "columns":
        raise ValueError("a column block must be inside a columns block")
    if block_id is not None and block_type != "columns":
        child = connection.execute(
            "SELECT 1 FROM blocks WHERE parent_id = ? AND type = 'column' LIMIT 1", (block_id,)
        ).fetchone()
        if child is not None:
            raise ValueError("a block holding columns must stay a columns block")


def _validate_parent(
    connection: sqlite3.Connection, document_id: str, parent_id: str | None
) -> None:
    if parent_id is None:
        return
    if not connection.execute(
        "SELECT 1 FROM blocks WHERE id = ? AND document_id = ?", (parent_id, document_id)
    ).fetchone():
        raise KeyError(f"parent block not found: {parent_id}")


def _is_descendant(
    connection: sqlite3.Connection, block_id: str, candidate_parent: str | None
) -> bool:
    if candidate_parent is None:
        return False
    row = connection.execute(
        "SELECT parent_id FROM blocks WHERE id = ?", (candidate_parent,)
    ).fetchone()
    while row is not None and row["parent_id"] is not None:
        if row["parent_id"] == block_id:
            return True
        row = connection.execute(
            "SELECT parent_id FROM blocks WHERE id = ?", (row["parent_id"],)
        ).fetchone()
    return False


def _next_position(
    connection: sqlite3.Connection, document_id: str, parent_id: str | None
) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(position) + 1, 0) AS next_position FROM blocks "
        "WHERE document_id = ? AND parent_id IS ?",
        (document_id, parent_id),
    ).fetchone()
    return int(row["next_position"])


def _shift_positions(
    connection: sqlite3.Connection,
    document_id: str,
    parent_id: str | None,
    from_position: int,
    delta: int,
    exclude_block_id: str | None = None,
) -> None:
    bounds = connection.execute(
        """SELECT MAX(position) AS maximum FROM blocks
           WHERE document_id = ? AND parent_id IS ?""",
        (document_id, parent_id),
    ).fetchone()
    maximum = bounds["maximum"]
    if maximum is None:
        return
    offset = int(maximum) + abs(delta) + 1
    if delta > 0:
        connection.execute(
            """UPDATE blocks SET position = position + ?
               WHERE document_id = ? AND parent_id IS ? AND position >= ?""",
            (offset, document_id, parent_id, from_position),
        )
        connection.execute(
            """UPDATE blocks SET position = position - ? + ?
               WHERE document_id = ? AND parent_id IS ? AND position >= ?""",
            (offset, delta, document_id, parent_id, from_position + offset),
        )
    else:
        connection.execute(
            """UPDATE blocks SET position = position - ?
               WHERE document_id = ? AND parent_id IS ? AND position >= ?""",
            (offset, document_id, parent_id, from_position),
        )
        if exclude_block_id is None:
            connection.execute(
                """UPDATE blocks SET position = position + ? + ?
                   WHERE document_id = ? AND parent_id IS ? AND position < 0""",
                (offset, delta, document_id, parent_id),
            )
        else:
            connection.execute(
                """UPDATE blocks SET position = position + ? + ?
                   WHERE document_id = ? AND parent_id IS ? AND position < 0 AND id <> ?""",
                (offset, delta, document_id, parent_id, exclude_block_id),
            )


def _replace_attrs(
    connection: sqlite3.Connection, block_id: str, attrs: dict[str, object]
) -> None:
    connection.execute("DELETE FROM block_attrs WHERE block_id = ?", (block_id,))
    connection.executemany(
        "INSERT INTO block_attrs(block_id, name, value) VALUES (?, ?, ?)",
        [(block_id, name, json.dumps(value)) for name, value in attrs.items()],
    )


def _replace_user_attrs(
    connection: sqlite3.Connection, block_id: str, user_attrs: dict[str, object]
) -> None:
    connection.execute("DELETE FROM block_user_attrs WHERE block_id = ?", (block_id,))
    connection.executemany(
        "INSERT INTO block_user_attrs(block_id, name, value) VALUES (?, ?, ?)",
        [(block_id, name, json.dumps(value)) for name, value in user_attrs.items()],
    )


def _refresh_fts(
    connection: sqlite3.Connection,
    block_id: str,
    document_id: str,
    text: str,
    replace: bool = True,
) -> None:
    asset_texts = connection.execute(
        """SELECT assets.text FROM block_assets
             JOIN assets USING (content_hash, media_name)
            WHERE block_assets.block_id = ? AND trim(assets.text) <> ''
            ORDER BY block_assets.media_name""",
        (block_id,),
    ).fetchall()
    indexed_text = "\n\n".join([text, *(row["text"] for row in asset_texts)])
    if replace:
        connection.execute("DELETE FROM blocks_fts WHERE block_id = ?", (block_id,))
    connection.execute(
        "INSERT INTO blocks_fts(block_id, document_id, text) VALUES (?, ?, ?)",
        (block_id, document_id, indexed_text),
    )


def _parse_blocks(body: str) -> list[ImportedBlock]:
    # Split exactly where Markdown does: str.splitlines() also breaks on U+2028,
    # form feeds and other separators, which would shift every token's line map.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    tokens = _BLOCK_MARKDOWN.parse(body)
    blocks, _ = _parse_sequence(tokens, lines, 0)
    return blocks


# CommonMark plus GFM tables (which become structured ``table`` blocks).
_BLOCK_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def _fold_layout(blocks: list[ImportedBlock]) -> list[ImportedBlock]:
    """Turn ``<!-- fortress:columns -->`` markers and what follows into columns blocks."""

    result: list[ImportedBlock] = []
    open_layouts: list[list] = []  # [columns block, current column or None]

    def target() -> list[ImportedBlock]:
        if not open_layouts:
            return result
        layout = open_layouts[-1]
        if layout[1] is None:
            layout[1] = ImportedBlock(_new_ulid(), "column", "", {}, [], content={}, text="")
            layout[0].children.append(layout[1])
        return layout[1].children

    for block in blocks:
        if block.type != "__marker":
            target().append(block)
            continue
        kind, value = block.attrs["kind"], block.attrs["value"]
        if kind == "columns":
            columns = ImportedBlock(
                _new_ulid(), "columns", "", value, [], block.provenance, content={}, text=""
            )
            target().append(columns)
            open_layouts.append([columns, None])
        elif kind == "column" and open_layouts:
            try:
                attrs, _, _ = _types.normalize_block("column", value, {}, None)
            except _types.BlockShapeError:
                attrs = {}
            column = ImportedBlock(
                _new_ulid(), "column", "", attrs, [], block.provenance, content={}, text=""
            )
            open_layouts[-1][0].children.append(column)
            open_layouts[-1][1] = column
        elif kind == "/columns" and open_layouts:
            open_layouts.pop()
    return result


def _structured_block(
    block_type: str, source: str, attrs: dict, content: dict | None = None
) -> ImportedBlock | None:
    """A table/image/file/math block read from Markdown, or None if it is not valid."""

    try:
        attrs, content, text = _types.normalize_block(block_type, attrs, content or {}, None)
    except _types.BlockShapeError:
        return None
    return ImportedBlock(
        _new_ulid(), block_type, str(content.get("markdown", source)), attrs,
        content=content, text=text,
    )


def _text_block(
    block_type: str, source: str, inline: str | None, attrs: dict | None = None
) -> ImportedBlock:
    """A text block; one with underline/colour/... HTML also gets its rich runs."""

    attrs = attrs or {}
    if inline is not None and _types.has_inline_mark_html(inline):
        runs = _types.inline_from_markdown(inline)
        if runs is not None:
            return ImportedBlock(
                _new_ulid(), block_type, source, attrs,
                content={"markdown": source, "blocknote": runs},
                text=_types.strip_inline_html(source),
            )
    return ImportedBlock(_new_ulid(), block_type, source, attrs)


def _parse_table(tokens: list[Token], lines: list[str], index: int, indent: int):
    """A GFM table token run as a structured table block."""

    open_token = tokens[index]
    source = _token_source(lines, open_token, indent)
    rows: list[dict] = []
    header_rows = 0
    row: list[dict] | None = None
    in_head = False
    index += 1
    while index < len(tokens) and tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "thead_open":
            in_head = True
        elif token.type == "thead_close":
            in_head = False
        elif token.type == "tr_open":
            row = []
        elif token.type == "tr_close" and row is not None:
            rows.append({"cells": row})
            header_rows += 1 if in_head else 0
            row = None
        elif token.type in {"th_open", "td_open"} and row is not None:
            style = str(token.attrGet("style") or "")
            align = style.partition("text-align:")[2].strip()
            inline = tokens[index + 1].content if tokens[index + 1].type == "inline" else ""
            runs = _types.inline_from_markdown(inline)
            props = {"textAlignment": align} if align in {"center", "right"} else {}
            row.append({"type": "tableCell", "props": props,
                        "content": runs if runs is not None else inline})
        index += 1
    table = {"headerRows": header_rows, "rows": rows}
    block = _structured_block("table", source, {}, {"blocknote": table}) if rows else None
    return block or ImportedBlock(_new_ulid(), "raw", source), index + 1


def _parse_sequence(
    tokens: list[Token], lines: list[str], index: int, stop: str | None = None, indent: int = 0
) -> tuple[list[ImportedBlock], int]:
    """Blocks from ``tokens[index:]``; ``indent`` is the enclosing list item's content column."""

    blocks: list[ImportedBlock] = []
    pending_provenance: dict[str, object] | None = None
    pending_file: tuple[dict, dict | None] | None = None
    while index < len(tokens):
        token = tokens[index]
        if stop and token.type == stop and token.nesting == -1:
            if pending_file is not None:
                blocks.append(_file_block(*pending_file))
            return _fold_layout(blocks), index + 1
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            nested, index = _parse_list(tokens, lines, index, indent)
            if nested and pending_provenance is not None:
                nested[0] = replace(nested[0], provenance=pending_provenance)
                pending_provenance = None
            blocks.extend(nested)
            continue
        block: ImportedBlock | None = None
        if token.type == "html_block":
            # Our own export writes a block's provenance on the line before it.
            restored = _provenance.parse_comment(token.content)
            if restored is not None:
                pending_provenance = restored
                index += 1
                continue
            marker = _types.parse_marker(token.content)
            if marker is not None and marker[0] == "file":
                pending_file = (marker[1], pending_provenance)
                pending_provenance = None
                index += 1
                continue
            if marker is not None:
                block = ImportedBlock(
                    _new_ulid(), "__marker", "", {"kind": marker[0], "value": marker[1]}
                )
                index += 1
            else:
                source = _token_source(lines, token, indent)
                stripped = source.lstrip()
                if stripped[:6].lower() == "<table":
                    parsed = _types.table_from_html(source)
                    if parsed is not None:
                        block = _structured_block(
                            "table", source, parsed[0], {"blocknote": parsed[1]}
                        )
                elif stripped[:4].lower() == "<img":
                    image = _types.image_from_html(source)
                    if image is not None:
                        block = _structured_block("image", source, image)
                if block is not None:
                    index += 1
        if block is not None:
            pass
        elif token.type == "table_open":
            block, index = _parse_table(tokens, lines, index, indent)
        elif token.type == "heading_open":
            block, index = _parse_inline_block(tokens, lines, index, "heading", indent)
        elif token.type == "paragraph_open":
            block, index = _parse_inline_block(tokens, lines, index, "paragraph", indent)
            latex = _types.math_from_markdown(block.source)
            if latex is not None:
                block = _structured_block("math", block.source, {}, {"latex": latex}) or block
        elif token.type == "blockquote_open":
            block, index = _parse_container_block(
                tokens, lines, index, "blockquote_close", "quote", indent
            )
        elif token.type in {"fence", "code_block"}:
            block = ImportedBlock(
                _new_ulid(),
                "code",
                _token_source(lines, token, indent),
                {"language": token.info.strip()} if token.type == "fence" and token.info else {},
            )
            index += 1
        elif token.type == "hr":
            block = ImportedBlock(_new_ulid(), "thematic_break", _token_source(lines, token, indent))
            index += 1
        elif token.type == "html_block":
            block = ImportedBlock(_new_ulid(), "raw", _token_source(lines, token, indent))
            index += 1
        else:
            index += 1
            continue
        if pending_file is not None:
            file_block = _file_block(*pending_file)
            pending_file = None
            if block.type == "paragraph":
                # The paragraph after the marker is the link a Markdown reader sees.
                if pending_provenance is not None and file_block.provenance is None:
                    file_block = replace(file_block, provenance=pending_provenance)
                pending_provenance = None
                blocks.append(file_block)
                continue
            blocks.append(file_block)
        if pending_provenance is not None:
            block = replace(block, provenance=pending_provenance)
            pending_provenance = None
        blocks.append(block)
    if pending_file is not None:
        blocks.append(_file_block(*pending_file))
    return _fold_layout(blocks), index


def _file_block(attrs: dict, provenance: dict | None) -> ImportedBlock:
    block = _structured_block("file", "", attrs) or _structured_block("file", "", {})
    assert block is not None
    return replace(block, provenance=provenance)


def _parse_list(
    tokens: list[Token], lines: list[str], index: int, indent: int = 0
) -> tuple[list[ImportedBlock], int]:
    ordered = tokens[index].type == "ordered_list_open"
    close_type = "ordered_list_close" if ordered else "bullet_list_close"
    index += 1
    blocks: list[ImportedBlock] = []
    while index < len(tokens) and tokens[index].type != close_type:
        if tokens[index].type != "list_item_open":
            index += 1
            continue
        block, index = _parse_list_item(tokens, lines, index, ordered, indent)
        blocks.append(block)
    return blocks, min(index + 1, len(tokens))


def _parse_list_item(
    tokens: list[Token], lines: list[str], index: int, ordered: bool, indent: int = 0
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    # Lines inside the item are indented to its content column; blocks are stored
    # without that indentation (export adds it back), or it would pile up.
    content_indent = indent
    if open_token.map:
        marker = _LIST_CONTENT_RE.match(lines[open_token.map[0]])
        if marker:
            content_indent = len(marker.group(0).expandtabs(4))
    attrs: dict[str, object] = {}
    if ordered:
        attrs["ordered"] = True
    first_token = tokens[index + 1] if index + 1 < len(tokens) else None
    # Parse everything inside the item like a document, so a second paragraph, a
    # heading or raw HTML inside an item is kept as a child instead of dropped.
    children, index = _parse_sequence(
        tokens, lines, index + 1, stop="list_item_close", indent=content_indent
    )
    source = ""
    if (
        children
        and first_token is not None
        and first_token.type not in {"bullet_list_open", "ordered_list_open"}
        and first_token.map
        and open_token.map
        and first_token.map[0] == open_token.map[0]
    ):
        # The block on the marker line is the item's own text.
        first = children.pop(0)
        source = first.source
        rich = first.content if first.type == "paragraph" else None
        if first.type == "paragraph":
            task = _TASK_RE.match(source)
            if task:
                attrs["checked"] = task.group(1).lower() == "x"
                if rich is not None:
                    rich = {**rich, "blocknote": _strip_task_marker(rich.get("blocknote"))}
        if rich is not None:
            return (
                ImportedBlock(_new_ulid(), "list", source, attrs, children,
                              content=rich, text=first.text),
                index,
            )
    if not source:
        # Only the marker: the item's content is all in its children.
        source = f"{open_token.info}{open_token.markup}" if ordered else open_token.markup
    return ImportedBlock(_new_ulid(), "list", source, attrs, children), index


def _strip_task_marker(runs: object) -> object:
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        first = runs[0]
        if first.get("type") == "text":
            text = re.sub(r"^\[[ xX]\]\s+", "", str(first.get("text", "")))
            return ([{**first, "text": text}] if text else []) + runs[1:]
    return runs


def _parse_inline_block(
    tokens: list[Token], lines: list[str], index: int, block_type: str, indent: int = 0
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    source = _token_source(lines, open_token, indent)
    inline = (
        tokens[index + 1].content
        if index + 1 < len(tokens) and tokens[index + 1].type == "inline"
        else None
    )
    index += 1
    while index < len(tokens) and tokens[index].type != f"{block_type}_close":
        index += 1
    block = _text_block(block_type, source, inline)
    if block.content is not None and block_type == "heading" and open_token.tag[1:].isdigit():
        # Rich headings are rendered from their runs, which need the level.
        block = replace(block, attrs={"level": int(open_token.tag[1:])})
    return block, min(index + 1, len(tokens))


def _parse_container_block(
    tokens: list[Token], lines: list[str], index: int, close_type: str, block_type: str,
    indent: int = 0,
) -> tuple[ImportedBlock, int]:
    open_token = tokens[index]
    index += 1
    start = open_token.map[0] if open_token.map else 0
    depth = 1
    while index < len(tokens) and depth:
        token = tokens[index]
        if token.type == open_token.type:
            depth += 1
        elif token.type == close_type:
            depth -= 1
        index += 1
    end = tokens[index - 1].map[1] if index and tokens[index - 1].map else len(lines)
    source = "\n".join(_dedent(line, indent) for line in lines[start:end]).strip()
    return ImportedBlock(_new_ulid(), block_type, source), index


def _token_source(lines: list[str], token: Token, indent: int = 0) -> str:
    if not token.map:
        return ""
    start, end = token.map
    return "\n".join(_dedent(line, indent) for line in lines[start:end]).strip()


_LIST_CONTENT_RE = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]{1,4}(?=\S)|[ \t]*$)")


def _dedent(line: str, indent: int) -> str:
    """``line`` without up to ``indent`` columns of leading whitespace."""

    if not indent:
        return line
    expanded = line.expandtabs(4) if line[:1] == "\t" or "\t" in line[:indent] else line
    stripped = len(expanded) - len(expanded.lstrip(" "))
    return expanded[min(stripped, indent):]


_TITLE_LIMIT = 80
_LINE_MARKER_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)")


def _refresh_auto_title(connection: sqlite3.Connection, document_id: str) -> None:
    """While a note has no chosen title, keep its title in step with its first line."""

    row = connection.execute(
        "SELECT title, title_auto FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None or not row["title_auto"]:
        return
    title = first_line_title(
        text
        for (text,) in connection.execute(
            """WITH RECURSIVE ordered(id, sort_key) AS (
                   SELECT id, printf('%08d', position) FROM blocks
                    WHERE document_id = ? AND parent_id IS NULL
                   UNION ALL
                   SELECT blocks.id, ordered.sort_key || '.' || printf('%08d', blocks.position)
                     FROM blocks JOIN ordered ON blocks.parent_id = ordered.id
               )
               SELECT blocks.text FROM ordered JOIN blocks ON blocks.id = ordered.id
                ORDER BY ordered.sort_key""",
            (document_id,),
        )
    )
    if title != row["title"]:
        connection.execute("UPDATE documents SET title = ? WHERE id = ?", (title, document_id))


def use_auto_title(path: Path, document_id: str) -> None:
    """Let the title follow the first line again (after someone clears it)."""

    initialize(path)
    with connection_scope(path) as connection:
        connection.execute(
            "UPDATE documents SET title_auto = 1 WHERE id = ? AND deleted_at IS NULL",
            (document_id,),
        )
        _refresh_auto_title(connection, document_id)


def first_line_title(texts) -> str:
    """The first non-empty line of the first block with text, trimmed for a title."""

    for text in texts:
        for line in str(text or "").splitlines():
            line = _LINE_MARKER_RE.sub("", line).strip()
            if not line:
                continue
            if len(line) <= _TITLE_LIMIT:
                return line
            cut = line[:_TITLE_LIMIT].rsplit(" ", 1)[0] or line[:_TITLE_LIMIT]
            return cut.rstrip(" ,;:") + "…"
    return "Untitled"


def derive_title(body: str) -> str:
    """A title for new Markdown: the same first-line rule as auto titles."""

    return first_line_title([body])


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
