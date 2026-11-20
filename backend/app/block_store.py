"""SQLite storage for documents, folders and canonical blocks.

The block model itself (types, inline content, derived text) is in
``block_schema``, Markdown in ``block_markdown``; docs/block-model.md is the
contract. This module stores blocks, applies transactions (with undo/redo),
and keeps the derived data in step: text, links, asset links and full-text
search.

Other modules that write documents with SQL of their own (sources, workspace,
duplicates, migrations) use only the public names here, mostly
:func:`document_write`. Everything that must follow a write follows from it:

* derived rows (text, links, asset links, the FTS row) of the documents a
  :class:`DocumentWrite` marks are rebuilt before it commits;
* response caches, the search corpus (``corpus``) and the Markdown mirrors
  (``documents.MirrorWriter``) follow ``change_log``, which triggers keep, so
  no writer has to tell them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import frontmatter
from ulid import ULID

from . import order_keys, textutil
from . import block_schema as schema
from . import provenance as _provenance
from . import vault
from .block_markdown import BlockSpec, parse_markdown
from .block_markdown import render_markdown as _render_markdown
from .block_schema import BlockShapeError
from .models import BlockOperation

SCHEMA_VERSION = 14
DB_FILENAME = ".fortress.sqlite3"

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


def new_id() -> str:
    """Generate a stable-format ID for records created by the block store."""

    return str(ULID())


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
    -- Fractional sibling order key (order_keys.py); unique per parent.
    order_key TEXT NOT NULL,
    type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    -- Inline nodes of text types, NULL otherwise.
    content_json TEXT,
    -- Structured data (math, html), NULL otherwise.
    payload_json TEXT,
    -- Derived plain text (block_schema.derive_text).
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provenance_json TEXT,
    revision INTEGER
);

-- The search corpus fingerprint reads MAX(updated_at) on every search: an index
-- makes that a lookup instead of a scan of every block.
CREATE INDEX IF NOT EXISTS blocks_updated_at ON blocks(updated_at);

CREATE TABLE IF NOT EXISTS block_user_attrs (
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (block_id, name)
);

-- Links derived from docLink/blockRef nodes. Targets may be missing or trashed
-- (readers join to live ones), so a link survives its target being deleted and
-- restored without any bookkeeping.
CREATE TABLE IF NOT EXISTS block_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_block_id TEXT NOT NULL,
    PRIMARY KEY (source_block_id, target_block_id)
);

CREATE TABLE IF NOT EXISTS document_refs (
    source_block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    target_document_id TEXT NOT NULL,
    PRIMARY KEY (source_block_id, target_document_id)
);

-- Re-import state of blocks that came from a living source (sources.py).
CREATE TABLE IF NOT EXISTS block_sync (
    block_id TEXT PRIMARY KEY REFERENCES blocks(id) ON DELETE CASCADE,
    source_hash TEXT,
    dismissed_hash TEXT,
    conflict_json TEXT,
    source_removed INTEGER NOT NULL DEFAULT 0
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

-- Text read from image/PDF assets (app/ocr.py), one row per content and model;
-- assets lacking a finished row for the current model are its queue.
CREATE TABLE IF NOT EXISTS ocr_results (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    text TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    pages INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    duration_ms INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, model)
);

CREATE TABLE IF NOT EXISTS transaction_receipts (
    transaction_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
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
    # 10 (link-check cache) and 11 (saved searches) made tables that were later
    # retired; ``_drop_retired_schema`` removes them.
    10: "",
    12: "",
    13: "",
    11: "",
    14: "",
}

_ready_paths: set[Path] = set()
# Databases whose schema is current, keyed by the path as given, with the file
# identity checked (a deleted or replaced file is initialized again).
_ready_files: dict[Path, tuple[Path, tuple[int, int], int]] = {}
# Creating or migrating a database is check-then-write; concurrent first use from
# several threads would otherwise race (duplicate version rows, lock errors).
_initialize_lock = threading.Lock()
UNSET = object()

# Per-connection settings. WAL lets readers run while one writer commits;
# synchronous=NORMAL is durable across application crashes in WAL mode (a power
# loss can drop the last commits, never corrupt). The page cache and memory map
# keep hot pages (navigation, open notes) out of read() calls.
_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -16000",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA temp_store = MEMORY",
)
# Idle connections kept per thread and database. Request handlers run on a small,
# reused thread pool, so each worker thread keeps its own warm connection (with
# its prepared-statement cache) instead of opening one per request.
_POOL_SIZE = 2


class _Idle(list):
    """A thread's idle connections to one database; closed when the thread ends."""

    def __del__(self) -> None:
        for connection, _identity in self:
            with suppress(Exception):
                connection.close()


_pool = threading.local()


def database_path(notes_path: Path) -> Path:
    return notes_path / DB_FILENAME


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, cached_statements=256)
    try:
        connection.row_factory = sqlite3.Row
        for pragma in _PRAGMAS[:2]:
            connection.execute(pragma)
        # Switching modes needs an exclusive lock and fails at once ("database is
        # locked") while another connection is open, so only switch when needed.
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            connection.execute("PRAGMA journal_mode = WAL")
        for pragma in _PRAGMAS[2:]:
            connection.execute(pragma)
    except BaseException:
        connection.close()
        raise
    return connection


def _checkout(path: Path) -> tuple[sqlite3.Connection, tuple[int, int] | None]:
    idle: dict = getattr(_pool, "idle", None) or {}
    _pool.idle = idle
    entries = idle.get(path)
    identity = _file_identity(path)
    while entries:
        connection, opened_identity = entries.pop()
        # The file was replaced or deleted since: that connection would read
        # (and write) the old one.
        if identity is not None and opened_identity == identity:
            return connection, identity
        connection.close()
    connection = connect(path)
    return connection, identity or _file_identity(path)


def _checkin(path: Path, connection: sqlite3.Connection, identity) -> None:
    entries = _pool.idle.get(path)
    if entries is None:
        entries = _pool.idle[path] = _Idle()
    if connection.in_transaction or len(entries) >= _POOL_SIZE or identity is None:
        connection.close()
        return
    entries.append((connection, identity))


@contextmanager
def connection_scope(path: Path, immediate: bool = False):
    """One connection and transaction; ``immediate`` takes the write lock up front.

    Writes that read sibling positions before shifting them need ``immediate``:
    otherwise two requests can read the same positions before either writes.
    The connection is this thread's pooled one (a nested scope gets its own).
    """

    connection, identity = _checkout(path)
    try:
        with connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        # ``with connection`` committed or rolled back; one still inside a
        # transaction (a failed rollback) is closed rather than reused.
        _checkin(path, connection, identity)


@contextmanager
def read_scope(path: Path):
    """A pooled connection holding one read snapshot for several queries."""

    with connection_scope(path) as connection:
        connection.execute("BEGIN")
        yield connection


def initialize(path: Path, force: bool = False) -> None:
    """Create or migrate the schema; a cheap check once a file is current.

    The check is the file's identity (a deleted or replaced file starts over)
    and SQLite's schema cookie (anything else that changed the schema, e.g. an
    older snapshot copied over it, runs the migrations again).
    """

    if not force:
        ready = _ready_files.get(path)
        if ready is not None and ready[1] == _file_identity(path):
            if _schema_cookie(path) == ready[2]:
                return
    with _initialize_lock:
        _initialize(path)
    identity = _file_identity(path)
    if identity is not None:
        _ready_files[path] = (path.resolve(), identity, _schema_cookie(path))
    _ready_paths.add(path.resolve())


def _schema_cookie(path: Path) -> int:
    connection, identity = _checkout(path)
    try:
        return int(connection.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        _checkin(path, connection, identity)


def _initialize(path: Path) -> None:
    with connection_scope(path) as connection:
        connection.executescript(_SCHEMA)
        row = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        elif row["version"] > SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported block database version {row['version']} (expected {SCHEMA_VERSION})"
            )
        else:
            current = int(row["version"])
            while current < SCHEMA_VERSION:
                current += 1
                # Each step commits with its version row. Rebuilding a table
                # (version 14) needs foreign keys off, which only works outside
                # a transaction.
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                try:
                    connection.execute("BEGIN")
                    _apply_migration(connection, current)
                    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise RuntimeError(f"migration {current} broke a foreign key")
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (current, utc_now()),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
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
        connection.executescript(_PERFORMANCE_INDEXES)
        _drop_retired_schema(connection)
        _ensure_change_tracking(connection)
        if not _fts_rowids_aligned(connection):
            rebuild_fts_rows(connection)


# Lookups that EXPLAIN QUERY PLAN showed as full scans: backlinks (by target),
# the foreign-key checks of every block delete (children by parent_id, refs by
# target), asset links by media name, and a purge's receipts.
_PERFORMANCE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS blocks_sibling_order
    ON blocks(document_id, COALESCE(parent_id, ''), order_key);
CREATE INDEX IF NOT EXISTS block_refs_target ON block_refs(target_block_id);
CREATE INDEX IF NOT EXISTS document_refs_target ON document_refs(target_document_id);
CREATE INDEX IF NOT EXISTS blocks_parent ON blocks(parent_id);
CREATE INDEX IF NOT EXISTS assets_media_name ON assets(media_name);
CREATE INDEX IF NOT EXISTS block_assets_asset ON block_assets(content_hash, media_name);
CREATE INDEX IF NOT EXISTS transaction_receipts_document ON transaction_receipts(document_id);
"""

# Change tracking for HTTP validators (ETags) and server-side response caches.
# ``change_log.seq`` grows with every change to anything a cached response
# shows; ``documents.change_seq`` is the seq of the last change to that note's
# own rows (blocks, user attrs, links, history, the document row). ``epoch``
# changes when a backup is restored, so validators from before never match.
# last_opened_at is deliberately not tracked: it is metadata of a read.
_DOCUMENT_COLUMNS = (
    "title, status, folder_id, position, deleted_at, created_at, updated_at, "
    "revision, title_auto, provenance_json, source_kind, source_external_id, sync_json"
)


def _bump(document_expr: str | None) -> str:
    sql = "UPDATE change_log SET seq = seq + 1 WHERE id = 1;"
    if document_expr:
        sql += (
            " UPDATE documents SET change_seq = (SELECT seq FROM change_log WHERE id = 1)"
            f" WHERE id IN ({document_expr});"
        )
    return sql


_CHANGE_TRIGGERS = {
    "blocks_changed_insert": ("AFTER INSERT ON blocks", _bump("NEW.document_id")),
    "blocks_changed_update": (
        "AFTER UPDATE ON blocks", _bump("NEW.document_id, OLD.document_id")
    ),
    "blocks_changed_delete": ("AFTER DELETE ON blocks", _bump("OLD.document_id")),
    "documents_changed_insert": ("AFTER INSERT ON documents", _bump("NEW.id")),
    "documents_changed_update": (
        f"AFTER UPDATE OF {_DOCUMENT_COLUMNS} ON documents", _bump("NEW.id")
    ),
    "documents_changed_delete": ("AFTER DELETE ON documents", _bump(None)),
    "folders_changed_insert": ("AFTER INSERT ON folders", _bump(None)),
    "folders_changed_update": ("AFTER UPDATE ON folders", _bump(None)),
    "folders_changed_delete": ("AFTER DELETE ON folders", _bump(None)),
    "document_history_changed_insert": (
        "AFTER INSERT ON document_history", _bump("NEW.document_id")
    ),
    "document_history_changed_update": (
        "AFTER UPDATE ON document_history", _bump("NEW.document_id")
    ),
    "document_history_changed_delete": (
        "AFTER DELETE ON document_history", _bump("OLD.document_id")
    ),
}
# A picture's OCR state and text show in the info of every note that contains it.
for _event, _row in (("insert", "NEW"), ("update", "NEW")):
    _CHANGE_TRIGGERS[f"ocr_results_changed_{_event}"] = (
        f"AFTER {_event.upper()} ON ocr_results",
        _bump(
            "SELECT blocks.document_id FROM block_assets"
            " JOIN blocks ON blocks.id = block_assets.block_id"
            f" WHERE block_assets.content_hash = {_row}.content_hash"
        ),
    )
# Text read from a picture (OCR) shows in the mirror and info of every note showing
# it. It lands after the ocr_results row that bumped them: bump them again.
_CHANGE_TRIGGERS["assets_text_changed_update"] = (
    "AFTER UPDATE OF text ON assets",
    _bump(
        "SELECT blocks.document_id FROM block_assets"
        " JOIN blocks ON blocks.id = block_assets.block_id"
        " WHERE block_assets.content_hash = NEW.content_hash"
        " AND block_assets.media_name = NEW.media_name"
    ),
)
for _table, _column in (
    ("block_user_attrs", "block_id"),
    ("block_sync", "block_id"),
    ("block_refs", "source_block_id"),
    ("document_refs", "source_block_id"),
):
    for _event, _row in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
        _CHANGE_TRIGGERS[f"{_table}_changed_{_event}"] = (
            f"AFTER {_event.upper()} ON {_table}",
            _bump(f"SELECT document_id FROM blocks WHERE id = {_row}.{_column}"),
        )


def _drop_retired_schema(connection: sqlite3.Connection) -> None:
    """Remove what retired features stored: tags, saved searches, link checks.

    Idempotent and version-free (runs on every initialize, a no-op once done).
    The documents update trigger names ``tags_json``, so it is dropped first;
    ``_ensure_change_tracking`` recreates it without the column.
    """

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
    if "tags_json" in columns:
        connection.execute("DROP TRIGGER IF EXISTS documents_changed_update")
        connection.execute("ALTER TABLE documents DROP COLUMN tags_json")
    connection.execute("DROP TABLE IF EXISTS saved_searches")
    connection.execute("DROP TABLE IF EXISTS link_check_cache")


def _ensure_change_tracking(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "documents", {"change_seq": "INTEGER NOT NULL DEFAULT 0"})
    connection.execute(
        """CREATE TABLE IF NOT EXISTS change_log (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               seq INTEGER NOT NULL,
               epoch TEXT NOT NULL
           )"""
    )
    connection.execute(
        "INSERT OR IGNORE INTO change_log(id, seq, epoch)"
        " VALUES (1, 0, lower(hex(randomblob(8))))"
    )
    for name, (event, body) in _CHANGE_TRIGGERS.items():
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {event} BEGIN {body} END")


def _fts_rowids_aligned(connection: sqlite3.Connection) -> bool:
    """Whether every FTS row has its block's rowid (``_fts_insert`` relies on it).

    Databases indexed before that rule get their FTS rebuilt once.
    """

    return connection.execute(
        """SELECT 1 FROM blocks_fts LEFT JOIN blocks ON blocks.rowid = blocks_fts.rowid
            WHERE blocks.id IS NOT blocks_fts.block_id LIMIT 1"""
    ).fetchone() is None


def new_change_epoch(path: Path) -> None:
    """Invalidate every validator handed out so far (e.g. after a restore)."""

    initialize(path)
    with connection_scope(path) as connection:
        connection.execute(
            "UPDATE change_log SET seq = seq + 1, epoch = lower(hex(randomblob(8)))"
            " WHERE id = 1"
        )


def change_state(path: Path) -> tuple[str, int]:
    """(epoch, seq): changes whenever anything a cached response shows changes."""

    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute("SELECT epoch, seq FROM change_log WHERE id = 1").fetchone()
    return str(row["epoch"]), int(row["seq"])


def register_asset(db_path: Path, asset_path: Path, content_hash: str, mime_type: str) -> None:
    """Record an uploaded asset before a block references it."""
    initialize(db_path)
    with connection_scope(db_path) as connection:
        connection.execute(
            """INSERT INTO assets(content_hash, media_name, mime_type, file_path, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, media_name) DO UPDATE SET
                 mime_type = excluded.mime_type, file_path = excluded.file_path""",
            (content_hash, asset_path.name, mime_type, str(asset_path), utc_now()),
        )


def store_asset_text(path: Path, content_hash: str, text: str) -> None:
    initialize(path)
    with connection_scope(path) as connection:
        cursor = connection.execute(
            "UPDATE assets SET text = ?, text_updated_at = ? "
            "WHERE content_hash = ? AND text <> ?",
            (text, utc_now(), content_hash, text),
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


def backfill_assets(db_path: Path, assets_path: Path) -> int:
    """Register older generated media files and migrate their text sidecars.

    Only ULID-named upload files with a supported media extension are considered.
    A ``<id>.txt`` sidecar (written by the OCR before ``ocr.py``) fills an empty
    row's text until the OCR queue reads the file again. Existing rows are
    preserved, making startup retries safe.
    """
    initialize(db_path)
    if not assets_path.is_dir():
        return 0
    registered = 0
    changed = False
    with connection_scope(db_path) as connection:
        # Registered files are not read again (this runs at every startup, and
        # attachments can be large): only a sidecar text an empty row lacks.
        known = {
            row["media_name"]: (row["content_hash"], row["text"])
            for row in connection.execute("SELECT media_name, content_hash, text FROM assets")
        }
        for asset_path in sorted(assets_path.iterdir()):
            mime_type = _MIME_BY_EXTENSION.get(asset_path.suffix.lower())
            if (
                not mime_type
                or not _UPLOAD_ID_RE.fullmatch(asset_path.stem)
                or asset_path.is_symlink()
                or not asset_path.is_file()
            ):
                continue
            old_sidecar = asset_path.with_suffix(".txt")
            existing = known.get(asset_path.name)
            if existing is not None and (existing[1] or not old_sidecar.is_file()):
                registered += 1
                continue
            try:
                text = (
                    old_sidecar.read_text(encoding="utf-8")
                    if old_sidecar.is_file() and not old_sidecar.is_symlink()
                    else ""
                )
            except (OSError, UnicodeError):
                text = ""
            if existing is not None:
                content_hash = existing[0]
                if not text:
                    registered += 1
                    continue
            else:
                try:
                    content_hash = _file_sha256(asset_path)
                except OSError:
                    continue
            changed = True
            registered_at = utc_now()
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
            registered += 1
        if changed:
            document_ids = connection.execute("SELECT id FROM documents").fetchall()
            for row in document_ids:
                rebuild_document_derived(connection, row["id"])
    if changed:
        rebuild_fts(db_path)
    return registered


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
    if version == 14:
        from . import block_migration

        block_migration.migrate(connection)
        return
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
            now = utc_now()
            created_at = _iso(post.metadata.get("created_at", now))
            updated_at = _iso(post.metadata.get("updated_at", now))
            title = str(post.metadata.get("title") or derive_title(post.content))
            status = post.metadata.get("status", "rough")
            if status not in {"rough", "polished"}:
                status = "rough"
            _insert_document(
                connection,
                note_id,
                title,
                status,
                post.content,
                created_at,
                updated_at,
                provenance=markdown_import_provenance(
                    post.metadata, note_path.name, now
                ),
            )
            imported += 1
    return imported


MARKDOWN_IMPORTER = {"name": "fortress-markdown", "version": "1"}


def markdown_import_provenance(
    metadata: dict, source_path: str, now: str
) -> dict[str, object]:
    """Provenance for a note read from a Markdown file.

    Our own export and mirror write the note's provenance in front matter
    (``fortress.provenance``; older versions ``provenance``); it is restored as
    it was (so export -> import -> export is stable). Any other file is a
    ``markdown_import`` (with its ``source`` URL, as web clippers write it).
    """

    note = vault.read_front_matter(metadata)
    if note.provenance:
        return note.provenance
    value: dict[str, object] = {
        "source": "markdown_import",
        "source_path": source_path,
        "imported_at": now,
        "importer": dict(MARKDOWN_IMPORTER),
    }
    for stamp, name in ((note.created, "source_created_at"), (note.updated, "source_modified_at")):
        if stamp is not None:
            value[name] = _iso(stamp)
    source = metadata.get("source")
    if isinstance(source, str) and re.match(r"^https?://", source.strip()):
        value["source_url"] = source.strip()
    try:
        return _provenance.normalize(value, trusted=True) or value
    except _provenance.ProvenanceError:
        return {key: item for key, item in value.items() if key != "source_url"}


def create_document(
    path: Path,
    document_id: str,
    title: str,
    status: str,
    body: str,
    created_at: object,
    updated_at: object,
    ensure_empty_paragraph: bool = False,
    folder_id: str | None = None,
    provenance: dict[str, object] | None = None,
    specs: list[BlockSpec] | None = None,
) -> None:
    """Create a note from Markdown ``body`` (or already parsed ``specs``)."""

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
            body,
            _iso(created_at),
            _iso(updated_at),
            ensure_empty_paragraph,
            provenance,
            specs,
        )
        if folder_id is not None:
            position = document_insert_position(connection, folder_id, None)
            connection.execute(
                "UPDATE documents SET folder_id = ?, position = ? WHERE id = ?",
                (folder_id, position, document_id),
            )


# ``POST .../opened`` writes last_opened_at, at most once a minute per note (the
# app sends one per note it shows). It is not part of the note's ETag.
OPEN_MARK_INTERVAL_S = 60.0
_opened_marks: dict[tuple[Path, str], float] = {}
_opened_lock = threading.Lock()


def mark_opened(path: Path, document_id: str, *, wait: bool = True) -> bool:
    """Remember when a note was last opened (metadata only: no revision, no edit time).

    Debounced per note (``OPEN_MARK_INTERVAL_S``). With ``wait=False`` the write
    is skipped, not waited for, while another writer holds the database (the
    next open tries again). Returns whether it wrote.
    """

    key = (path, document_id)
    now = time.monotonic()
    with _opened_lock:
        last = _opened_marks.get(key)
        if last is not None and now - last < OPEN_MARK_INTERVAL_S:
            return False
    initialize(path)
    try:
        with connection_scope(path) as connection:
            if not wait:
                connection.execute("PRAGMA busy_timeout = 0")
            try:
                connection.execute(
                    "UPDATE documents SET last_opened_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (utc_now(), document_id),
                )
            finally:
                if not wait:
                    connection.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.OperationalError:
        if wait:
            raise
        return False
    with _opened_lock:
        if len(_opened_marks) > 10_000:
            _opened_marks.clear()
        _opened_marks[key] = now
    return True


def document_title(path: Path, document_id: str) -> str | None:
    """A live document's title (None if missing or trashed)."""

    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute(
            "SELECT title FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
    return None if row is None else str(row["title"])


def block_text(path: Path, document_id: str, block_id: str) -> str | None:
    """One block's stored text (None if the block is not in that live document)."""

    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute(
            """SELECT blocks.text FROM blocks JOIN documents ON documents.id = blocks.document_id
                WHERE blocks.id = ? AND blocks.document_id = ? AND documents.deleted_at IS NULL""",
            (block_id, document_id),
        ).fetchone()
    return None if row is None else str(row["text"])


def document_exists_any(path: Path, document_id: str) -> bool:
    """Whether a note (active or in the trash) has this id."""

    initialize(path)
    with connection_scope(path) as connection:
        return connection.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone() is not None


def document_exists(path: Path, document_id: str) -> bool:
    initialize(path)
    with connection_scope(path) as connection:
        return connection.execute(
            "SELECT 1 FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone() is not None


def _document_validator(connection: sqlite3.Connection, document_id: str) -> tuple | None:
    """What a document payload depends on, as a tuple that changes when it does.

    Its own rows (``change_seq``) and the newest change of every note or block
    it links to (their titles and text are in
    ``resolved_links`` and ``[[id|Title]]`` labels). ``change_seq`` values grow
    globally, so a change to any link target raises the maximum; a link that
    appears or disappears changes this note's own rows.
    """

    row = connection.execute(
        """SELECT change_log.epoch, documents.change_seq,
                  (SELECT MAX(target.change_seq)
                     FROM blocks AS source
                     JOIN document_refs AS refs ON refs.source_block_id = source.id
                     JOIN documents AS target ON target.id = refs.target_document_id
                    WHERE source.document_id = documents.id) AS document_targets,
                  (SELECT MAX(target_doc.change_seq)
                     FROM blocks AS source
                     JOIN block_refs AS refs ON refs.source_block_id = source.id
                     JOIN blocks AS target ON target.id = refs.target_block_id
                     JOIN documents AS target_doc ON target_doc.id = target.document_id
                    WHERE source.document_id = documents.id) AS block_targets
             FROM documents, change_log
            WHERE documents.id = ? AND documents.deleted_at IS NULL AND change_log.id = 1""",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    # Not last_opened_at: opening a note (a read) never changes its ETag.
    return (row["epoch"], row["change_seq"], row["document_targets"], row["block_targets"])


def document_validator(path: Path, document_id: str) -> tuple | None:
    """The document's validator (see ``_document_validator``); None if missing."""

    initialize(path)
    with connection_scope(path) as connection:
        return _document_validator(connection, document_id)


def document_tree_with_validator(path: Path, document_id: str) -> tuple[tuple, dict] | None:
    """The tree and its validator, read from one snapshot."""

    initialize(path)
    with read_scope(path) as connection:
        validator = _document_validator(connection, document_id)
        if validator is None:
            return None
        tree = _document_tree(connection, document_id)
    return (validator, tree) if tree is not None else None


def document_info_validator(path: Path, document_id: str) -> tuple | None:
    """Info and backlinks show other notes too: the workspace change state."""

    initialize(path)
    with connection_scope(path) as connection:
        row = connection.execute(
            """SELECT change_log.epoch, change_log.seq, documents.last_opened_at
                 FROM documents, change_log
                WHERE documents.id = ? AND documents.deleted_at IS NULL AND change_log.id = 1""",
            (document_id,),
        ).fetchone()
    return None if row is None else tuple(row)


def ensure_editable_block(path: Path, document_id: str | None = None) -> int:
    """Give live documents with no blocks one empty paragraph; how many got one.

    One document, or every live document when ``document_id`` is None (startup
    repairs older data). Called where a note can end up empty - create, import,
    restore - never on a read. The editor needs a paragraph with a stable ID the
    server knows; the content is unchanged (an empty paragraph exports as
    nothing), so the revision and history stay as they are.
    """

    initialize(path)
    with connection_scope(path) as connection:
        if not _needs_editable_block(connection, document_id):
            return 0
    with connection_scope(path, immediate=True) as connection:
        ids = _needs_editable_block(connection, document_id)
        now = utc_now()
        for empty_id in ids:
            _insert_markdown_body(connection, empty_id, "", now, now, ensure_empty_paragraph=True)
    return len(ids)


def _needs_editable_block(connection: sqlite3.Connection, document_id: str | None) -> list[str]:
    only = "" if document_id is None else "AND id = ?"
    return [
        row[0]
        for row in connection.execute(
            f"""SELECT id FROM documents
                 WHERE deleted_at IS NULL {only}
                   AND NOT EXISTS (SELECT 1 FROM blocks WHERE blocks.document_id = documents.id)""",
            () if document_id is None else (document_id,),
        )
    ]


def list_documents(path: Path, ids: list[str] | None = None) -> list[dict[str, object]]:
    """Active documents, newest first; only ``ids`` when given (one query either way)."""

    initialize(path)
    only = "" if ids is None else f"AND id IN ({','.join('?' for _ in ids)})"
    with connection_scope(path) as connection:
        rows = connection.execute(
            f"""SELECT id, title, status, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((SELECT text FROM blocks WHERE blocks.document_id = documents.id
                                AND text <> '' ORDER BY parent_id IS NOT NULL, order_key
                                LIMIT 1), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL {only}
                ORDER BY updated_at DESC, id ASC""",
            [] if ids is None else list(ids),
        ).fetchall()
    return [_navigation_document(row) for row in rows]


def create_block_document(
    path: Path, title: str, status: str, body: str,
    folder_id: str | None = None,
    created_at: object | None = None,
    title_auto: bool = False,
    provenance: dict[str, object] | None = None,
    document_id: str | None = None,
    specs: list[BlockSpec] | None = None,
) -> dict:
    """Create a document; with ``title_auto`` its title follows its first line.

    ``specs`` (already parsed blocks, e.g. with links resolved across an import)
    replace ``body``; ``document_id`` is used when no note has it yet.
    """

    if document_id is None or document_exists_any(path, document_id):
        document_id = new_id()
    now = utc_now()
    create_document(
        path, document_id, title, status, body,
        _iso(created_at) if created_at is not None else now, now,
        ensure_empty_paragraph=True,
        folder_id=folder_id,
        provenance=provenance,
        specs=specs,
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
            (title, utc_now(), document_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("document not found")
        document_retitled(connection, document_id)
    tree = document_tree(path, document_id)
    assert tree is not None
    return tree


UNSET_TITLE = "Untitled"


def update_document_metadata(
    path: Path,
    document_id: str,
    *,
    title: str | None = None,
    status: str | None = None,
) -> tuple[dict, tuple]:
    """Change a document's title and/or status; KeyError if it is missing.

    A title of ``""`` or ``"Untitled"`` hands the title back to the first line
    (``title_auto``); any other title is the person's choice. Reads and writes
    only the document row (never the blocks' tree) and returns the document's
    metadata payload (``children`` empty) with its validator, read in the same
    transaction.
    """

    initialize(path)
    with connection_scope(path, immediate=True) as connection:
        row = connection.execute(
            "SELECT title, status FROM documents WHERE id = ? AND deleted_at IS NULL",
            (document_id,),
        ).fetchone()
        if row is None:
            raise KeyError("document not found")
        clearing = title is not None and title.strip() in ("", UNSET_TITLE)
        connection.execute(
            """UPDATE documents
                  SET title = ?, status = ?, updated_at = ?,
                      title_auto = CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE title_auto END
                WHERE id = ?""",
            (
                row["title"] if title is None or clearing else title,
                status or row["status"],
                utc_now(),
                clearing,
                title is not None,
                document_id,
            ),
        )
        if clearing:
            _refresh_auto_title(connection, document_id)
        elif title is not None and title != row["title"]:
            document_retitled(connection, document_id)
        meta = _document_tree(connection, document_id, include_children=False)
        validator = _document_validator(connection, document_id)
    assert meta is not None and validator is not None
    return meta, validator


def delete_document(path: Path, document_id: str) -> None:
    """Move a document to the trash: hidden everywhere, recoverable until purged."""

    initialize(path)
    now = utc_now()
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
        shift_document_positions(connection, row["folder_id"], row["position"] + 1, -1)


def create_folder(
    path: Path, name: str, parent_id: str | None = None, position: int | None = None
) -> dict[str, object]:
    initialize(path)
    now = utc_now()
    with connection_scope(path, immediate=True) as connection:
        _validate_folder_parent(connection, parent_id)
        target_position = _folder_insert_position(connection, parent_id, position)
        folder_id = new_id()
        connection.execute(
            """INSERT INTO folders(id, parent_id, name, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (folder_id, parent_id, name, target_position, now, now),
        )
        row = _folder_row(connection, folder_id)
    return _folder_data(row)


def rename_folder(path: Path, folder_id: str, name: str) -> dict[str, object]:
    initialize(path)
    now = utc_now()
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
    now = utc_now()
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
    now = utc_now()
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
        shift_document_positions(
            connection, document["folder_id"], document["position"] + 1, -1
        )
        target_position = document_insert_position(
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
            """SELECT id, title, status, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id AND text <> ''
                           ORDER BY parent_id IS NOT NULL, order_key LIMIT 1
                      ), '') AS snippet
                 FROM documents
                WHERE deleted_at IS NULL
                ORDER BY folder_id, position, id"""
        ).fetchall()
        recent = connection.execute(
            """SELECT id, title, status, folder_id, position,
                      created_at, updated_at, source_kind,
                      COALESCE((
                          SELECT text FROM blocks
                           WHERE blocks.document_id = documents.id AND text <> ''
                           ORDER BY parent_id IS NOT NULL, order_key LIMIT 1
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


def document_insert_position(
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
    shift_document_positions(connection, folder_id, target_position, 1)
    return target_position


def shift_document_positions(
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
    """Check that FTS contains exactly the active canonical blocks.

    ``blocks_fts.block_id`` is UNINDEXED, so a SQL join on it scans the whole FTS
    table once per block (minutes on a large workspace). Comparing the two ID
    lists is linear.
    """

    initialize(path)
    with read_scope(path) as connection:
        # Each FTS row also has its block's rowid (see ``_fts_insert``).
        active = dict(
            connection.execute(
                """SELECT blocks.rowid, blocks.id FROM blocks
                     JOIN documents ON documents.id = blocks.document_id
                    WHERE documents.deleted_at IS NULL"""
            ).fetchall()
        )
        indexed = dict(connection.execute("SELECT rowid, block_id FROM blocks_fts").fetchall())
    return active == indexed


def rebuild_fts(path: Path) -> None:
    """Rebuild the disposable FTS rows from active canonical blocks."""

    initialize(path)
    with connection_scope(path) as connection:
        rebuild_fts_rows(connection)


def rebuild_fts_rows(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM blocks_fts")
    connection.execute(
        """INSERT INTO blocks_fts(rowid, block_id, document_id, text)
             SELECT blocks.rowid, blocks.id, blocks.document_id,
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
                             ORDER BY first_block.order_key ASC
                             LIMIT 1
                        )
                   )
                  )
                -- An exact block ID (a reference preview looks one up) comes first.
                ORDER BY blocks.id = ? DESC, documents.updated_at DESC, blocks.updated_at DESC,
                         blocks.order_key ASC, documents.id ASC, blocks.id ASC
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
                           '' AS label, refs.target_block_id, source.updated_at AS updated_at
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
                           '' AS label, NULL AS target_block_id, source.updated_at AS updated_at
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
    body: str,
    created_at: str,
    updated_at: str,
    ensure_empty_paragraph: bool = False,
    provenance: dict[str, object] | None = None,
    specs: list[BlockSpec] | None = None,
) -> None:
    position = connection.execute(
        """SELECT COALESCE(MAX(position) + 1, 0)
             FROM documents
            WHERE folder_id IS NULL AND deleted_at IS NULL"""
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO documents(
                   id, title, status, folder_id, position, deleted_at,
                   created_at, updated_at
               ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)""",
        (document_id, title, status, position, created_at, updated_at),
    )
    _set_document_provenance(
        connection, document_id, _unclaimed_source_key(connection, provenance or {"source": "manual"})
    )
    _insert_markdown_body(
        connection, document_id, body, created_at, updated_at, ensure_empty_paragraph, specs
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
        "folder_id": row["folder_id"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "snippet": row["snippet"][:160],
        # Where the note came from ("manual", "capture", "onenote_import", ...).
        "source": row["source_kind"] if "source_kind" in row.keys() else "manual",
    }


# --- Writing blocks ----------------------------------------------------------------------------


def _insert_markdown_body(
    connection: sqlite3.Connection,
    document_id: str,
    body: str,
    created_at: str,
    updated_at: str,
    ensure_empty_paragraph: bool = False,
    specs: list[BlockSpec] | None = None,
) -> None:
    """A document's blocks from a Markdown body (ids from it when free) or parsed specs."""

    if specs is None:
        specs = parse_markdown(body)
    if not specs and ensure_empty_paragraph:
        # The editor always has a paragraph to type into; give it a stable id.
        specs = [BlockSpec("paragraph", content=[])]
    written = insert_specs(
        connection, document_id, specs, None, None, None, created_at, updated_at, None
    )
    refresh_derived(connection, document_id, written)


def insert_specs(
    connection: sqlite3.Connection,
    document_id: str,
    specs: list[BlockSpec],
    parent_id: str | None,
    after_key: str | None,
    before_key: str | None,
    created_at: str,
    updated_at: str,
    revision: int | None,
    changes: "ChangeRecorder | None" = None,
    default_provenance: dict[str, object] | None = None,
) -> list[str]:
    """Insert canonical block trees between two sibling keys; returns the new ids.

    A spec's id is used when no block has it yet (Markdown read back keeps its
    ids); otherwise the block gets a new one. Specs are validated against their
    parent. Derived fields (text, links, search) are left to ``refresh_derived``.
    """

    parent_type = None
    if parent_id is not None:
        parent_type = connection.execute(
            "SELECT type FROM blocks WHERE id = ?", (parent_id,)
        ).fetchone()["type"]
    written: list[str] = []
    keys = order_keys.keys_between(after_key, before_key, len(specs))
    for block_spec, key in zip(specs, keys):
        schema.check_nesting(parent_type, block_spec.type)
        block_id = block_spec.id
        if not block_id or connection.execute(
            "SELECT 1 FROM blocks WHERE id = ?", (block_id,)
        ).fetchone():
            block_id = new_id()
        if changes is not None:
            changes.touch(block_id)
        provenance = block_spec.provenance or default_provenance
        connection.execute(
            """INSERT INTO blocks(id, document_id, parent_id, order_key, type, attrs_json,
                   content_json, payload_json, text, created_at, updated_at, provenance_json,
                   revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
            (
                block_id, document_id, parent_id, key, block_spec.type,
                _dumps(block_spec.attrs), _dumps_nullable(block_spec.content),
                _dumps_nullable(block_spec.payload), created_at, updated_at,
                _provenance.dumps(provenance), revision,
            ),
        )
        written.append(block_id)
        written.extend(insert_specs(
            connection, document_id, block_spec.children, block_id, None, None,
            created_at, updated_at, revision, changes,
        ))
    return written


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dumps_nullable(value: object) -> str | None:
    return None if value is None else _dumps(value)


def _loads(raw: str | None) -> object:
    if raw is None:
        return None
    return {} if raw == "{}" else json.loads(raw)


def _row_block(row: sqlite3.Row) -> schema.Block:
    return schema.Block(
        row["type"], _loads(row["attrs_json"]) or {}, _loads(row["content_json"]),
        _loads(row["payload_json"]),
    )


class _Labels:
    """What ``docLink``/``blockRef`` nodes read as in derived text (cached per write)."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.cache: dict[tuple[str, str], str] = {}

    def __call__(self, kind: str, target: str) -> str:
        key = (kind, target)
        if key not in self.cache:
            if kind == "docLink":
                row = self.connection.execute(
                    "SELECT title FROM documents WHERE id = ?", (target,)
                ).fetchone()
                self.cache[key] = str(row["title"]) if row else ""
            else:
                row = self.connection.execute(
                    "SELECT text FROM blocks WHERE id = ?", (target,)
                ).fetchone()
                self.cache[key] = block_label(str(row["text"])) if row else ""
        return self.cache[key]


def block_label(text: str) -> str:
    """A referenced block as it reads inside another block: its first line, shortened."""

    line = next((part.strip() for part in text.split("\n") if part.strip()), "")
    return line if len(line) <= 80 else line[:79].rstrip() + "…"


def refresh_derived(
    connection: sqlite3.Connection,
    document_id: str,
    block_ids: Iterable[str],
    *,
    removed: Iterable[str] = (),
    retitled: Iterable[str] = (),
) -> None:
    """Re-derive text, links, asset links and the search row of ``block_ids``.

    Blocks elsewhere whose text shows one of these blocks (a ``blockRef``), a
    ``removed`` block, or the title of a ``retitled`` document get their text
    re-derived too.
    """

    labels = _Labels(connection)
    ids = list(dict.fromkeys(block_ids))
    for chunk in _chunks(ids):
        rows = connection.execute(
            f"""SELECT id, document_id, type, attrs_json, content_json, payload_json, text
                  FROM blocks WHERE id IN ({",".join("?" for _ in chunk)})""",
            chunk,
        ).fetchall()
        for row in rows:
            _refresh_row(connection, row, labels)
    # Every refreshed block counts, not only changed text: a restored block
    # keeps its text but its referrers read as broken until now.
    referrers = _referrers(connection, set(ids) | set(removed), set(retitled))
    referrers -= set(ids)
    if referrers:
        fresh = _Labels(connection)
        for chunk in _chunks(sorted(referrers)):
            for row in connection.execute(
                f"""SELECT id, document_id, type, attrs_json, content_json, payload_json, text
                      FROM blocks WHERE id IN ({",".join("?" for _ in chunk)})""",
                chunk,
            ).fetchall():
                block = _row_block(row)
                text = schema.derive_text(block, fresh)
                if text != row["text"]:
                    connection.execute("UPDATE blocks SET text = ? WHERE id = ?", (text, row["id"]))
                    _refresh_fts(connection, row["id"], row["document_id"], text)


def _chunks(items: list[str], size: int = 500):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _refresh_row(connection: sqlite3.Connection, row: sqlite3.Row, labels: _Labels) -> None:
    """Re-derive the text, refs, asset links and search row of one block."""

    block = _row_block(row)
    block_id = row["id"]
    text = schema.derive_text(block, labels)
    if text != row["text"]:
        connection.execute("UPDATE blocks SET text = ? WHERE id = ?", (text, block_id))
    documents, blocks = schema.references(block.content)
    blocks.discard(block_id)
    connection.execute("DELETE FROM block_refs WHERE source_block_id = ?", (block_id,))
    connection.execute("DELETE FROM document_refs WHERE source_block_id = ?", (block_id,))
    connection.executemany(
        "INSERT INTO block_refs(source_block_id, target_block_id) VALUES (?, ?)",
        [(block_id, target) for target in sorted(blocks)],
    )
    connection.executemany(
        "INSERT INTO document_refs(source_block_id, target_document_id) VALUES (?, ?)",
        [(block_id, target) for target in sorted(documents)],
    )
    connection.execute("DELETE FROM block_assets WHERE block_id = ?", (block_id,))
    for media_name in sorted(schema.media_names(block.attrs)):
        connection.execute(
            """INSERT OR IGNORE INTO block_assets(block_id, content_hash, media_name)
               SELECT ?, content_hash, media_name FROM assets WHERE media_name = ?""",
            (block_id, media_name),
        )
    _refresh_fts(connection, block_id, row["document_id"], text)


def _referrers(
    connection: sqlite3.Connection, block_ids: set[str], document_ids: set[str]
) -> set[str]:
    found: set[str] = set()
    for chunk in _chunks(sorted(block_ids)):
        found.update(
            row[0] for row in connection.execute(
                f"""SELECT source_block_id FROM block_refs
                     WHERE target_block_id IN ({",".join("?" for _ in chunk)})""",
                chunk,
            )
        )
    for chunk in _chunks(sorted(document_ids)):
        found.update(
            row[0] for row in connection.execute(
                f"""SELECT source_block_id FROM document_refs
                     WHERE target_document_id IN ({",".join("?" for _ in chunk)})""",
                chunk,
            )
        )
    return found


def rebuild_document_derived(connection: sqlite3.Connection, document_id: str) -> None:
    """Re-derive every block of one document (after a bulk change)."""

    ids = [row[0] for row in connection.execute(
        "SELECT id FROM blocks WHERE document_id = ?", (document_id,)
    )]
    # blocks_fts.block_id is UNINDEXED: one document-wide delete beats a scan per block.
    connection.execute("DELETE FROM blocks_fts WHERE document_id = ?", (document_id,))
    refresh_derived(connection, document_id, ids)


def document_retitled(connection: sqlite3.Connection, document_id: str) -> None:
    """A note's title changed: text that shows links to it follows."""

    refresh_derived(connection, document_id, [], retitled=[document_id])


class DocumentWrite:
    """One write transaction over documents (see :func:`document_write`)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._rederive: dict[str, None] = {}
        self._retitled: dict[str, None] = {}

    def rederive(self, document_id: str) -> None:
        """Rebuild every derived row of ``document_id``'s blocks before commit."""

        self._rederive[document_id] = None

    def retitled(self, document_id: str) -> None:
        """``document_id``'s title changed: text showing links to it follows."""

        self._retitled[document_id] = None

    def purge(self, document_ids: Iterable[str]) -> None:
        """Delete documents for good, with everything hanging off them."""

        for document_id in document_ids:
            self.connection.execute(
                "DELETE FROM blocks_fts WHERE document_id = ?", (document_id,)
            )
            # Children reference parents with ON DELETE CASCADE: blocks, refs,
            # history and receipts go with the document.
            self.connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._rederive.pop(document_id, None)

    def finish(self) -> None:
        for document_id in self._rederive:
            rebuild_document_derived(self.connection, document_id)
        for document_id in self._retitled:
            document_retitled(self.connection, document_id)


@contextmanager
def document_write(path: Path, immediate: bool = True):
    """A write transaction whose marked documents get their derived data rebuilt."""

    initialize(path)
    with connection_scope(path, immediate=immediate) as connection:
        write = DocumentWrite(connection)
        yield write
        write.finish()


# --- Reading -------------------------------------------------------------------------------------


def render_markdown(
    nodes: list[dict], depth: int = 0, document_provenance: dict | None = None,
    include_ids: bool = True, links=None,
) -> str:
    """A block tree as Markdown (see ``block_markdown.render_markdown``)."""

    return _render_markdown(
        nodes, document_provenance=document_provenance, include_ids=include_ids, links=links
    )


def document_tree(path: Path, document_id: str) -> dict | None:
    initialize(path)
    with connection_scope(path) as connection:
        return _document_tree(connection, document_id)


_BLOCK_COLUMNS = """id, document_id, parent_id, order_key, type, attrs_json, content_json,
                   payload_json, text, created_at, updated_at, provenance_json, revision"""


def _node(row: sqlite3.Row, user_attrs: dict, document_provenance: dict) -> dict:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "parent_id": row["parent_id"],
        "order": row["order_key"],
        "position": 0,
        "type": row["type"],
        "attrs": _loads(row["attrs_json"]) or {},
        "content": _loads(row["content_json"]),
        "payload": _loads(row["payload_json"]),
        "text": row["text"],
        "user_attrs": user_attrs,
        # Where this block came from; ``inherited`` when it is the document's.
        "provenance": _provenance.effective_block_provenance(
            row["provenance_json"], document_provenance
        ),
        # The document revision that last changed this block (null: never since
        # it was created or imported).
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "children": [],
    }


def _document_tree(
    connection: sqlite3.Connection, document_id: str, include_children: bool = True
) -> dict | None:
    """The document payload; without ``include_children`` only its metadata."""

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
    document_provenance = _provenance.document_provenance(
        document["provenance_json"], document["source_kind"]
    )
    roots: list[dict] = []
    if include_children:
        user_attrs_by_block: dict[str, dict[str, object]] = {}
        for row in connection.execute(
            """SELECT attrs.block_id, attrs.name, attrs.value
                 FROM block_user_attrs AS attrs
                 JOIN blocks ON blocks.id = attrs.block_id
                WHERE blocks.document_id = ?""",
            (document_id,),
        ):
            user_attrs_by_block.setdefault(row["block_id"], {})[row["name"]] = json.loads(row["value"])
        rows = connection.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM blocks WHERE document_id = ? ORDER BY order_key",
            (document_id,),
        ).fetchall()
        nodes = {
            row["id"]: _node(row, user_attrs_by_block.get(row["id"], {}), document_provenance)
            for row in rows
        }
        for node in nodes.values():
            parent = nodes.get(node["parent_id"])
            siblings = parent["children"] if parent else roots
            node["position"] = len(siblings)
            siblings.append(node)
    return {
        "id": document["id"],
        "title": document["title"],
        "status": document["status"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "revision": document["revision"],
        "title_auto": bool(document["title_auto"]),
        "can_undo": bool(history_state["can_undo"]),
        "can_redo": bool(history_state["can_redo"]),
        "provenance": document_provenance,
        # Current titles/text of every note and block this document links to.
        "resolved_links": _resolved_links(connection, document_id),
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


def _snapshot(connection: sqlite3.Connection, block_id: str) -> dict | None:
    """One block's stored state, as undo and redo put it back."""

    row = connection.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
    if row is None:
        return None
    state = {key: row[key] for key in _SNAPSHOT_COLUMNS}
    state["user_attrs"] = {
        item["name"]: item["value"]
        for item in connection.execute(
            "SELECT name, value FROM block_user_attrs WHERE block_id = ?", (block_id,)
        )
    }
    sync = connection.execute(
        "SELECT source_hash, dismissed_hash, conflict_json, source_removed FROM block_sync WHERE block_id = ?",
        (block_id,),
    ).fetchone()
    state["sync"] = None if sync is None else dict(sync)
    return state


_SNAPSHOT_COLUMNS = (
    "document_id", "parent_id", "order_key", "type", "attrs_json", "content_json",
    "payload_json", "text", "created_at", "updated_at", "provenance_json", "revision",
)


class ChangeRecorder:
    """The blocks one write touched, with their state before it (one undo step)."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.before: dict[str, dict | None] = {}

    def touch(self, block_id: str) -> None:
        if block_id not in self.before:
            self.before[block_id] = _snapshot(self.connection, block_id)

    def touch_subtree(self, block_id: str) -> None:
        for row in self.connection.execute(
            """WITH RECURSIVE subtree(id) AS (
                   SELECT ? UNION ALL
                   SELECT blocks.id FROM blocks JOIN subtree ON blocks.parent_id = subtree.id
               ) SELECT id FROM subtree""",
            (block_id,),
        ).fetchall():
            self.touch(row["id"])

    def patch(self) -> list[dict]:
        return [
            {"id": block_id, "before": before, "after": _snapshot(self.connection, block_id)}
            for block_id, before in self.before.items()
        ]

    def record(self, document_id: str, revision: int, now: str) -> None:
        """Store this write as the document's newest undo step (dropping redo)."""

        self.connection.execute(
            "DELETE FROM document_history WHERE document_id = ? AND undone = 1", (document_id,)
        )
        self.connection.execute(
            """INSERT INTO document_history(document_id, revision, patches_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (document_id, revision, json.dumps(self.patch()), now),
        )


def _restore(connection: sqlite3.Connection, document_id: str, entries: list[dict], key: str) -> None:
    """Put the blocks of one history step back into their ``key`` ("before"/"after") state.

    Touched blocks that exist are parked on unique temporary order keys first, so
    restoring them in any order never trips the sibling-order index; foreign keys
    are checked at commit (a parent may be restored after its child).
    """

    connection.execute("PRAGMA defer_foreign_keys = ON")
    present = {
        entry["id"] for entry in entries
        if connection.execute("SELECT 1 FROM blocks WHERE id = ?", (entry["id"],)).fetchone()
    }
    for block_id in present:
        connection.execute("UPDATE blocks SET order_key = '~' || id WHERE id = ?", (block_id,))
    columns = ", ".join(_SNAPSHOT_COLUMNS)
    for entry in entries:
        state = entry[key]
        if state is None:
            continue
        values = [state[column] for column in _SNAPSHOT_COLUMNS]
        if entry["id"] in present:
            assignments = ", ".join(f"{column} = ?" for column in _SNAPSHOT_COLUMNS)
            connection.execute(f"UPDATE blocks SET {assignments} WHERE id = ?", (*values, entry["id"]))
        else:
            connection.execute(
                f"INSERT INTO blocks(id, {columns}) VALUES (?, {', '.join('?' for _ in values)})",
                (entry["id"], *values),
            )
        connection.execute("DELETE FROM block_user_attrs WHERE block_id = ?", (entry["id"],))
        connection.executemany(
            "INSERT INTO block_user_attrs(block_id, name, value) VALUES (?, ?, ?)",
            [(entry["id"], name, value) for name, value in state["user_attrs"].items()],
        )
        write_block_sync(connection, entry["id"], state["sync"])
    # Search rows go first: deleting a parent cascades to its children.
    doomed = [entry["id"] for entry in entries if entry[key] is None and entry["id"] in present]
    _delete_fts(connection, doomed)
    for block_id in doomed:
        connection.execute("DELETE FROM blocks WHERE id = ?", (block_id,))


def write_block_sync(connection: sqlite3.Connection, block_id: str, sync: dict | None) -> None:
    if sync is None:
        connection.execute("DELETE FROM block_sync WHERE block_id = ?", (block_id,))
        return
    connection.execute(
        """INSERT INTO block_sync(block_id, source_hash, dismissed_hash, conflict_json, source_removed)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(block_id) DO UPDATE SET source_hash = excluded.source_hash,
               dismissed_hash = excluded.dismissed_hash, conflict_json = excluded.conflict_json,
               source_removed = excluded.source_removed""",
        (block_id, sync.get("source_hash"), sync.get("dismissed_hash"),
         sync.get("conflict_json"), int(bool(sync.get("source_removed")))),
    )


def _delete_fts(connection: sqlite3.Connection, block_ids: list[str]) -> None:
    for chunk in _chunks(block_ids):
        connection.execute(
            f"""DELETE FROM blocks_fts WHERE rowid IN (
                    SELECT rowid FROM blocks WHERE id IN ({",".join("?" for _ in chunk)}))""",
            chunk,
        )


def _request_hash(base_revision: int, operations: list[BlockOperation]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "base_revision": base_revision,
                "operations": [operation.model_dump(mode="json", exclude_unset=True)
                               for operation in operations],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def apply_transaction(
    path: Path,
    document_id: str,
    operations: list[BlockOperation],
    base_revision: int,
    transaction_id: str | None = None,
    validator_out: list | None = None,
    include_children: bool = True,
) -> dict:
    """Apply operations for ``base_revision`` atomically and return the new tree.

    Raises ``BlockShapeError`` (or ``ValueError``/``KeyError``) for operations
    the block model doesn't allow; nothing is written then. ``validator_out``
    (a list) receives the new tree's validator, read in the same transaction.
    Without ``include_children`` the result is the document's metadata only.
    """

    initialize(path)
    now = utc_now()
    with connection_scope(path) as connection:
        # Take the writer lock before reading the revision: two writers must not
        # both read the same revision before either writes.
        connection.execute("BEGIN IMMEDIATE")
        request_hash = _request_hash(base_revision, operations)
        if transaction_id is not None:
            receipt = connection.execute(
                "SELECT document_id, request_hash FROM transaction_receipts WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["document_id"] != document_id or receipt["request_hash"] != request_hash:
                    raise ValueError("transaction_id was already used for a different request")
                tree = _document_tree(connection, document_id, include_children)
                if tree is None:
                    raise KeyError("document not found")
                if validator_out is not None:
                    validator_out.append(_document_validator(connection, document_id))
                return tree
        document = connection.execute(
            "SELECT revision FROM documents WHERE id = ? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError("document not found")
        if base_revision != document["revision"]:
            raise DocumentRevisionConflict(document_id, base_revision, document["revision"])
        revision = base_revision + 1
        writer = _Writer(connection, document_id, now, revision)
        for operation in operations:
            writer.apply(operation)
        refresh_derived(
            connection, document_id, writer.content_changed, removed=writer.removed
        )
        connection.execute(
            "UPDATE documents SET updated_at = ?, revision = ? WHERE id = ?",
            (now, revision, document_id),
        )
        _refresh_auto_title(connection, document_id)
        writer.changes.record(document_id, revision, now)
        tree = _document_tree(connection, document_id, include_children)
        if tree is None:  # pragma: no cover - protected by the transaction above
            raise KeyError("document not found")
        if validator_out is not None:
            validator_out.append(_document_validator(connection, document_id))
        if transaction_id is not None:
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id, document_id, request_hash, created_at) VALUES (?, ?, ?, ?)",
                (transaction_id, document_id, request_hash, now),
            )
    return tree


class _Writer:
    """Applies one transaction's operations (docs/block-model.md §4)."""

    def __init__(self, connection: sqlite3.Connection, document_id: str, now: str, revision: int):
        self.connection = connection
        self.document_id = document_id
        self.now = now
        self.revision = revision
        self.changes = ChangeRecorder(connection)
        self.content_changed: list[str] = []
        self.removed: list[str] = []

    # -- helpers

    def row(self, block_id: str | None, what: str = "block") -> sqlite3.Row:
        if not block_id:
            raise ValueError(f"{what} id is required")
        row = self.connection.execute(
            "SELECT * FROM blocks WHERE id = ? AND document_id = ?", (block_id, self.document_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"{what} not found: {block_id}")
        return row

    def parent_type(self, parent_id: str | None) -> str | None:
        return None if parent_id is None else self.row(parent_id, "parent block")["type"]

    def sibling_keys(self, parent_id: str | None, moving: str | None) -> list[tuple[str, str]]:
        return [
            (row["id"], row["order_key"])
            for row in self.connection.execute(
                """SELECT id, order_key FROM blocks
                    WHERE document_id = ? AND parent_id IS ? AND id IS NOT ?
                    ORDER BY order_key""",
                (self.document_id, parent_id, moving),
            )
        ]

    def placement(self, operation: BlockOperation, moving: str | None = None):
        """``(parent_id, order key)`` for an insert or move."""

        given_parent = "parent_id" in operation.model_fields_set
        anchor_id = operation.after_id or operation.before_id
        if operation.after_id and operation.before_id:
            raise ValueError("give after_id or before_id, not both")
        if anchor_id:
            if anchor_id == moving:
                raise ValueError("a block can't be placed next to itself")
            anchor = self.row(anchor_id, "sibling block")
            parent_id = anchor["parent_id"]
            if given_parent and operation.parent_id != parent_id:
                raise ValueError("parent_id does not match the sibling's parent")
            siblings = self.sibling_keys(parent_id, moving)
            index = next(i for i, (block_id, _) in enumerate(siblings) if block_id == anchor_id)
            if operation.after_id:
                low = siblings[index][1]
                high = siblings[index + 1][1] if index + 1 < len(siblings) else None
            else:
                high = siblings[index][1]
                low = siblings[index - 1][1] if index > 0 else None
        else:
            parent_id = operation.parent_id
            if parent_id is not None:
                self.row(parent_id, "parent block")
            siblings = self.sibling_keys(parent_id, moving)
            low = siblings[-1][1] if siblings else None
            high = None
        return parent_id, order_keys.key_between(low, high)

    # -- operations

    def apply(self, operation: BlockOperation) -> None:
        if operation.operation == "insert":
            self.insert(operation)
        elif operation.operation == "update":
            self.update(operation)
        elif operation.operation == "move":
            self.move(operation)
        elif operation.operation == "delete":
            self.delete(operation)
        else:  # pragma: no cover - the model restricts operation names
            raise ValueError(f"unsupported operation: {operation.operation}")

    def insert(self, operation: BlockOperation) -> None:
        block = _operation_block(operation)
        block_id = operation.block_id or new_id()
        if self.connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError(f"block already exists: {block_id}")
        parent_id, key = self.placement(operation)
        schema.check_nesting(self.parent_type(parent_id), block.type)
        provenance = (
            _provenance.normalize(operation.provenance)
            if operation.provenance is not None else {"source": "manual"}
        )
        self.changes.touch(block_id)
        self.connection.execute(
            """INSERT INTO blocks(id, document_id, parent_id, order_key, type, attrs_json,
                   content_json, payload_json, text, created_at, updated_at, provenance_json,
                   revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
            (
                block_id, self.document_id, parent_id, key, block.type, _dumps(block.attrs),
                _dumps_nullable(block.content), _dumps_nullable(block.payload),
                self.now, self.now, _provenance.dumps(provenance), self.revision,
            ),
        )
        self.set_user_attrs(block_id, {}, operation.user_attrs)
        self.content_changed.append(block_id)

    def update(self, operation: BlockOperation) -> None:
        if not operation.model_fields_set & {"type", "attrs", "content", "payload", "user_attrs", "provenance"}:
            raise BlockShapeError("an update names what to change: type, attrs, content, payload, user_attrs or provenance")
        row = self.row(operation.block_id)
        self.changes.touch(row["id"])
        current = _row_block(row)
        new_type = current.type
        if operation.type is not None:
            new_type = operation.type
            schema.check_type_change(current.type, new_type)
            if new_type != current.type:
                schema.check_nesting(self.parent_type(row["parent_id"]), new_type)
                for child in self.connection.execute(
                    "SELECT type FROM blocks WHERE parent_id = ?", (row["id"],)
                ):
                    schema.check_nesting(new_type, child["type"])
        attrs = schema.retype_attrs(current.type, new_type, current.attrs)
        attrs = schema.normalize_attrs(new_type, schema.patch_attrs(attrs, operation.attrs))
        content, payload = current.content, current.payload
        if "content" in operation.model_fields_set:
            content = operation.content
        if "payload" in operation.model_fields_set:
            payload = operation.payload
        if new_type == "code" and content:
            content = schema.plain_content(content)
        content = schema.normalize_content(new_type, content)
        payload = schema.normalize_payload(new_type, payload)
        provenance_json = row["provenance_json"]
        if operation.provenance:
            supplied = _provenance.normalize(operation.provenance)
            if "source" not in operation.provenance:
                supplied.pop("source", None)
            provenance_json = _provenance.dumps({**_provenance.loads(provenance_json), **supplied})
        self.connection.execute(
            """UPDATE blocks SET type = ?, attrs_json = ?, content_json = ?, payload_json = ?,
                      provenance_json = ?, updated_at = ?, revision = ? WHERE id = ?""",
            (
                new_type, _dumps(attrs), _dumps_nullable(content), _dumps_nullable(payload),
                provenance_json, self.now, self.revision, row["id"],
            ),
        )
        if operation.user_attrs is not None:
            self.set_user_attrs(row["id"], None, operation.user_attrs)
        self.content_changed.append(row["id"])

    def move(self, operation: BlockOperation) -> None:
        row = self.row(operation.block_id)
        parent_id, key = self.placement(operation, moving=row["id"])
        if parent_id is not None and (parent_id == row["id"] or _is_descendant(self.connection, row["id"], parent_id)):
            raise ValueError("cannot move a block into itself or its descendant")
        schema.check_nesting(self.parent_type(parent_id), row["type"])
        self.changes.touch(row["id"])
        self.connection.execute(
            "UPDATE blocks SET parent_id = ?, order_key = ?, updated_at = ?, revision = ? WHERE id = ?",
            (parent_id, key, self.now, self.revision, row["id"]),
        )

    def delete(self, operation: BlockOperation) -> None:
        row = self.row(operation.block_id)
        self.changes.touch_subtree(row["id"])
        subtree = [
            item[0] for item in self.connection.execute(
                """WITH RECURSIVE subtree(id) AS (
                       SELECT ? UNION ALL
                       SELECT blocks.id FROM blocks JOIN subtree ON blocks.parent_id = subtree.id
                   ) SELECT id FROM subtree""",
                (row["id"],),
            )
        ]
        _delete_fts(self.connection, subtree)
        self.connection.execute("DELETE FROM blocks WHERE id = ?", (row["id"],))
        self.removed.extend(subtree)
        removed = set(subtree)
        self.content_changed = [block_id for block_id in self.content_changed if block_id not in removed]

    def set_user_attrs(self, block_id: str, current: dict | None, patch: dict | None) -> None:
        if patch is None:
            return
        if not isinstance(patch, dict):
            raise BlockShapeError("user_attrs must be an object")
        for name, value in patch.items():
            if value is None:
                self.connection.execute(
                    "DELETE FROM block_user_attrs WHERE block_id = ? AND name = ?", (block_id, name)
                )
            else:
                self.connection.execute(
                    """INSERT INTO block_user_attrs(block_id, name, value) VALUES (?, ?, ?)
                       ON CONFLICT(block_id, name) DO UPDATE SET value = excluded.value""",
                    (block_id, name, json.dumps(value)),
                )


def _operation_block(operation: BlockOperation) -> schema.Block:
    """The block an insert describes."""

    if not operation.type:
        raise BlockShapeError("insert requires a type")
    return schema.normalize_block(operation.type, operation.attrs, operation.content, operation.payload)


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


def _change_history(
    path: Path, document_id: str, base_revision: int, redo: bool
) -> dict:
    initialize(path)
    now = utc_now()
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
        history = connection.execute(
            """SELECT * FROM document_history WHERE document_id = ? AND undone = ?
               ORDER BY id """ + ("ASC" if redo else "DESC") + " LIMIT 1",
            (document_id, 1 if redo else 0),
        ).fetchone()
        if history is None:
            raise ValueError("nothing to redo" if redo else "nothing to undo")
        entries = json.loads(history["patches_json"])
        _restore(connection, document_id, entries, "after" if redo else "before")
        present = [
            entry["id"] for entry in entries if entry["after" if redo else "before"] is not None
        ]
        gone = [entry["id"] for entry in entries if entry["after" if redo else "before"] is None]
        refresh_derived(connection, document_id, present, removed=gone)
        connection.execute(
            "UPDATE document_history SET undone = ? WHERE id = ?",
            (0 if redo else 1, history["id"]),
        )
        connection.execute(
            "UPDATE documents SET updated_at = ?, revision = revision + 1 WHERE id = ?",
            (now, document_id),
        )
        _refresh_auto_title(connection, document_id)
    tree = document_tree(path, document_id)
    if tree is None:  # pragma: no cover - protected by the transaction above
        raise KeyError("document not found")
    return tree


def undo_transaction(path: Path, document_id: str, base_revision: int) -> dict:
    return _change_history(path, document_id, base_revision, redo=False)


def redo_transaction(path: Path, document_id: str, base_revision: int) -> dict:
    return _change_history(path, document_id, base_revision, redo=True)


def stored_document_provenance(connection: sqlite3.Connection, document_id: str) -> dict:
    row = connection.execute(
        "SELECT provenance_json, source_kind FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return {"source": _provenance.DEFAULT_SOURCE}
    return _provenance.document_provenance(row["provenance_json"], row["source_kind"])


def _refresh_fts(
    connection: sqlite3.Connection,
    block_id: str,
    document_id: str,
    text: str,
) -> None:
    # The same text the corpus gives embeddings, related notes and Ask.
    asset_text = connection.execute(
        f"SELECT {textutil.asset_text_sql('?')}", (block_id,)
    ).fetchone()[0]
    indexed_text = textutil.searchable_text(text, asset_text or "")
    _fts_insert(connection, block_id, document_id, indexed_text)


def _fts_insert(
    connection: sqlite3.Connection, block_id: str, document_id: str, text: str
) -> None:
    """Index a block's text in the FTS row that has the block's own rowid.

    ``blocks_fts.block_id`` is UNINDEXED: finding a block's row by it scans the
    whole table (milliseconds per lookup on a large workspace, on every edit).
    Keyed by the block's rowid, replacing or deleting it is one B-tree lookup.
    """

    connection.execute(
        """INSERT OR REPLACE INTO blocks_fts(rowid, block_id, document_id, text)
           SELECT rowid, ?, ?, ? FROM blocks WHERE id = ?""",
        (block_id, document_id, text, block_id),
    )


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
            # Document order: a block's path of order keys ('.' sorts below every key digit).
            """WITH RECURSIVE ordered(id, sort_key) AS (
                   SELECT id, order_key FROM blocks
                    WHERE document_id = ? AND parent_id IS NULL
                   UNION ALL
                   SELECT blocks.id, ordered.sort_key || '.' || blocks.order_key
                     FROM blocks JOIN ordered ON blocks.parent_id = ordered.id
               )
               SELECT blocks.text FROM ordered JOIN blocks ON blocks.id = ordered.id
                ORDER BY ordered.sort_key""",
            (document_id,),
        )
    )
    if title != row["title"]:
        connection.execute("UPDATE documents SET title = ? WHERE id = ?", (title, document_id))
        document_retitled(connection, document_id)


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
    """A title for new Markdown: the auto-title rule over the blocks it parses into."""

    texts: list[str] = []

    def collect(specs: list[BlockSpec]) -> None:
        for block_spec in specs:
            texts.append(schema.derive_text(block_spec.block()))
            collect(block_spec.children)

    collect(parse_markdown(body))
    return first_line_title(texts)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
