"""Note lifecycle over the canonical SQLite store, plus the Markdown mirrors.

SQLite is the only source of truth. ``NOTES_DIR/<id>.md`` is a read-only mirror
of each note, kept so notes stay readable outside the app; nothing reads the
mirrors back except the explicit importer. Writers never touch mirrors: the
background :class:`MirrorWriter` is their only writer and remover, and it
follows SQLite's change log, so every write (here, transactions, imports,
restores) is mirrored without its writer asking.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import frontmatter

from . import background, block_store, duplicates, images, provenance, workspace
from .config import get_settings
from .models import NoteCreate, NoteSummary, NoteUpdate


def _paths() -> tuple[Path, Path]:
    settings = get_settings()
    return settings.block_db_path, settings.notes_path


# --- Reads -------------------------------------------------------------------


def summaries(ids: list[str]) -> dict[str, NoteSummary]:
    """Summaries of the active notes among ``ids``."""

    db_path, _ = _paths()
    return {
        str(item["id"]): NoteSummary(**{key: item[key] for key in NoteSummary.model_fields})
        for item in block_store.list_documents(db_path, list(ids))
    }


# --- Writes ------------------------------------------------------------------


def create(
    data: NoteCreate,
    created_at: object | None = None,
    source: dict[str, object] | None = None,
) -> dict:
    """Create a note and return its tree; raises KeyError when the folder does not exist.

    ``source`` is server-side provenance (an import); otherwise the client's
    ``data.provenance`` is validated (ValueError when unusable), defaulting to
    a note written by hand.
    """

    db_path, _ = _paths()
    if source is None:
        source = provenance.normalize(data.provenance) or {"source": "manual"}
    tree = block_store.create_block_document(
        db_path,
        data.title or block_store.derive_title(data.body),
        data.status,
        data.body,
        folder_id=data.folder_id,
        created_at=created_at,
        # An unnamed note takes its title from its first line until someone names it.
        title_auto=not data.title,
        provenance=source,
    )
    return tree


def update(document_id: str, data: NoteUpdate) -> tuple[dict, tuple]:
    """Change title and/or status; the metadata payload and validator. KeyError if missing."""

    db_path, _ = _paths()
    return block_store.update_document_metadata(
        db_path, document_id, title=data.title, status=data.status
    )


def trash(document_id: str) -> None:
    db_path, _ = _paths()
    block_store.delete_document(db_path, document_id)


def restore(document_id: str) -> dict[str, object]:
    db_path, _ = _paths()
    result = workspace.restore_document(db_path, document_id)
    # A note emptied by move-into comes back with no blocks; give it one to type in.
    block_store.ensure_editable_block(db_path, document_id)
    return result


def purge(document_id: str) -> None:
    db_path, _ = _paths()
    workspace.purge_document(db_path, document_id)
    mirrors.removed(db_path, [document_id])


def empty_trash() -> int:
    db_path, _ = _paths()
    purged = workspace.empty_trash(db_path)
    mirrors.removed(db_path, purged)
    return len(purged)


# --- Quick capture -----------------------------------------------------------

UNCATEGORIZED = "Uncategorized"
# Find-or-create must not run twice at once, or simultaneous first captures
# would each create their own Uncategorized folder.
_uncategorized_lock = threading.Lock()


def capture(text: str, extra_provenance: dict | None = None) -> dict[str, object]:
    """Save a quick thought as its own note in the Uncategorized folder.

    The note is titled from its first line and placed at the top of the folder
    (newest first), so captures can be filed later by dragging them elsewhere.
    The folder is created on first use and found again by its name.
    """

    body = text.strip()
    if not body:
        raise ValueError("nothing to capture")
    source = {**provenance.normalize(extra_provenance), "source": "capture"}
    db_path, _ = _paths()
    folder_id = _uncategorized_folder(db_path)
    tree = block_store.create_block_document(
        db_path,
        block_store.derive_title(body)[:80],
        "rough",
        body,
        folder_id=folder_id,
        provenance=source,
    )
    block_store.move_document(db_path, tree["id"], folder_id, position=0)
    return {"document_id": tree["id"], "title": tree["title"], "folder": UNCATEGORIZED}


def _uncategorized_folder(db_path: Path) -> str:
    with _uncategorized_lock:
        for item in block_store.navigation(db_path)["items"]:
            if item.get("kind") == "folder" and item.get("name") == UNCATEGORIZED:
                return str(item["id"])
        return str(block_store.create_folder(db_path, UNCATEGORIZED, None, position=0)["id"])


def move_into(source_id: str, target_id: str) -> int:
    """Fold one note into another (keeping block IDs) and trash the emptied note."""

    db_path, _ = _paths()
    moved = duplicates.move_into(db_path, source_id, target_id)
    trash(source_id)
    return moved


# --- Living documents --------------------------------------------------------


def upsert_from_source(**kwargs):
    """Create or update the note for one source page.

    Takes the keyword arguments of ``sources.upsert_document_from_source``
    (``source``, ``external_id``, ``title``, ``blocks``, ``folder_path``, ...).
    """

    from . import sources

    db_path, _ = _paths()
    return sources.upsert_document_from_source(db_path, **kwargs)


# --- Mirrors -----------------------------------------------------------------

# How often the writer looks for changes: a burst of keystrokes rewrites the
# file about once per interval. (Rendering a 2,000-block note's Markdown takes
# as long as the edit itself.)
MIRROR_DELAY_S = 0.5
# NOTES_DIR/<this>: the change-log position the mirrors are written up to, so a
# restart only rewrites what changed meanwhile (and a crash loses nothing).
MIRROR_STATE = ".fortress-mirrors.json"
_log = logging.getLogger("fortress")


class MirrorWriter(background.Service):
    """The only writer and remover of ``NOTES_DIR/<id>.md`` (the ``mirrors`` lane).

    Each pass rewrites the mirror of every note whose ``change_seq`` moved
    since the last pass, and removes those of trashed notes. Notes purged
    before a pass saw them trashed are handed over with :meth:`removed`. A
    database it has no position for (first run, a replaced database) gets
    every mirror rewritten once.
    """

    name = "mirrors"
    priority = background.HIGH
    poll = MIRROR_DELAY_S

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._removed: dict[Path, set[str]] = {}
        self._positions: dict[Path, tuple[str, int] | None] = {}

    def removed(self, db_path: Path, document_ids: list[str]) -> None:
        """These notes are gone for good: remove their mirrors on the next pass."""

        if document_ids:
            with self._lock:
                self._removed.setdefault(db_path, set()).update(document_ids)
            background.wake(self.name)

    def step(self) -> None:
        db_path, notes_path = _paths()
        with self._lock:
            removals = self._removed
            self._removed = {}
        for removed_db, ids in removals.items():
            for document_id in sorted(ids):
                (removed_db.parent / f"{document_id}.md").unlink(missing_ok=True)
        self.sync(db_path, notes_path)
        return None

    def sync(self, db_path: Path, notes_path: Path) -> int:
        """Mirror every note changed since the last pass; returns how many."""

        if not db_path.exists():
            return 0  # the workspace went away (never recreate it from here)
        block_store.initialize(db_path)
        position = self._position(notes_path)
        with block_store.read_scope(db_path) as connection:
            epoch, seq = connection.execute(
                "SELECT epoch, seq FROM change_log WHERE id = 1"
            ).fetchone()
            if position == (epoch, seq):
                return 0
            if position is None or position[0] != epoch:
                rows = connection.execute("SELECT id FROM documents").fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM documents WHERE change_seq > ?", (position[1],)
                ).fetchall()
        for row in rows:
            try:
                _mirror_document(db_path, notes_path, row[0])
            except Exception:  # logged; one bad note must not stop the others
                _log.exception("mirror write failed for %s", row[0])
        self._save_position(notes_path, (epoch, seq))
        return len(rows)

    def rebuild(self, db_path: Path, notes_path: Path, stash: Path) -> None:
        """Make every mirror match SQLite (after a restore).

        Mirrors of notes SQLite no longer has are moved into ``stash`` rather
        than deleted, so nothing on disk is lost.
        """

        active = {str(item["id"]) for item in block_store.list_documents(db_path)}
        for mirror in notes_path.glob("*.md"):
            if mirror.stem not in active:
                stash.mkdir(parents=True, exist_ok=True)
                mirror.replace(stash / mirror.name)
        self._save_position(notes_path, None)
        self.sync(db_path, notes_path)

    def _position(self, notes_path: Path) -> tuple[str, int] | None:
        if notes_path not in self._positions:
            try:
                saved = json.loads((notes_path / MIRROR_STATE).read_text(encoding="utf-8"))
                self._positions[notes_path] = (str(saved["epoch"]), int(saved["seq"]))
            except (OSError, ValueError, KeyError, TypeError):
                self._positions[notes_path] = None
        return self._positions[notes_path]

    def _save_position(self, notes_path: Path, position: tuple[str, int] | None) -> None:
        self._positions[notes_path] = position
        target = notes_path / MIRROR_STATE
        if position is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, json.dumps({"epoch": position[0], "seq": position[1]}))


mirrors = background.register(MirrorWriter())


def flush_mirrors() -> None:
    """Bring every mirror up to date now (restore, shutdown, tests, scripts)."""

    background.run_inline(MirrorWriter.name)


def rebuild_mirrors(stash: Path) -> None:
    """Rewrite every mirror, stashing those of notes SQLite no longer has (a restore)."""

    db_path, notes_path = _paths()
    with background.exclusive(MirrorWriter.name):
        mirrors.rebuild(db_path, notes_path, stash)


def mirror_document(db_path: Path, notes_path: Path, document_id: str) -> None:
    """Write (or remove) one note's mirror now, serialized with the writer."""

    with background.exclusive(MirrorWriter.name):
        _mirror_document(db_path, notes_path, document_id)


def _mirror_document(db_path: Path, notes_path: Path, document_id: str) -> None:
    tree = block_store.document_tree(db_path, document_id)
    target = notes_path / f"{document_id}.md"
    if tree is None:
        target.unlink(missing_ok=True)
        return
    body = block_store.render_markdown(
        tree["children"], document_provenance=tree["provenance"]
    )
    # Keep any extra front matter a person added to the file by hand.
    metadata: dict[str, object] = {}
    if target.is_file():
        try:
            metadata = dict(frontmatter.loads(target.read_text(encoding="utf-8")).metadata)
        except Exception:  # A damaged mirror is simply rewritten.
            metadata = {}
    metadata.pop("tags", None)  # retired; dropped from older mirrors too
    metadata.update(
        id=tree["id"],
        title=tree["title"],
        status=tree["status"],
        created_at=tree["created_at"],
        updated_at=tree["updated_at"],
        images=images.collect_image_texts(db_path, body),
        provenance=provenance.front_matter(tree["provenance"]),
    )
    _atomic_write(target, frontmatter.dumps(frontmatter.Post(body, **metadata)))


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
