"""Note lifecycle over the canonical SQLite store, plus the Markdown mirrors.

SQLite is the only source of truth. The notes folder is a one-way export of
every note as plain Markdown, laid out as an Obsidian vault (``vault.py``), kept so
notes stay readable outside the app; nothing reads the mirrors back, and each is
overwritten whenever its note changes. Writers never touch mirrors: the
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

from . import background, block_store, images, provenance, vault, workspace
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
    document_id: str | None = None,
    specs: list | None = None,
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
        data.body,
        folder_id=data.folder_id,
        created_at=created_at,
        # An unnamed note takes its title from its first line until someone names it.
        title_auto=not data.title,
        provenance=source,
        document_id=document_id,
        specs=specs if specs is not None else _linked_specs(db_path, data.body),
    )
    return tree


def _linked_specs(db_path: Path, body: str) -> list | None:
    """``body``'s blocks with its ``[[Note name]]`` wikilinks resolved to workspace notes
    (None: nothing to resolve, parse it as usual)."""

    if "[[" not in body:
        return None
    from .block_markdown import parse_markdown

    block_store.initialize(db_path)
    with block_store.read_scope(db_path) as connection:
        links = vault.ImportLinks(vault.workspace_names(connection))
    return parse_markdown(body, links)


def update(document_id: str, data: NoteUpdate) -> tuple[dict, tuple]:
    """Change the title; the metadata payload and validator. KeyError if missing."""

    db_path, _ = _paths()
    return block_store.update_document_metadata(
        db_path, document_id, title=data.title
    )


def trash(document_id: str) -> None:
    db_path, _ = _paths()
    block_store.delete_document(db_path, document_id)


def restore(document_id: str) -> dict[str, object]:
    db_path, _ = _paths()
    return workspace.restore_document(db_path, document_id)


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
        body,
        folder_id=folder_id,
        provenance=source,
        specs=_linked_specs(db_path, body),
    )
    block_store.move_document(db_path, tree["id"], folder_id, position=0)
    return {"document_id": tree["id"], "title": tree["title"], "folder": UNCATEGORIZED}


def _uncategorized_folder(db_path: Path) -> str:
    with _uncategorized_lock:
        for item in block_store.navigation(db_path)["items"]:
            if item.get("kind") == "folder" and item.get("name") == UNCATEGORIZED:
                return str(item["id"])
        return str(block_store.create_folder(db_path, UNCATEGORIZED, None, position=0)["id"])


# --- Re-imports ---------------------------------------------------------------


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
# NOTES_DIR/<this>: the change-log position (and vault format) the mirrors are
# written up to, so a restart only rewrites what changed meanwhile (and a crash
# loses nothing).
MIRROR_STATE = ".fortress-mirrors.json"
# NOTES_DIR/<this>: where each note's mirror is and which of its blocks carry
# ids, as last written (rewritten only when that changes).
MIRROR_FILES = ".fortress-mirror-files.json"
_log = logging.getLogger("fortress")


class _MirrorState:
    def __init__(self) -> None:
        self.position: tuple[str, int] | None = None
        self.format: int | None = None
        self.files: dict[str, str] = {}
        self.targets: dict[str, str] = {}
        self.anchors: dict[str, list[str]] = {}


class MirrorWriter(background.Service):
    """The only writer and remover of the vault mirrors (the ``mirrors`` lane).

    The notes folder is an Obsidian vault (``vault.py``): ``<folders>/<title>.md``.
    Each pass works out every note's path from the folder tree and titles,
    removes the mirrors of trashed notes and the old files of moved or renamed
    ones, and rewrites the mirror of every note whose ``change_seq`` moved,
    that moved, that links to a note whose link name changed, or whose
    referenced blocks changed. A database it has no position for (first run, a
    replaced database, an older vault format) gets every mirror rewritten.
    """

    name = "mirrors"
    priority = background.HIGH
    poll = MIRROR_DELAY_S

    def __init__(self) -> None:
        self._states: dict[Path, _MirrorState] = {}

    def removed(self, db_path: Path, document_ids: list[str]) -> None:
        """These notes are gone for good: their mirrors go on the next pass."""

        if document_ids:
            background.wake(self.name)

    def step(self) -> None:
        db_path, notes_path = _paths()
        self.sync(db_path, notes_path)
        return None

    def path(self, notes_path: Path, document_id: str) -> Path | None:
        """Where a note's mirror was last written."""

        relative = self._state(notes_path).files.get(document_id)
        return notes_path / relative if relative else None

    def sync(self, db_path: Path, notes_path: Path) -> int:
        """Bring every mirror up to date; returns how many were written."""

        if not db_path.exists():
            return 0  # the workspace went away (never recreate it from here)
        block_store.initialize(db_path)
        state = self._state(notes_path)
        with block_store.read_scope(db_path) as connection:
            epoch, seq = connection.execute(
                "SELECT epoch, seq FROM change_log WHERE id = 1"
            ).fetchone()
            current = state.format == vault.FORMAT_VERSION
            if current and state.position == (epoch, seq):
                return 0
            full = not current or state.position is None or state.position[0] != epoch
            rows = [dict(row) for row in connection.execute(
                """SELECT id, title, folder_id, created_at, change_seq
                     FROM documents WHERE deleted_at IS NULL"""
            )]
            folders = [dict(row) for row in connection.execute(
                "SELECT id, parent_id, name, position FROM folders"
            )]
            anchors = vault.referenced_blocks(connection)
        layout = self._layout(notes_path, state, rows, folders)
        changed = (
            set(layout.paths) if full
            else {row["id"] for row in rows if row["change_seq"] > state.position[1]}
        )
        gone = [document_id for document_id in state.files if document_id not in layout.paths]
        moved = {
            document_id for document_id, path in layout.paths.items()
            if state.files.get(document_id) != path
        }
        renamed = {
            document_id for document_id, target in layout.targets.items()
            if state.targets.get(document_id) != target
        }
        anchored = {
            document_id for document_id in layout.paths
            if sorted(anchors.get(document_id, ())) != state.anchors.get(document_id, [])
        }
        linking = _documents_linking_to(db_path, renamed | set(gone)) if (renamed or gone) else set()
        rewrite = (changed | moved | anchored | linking) & set(layout.paths)

        # First take away what is stale (so a name can pass from one note to another)...
        for document_id in gone:
            _remove(notes_path, state.files[document_id])
        for document_id in moved & set(state.files):
            _remove(notes_path, state.files[document_id])
        # ... then write.
        for document_id in sorted(rewrite):
            try:
                _mirror_document(db_path, notes_path, document_id, layout,
                                 anchors.get(document_id, ()))
            except Exception:  # logged; one bad note must not stop the others
                _log.exception("mirror write failed for %s", document_id)
        new_anchors = {key: sorted(value) for key, value in anchors.items() if key in layout.paths}
        files_changed = (state.files, state.targets, state.anchors) != (
            layout.paths, layout.targets, new_anchors
        )
        state.files, state.targets = dict(layout.paths), dict(layout.targets)
        state.anchors = new_anchors
        state.format = vault.FORMAT_VERSION
        self._save(notes_path, state, (epoch, seq), files_changed=files_changed)
        return len(rewrite)

    def rebuild(self, db_path: Path, notes_path: Path, stash: Path) -> None:
        """Make every mirror match SQLite (after a restore).

        Mirrors of notes SQLite no longer has are moved into ``stash`` rather
        than deleted, so nothing on disk is lost.
        """

        active = {str(item["id"]) for item in block_store.list_documents(db_path)}
        state = self._state(notes_path)
        stale = {path for document_id, path in state.files.items() if document_id not in active}
        for relative in sorted(stale):
            source = notes_path / relative
            if source.is_file():
                target = stash / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
                _prune(notes_path, source.parent)
        state.files = {key: value for key, value in state.files.items() if key in active}
        self._save(notes_path, state, None, files_changed=True)
        self.sync(db_path, notes_path)

    def write_one(self, db_path: Path, notes_path: Path, document_id: str) -> None:
        """Write (or remove) one note's mirror now."""

        state = self._state(notes_path)
        with block_store.read_scope(db_path) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT id, title, folder_id, created_at FROM documents WHERE deleted_at IS NULL"
            )]
            folders = [dict(row) for row in connection.execute(
                "SELECT id, parent_id, name, position FROM folders"
            )]
            anchors = vault.referenced_blocks(connection)
        layout = self._layout(notes_path, state, rows, folders)
        old = state.files.get(document_id)
        if old and old != layout.paths.get(document_id):
            _remove(notes_path, old)
        if document_id in layout.paths:
            _mirror_document(db_path, notes_path, document_id, layout,
                             anchors.get(document_id, ()))
            state.files[document_id] = layout.paths[document_id]
            state.targets[document_id] = layout.targets[document_id]
        else:
            state.files.pop(document_id, None)
        self._save(notes_path, state, state.position, files_changed=True)

    def _layout(self, notes_path: Path, state: _MirrorState, rows: list[dict],
                folders: list[dict]) -> vault.Layout:
        ours = {path.casefold() for path in state.files.values()}

        def taken(path: str, document_id: str) -> bool:
            # Someone else's file at that name is never overwritten.
            if state.files.get(document_id) == path or path.casefold() in ours:
                return False
            existing = notes_path / path
            if not existing.exists():
                return False
            try:
                found = frontmatter.loads(existing.read_text(encoding="utf-8")).metadata.get("id")
            except Exception:
                return True
            return str(found) != document_id

        return vault.compute_layout(rows, folders, taken)

    def _state(self, notes_path: Path) -> _MirrorState:
        if notes_path not in self._states:
            state = _MirrorState()
            try:
                saved = json.loads((notes_path / MIRROR_STATE).read_text(encoding="utf-8"))
                state.position = (str(saved["epoch"]), int(saved["seq"]))
                state.format = int(saved["format"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                files = json.loads((notes_path / MIRROR_FILES).read_text(encoding="utf-8"))
                state.files = {str(k): str(v) for k, v in dict(files.get("files") or {}).items()}
                state.targets = {str(k): str(v) for k, v in dict(files.get("targets") or {}).items()}
                state.anchors = {str(k): list(v) for k, v in dict(files.get("anchors") or {}).items()}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            self._states[notes_path] = state
        return self._states[notes_path]

    def _save(self, notes_path: Path, state: _MirrorState, position: tuple[str, int] | None,
              files_changed: bool) -> None:
        state.position = position
        if files_changed:
            _atomic_write(notes_path / MIRROR_FILES, json.dumps(
                {"files": state.files, "targets": state.targets, "anchors": state.anchors},
                ensure_ascii=False, sort_keys=True,
            ))
        target = notes_path / MIRROR_STATE
        if position is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, json.dumps(
                {"epoch": position[0], "seq": position[1], "format": state.format}
            ))


def _documents_linking_to(db_path: Path, document_ids: set[str]) -> set[str]:
    """Notes with a link to one of ``document_ids`` or to a block in one of them."""

    found: set[str] = set()
    ids = sorted(document_ids)
    with block_store.read_scope(db_path) as connection:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"""SELECT source.document_id FROM document_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                     WHERE refs.target_document_id IN ({marks})
                    UNION
                    SELECT source.document_id FROM block_refs AS refs
                      JOIN blocks AS source ON source.id = refs.source_block_id
                      JOIN blocks AS target ON target.id = refs.target_block_id
                     WHERE target.document_id IN ({marks})""",
                chunk + chunk,
            ):
                found.add(row[0])
    return found


def _remove(notes_path: Path, relative: str) -> None:
    path = notes_path / relative
    path.unlink(missing_ok=True)
    _prune(notes_path, path.parent)


def _prune(notes_path: Path, directory: Path) -> None:
    """Remove folders a move or trash left empty (up to the notes folder)."""

    root = notes_path.resolve()
    current = directory
    while current.resolve() != root and root in current.resolve().parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


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
        mirrors.write_one(db_path, notes_path, document_id)


def mirror_path(document_id: str, notes_path: Path | None = None) -> Path | None:
    """Where a note's mirror is (as last written)."""

    return mirrors.path(notes_path or _paths()[1], document_id)


def _mirror_document(db_path: Path, notes_path: Path, document_id: str, layout: vault.Layout,
                     anchors) -> None:
    tree = block_store.document_tree(db_path, document_id)
    if tree is None or document_id not in layout.paths:
        return
    resolved = (tree.get("resolved_links") or {}).get("blocks") or {}
    body = block_store.render_markdown(
        tree["children"],
        links=layout.render_links(document_id, anchors, resolved.get),
    )
    relative = layout.paths[document_id]
    target = notes_path / relative
    # Always overwritten: the mirror is an export, never edited in place.
    metadata = vault.front_matter(
        tree, Path(relative).stem, images.collect_image_texts(db_path, body)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, vault.dump(metadata, body))


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
