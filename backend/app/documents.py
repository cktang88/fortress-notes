"""Note lifecycle over the canonical SQLite store, plus the Markdown mirrors.

SQLite is the only source of truth. Every write goes to SQLite first and then
refreshes ``NOTES_DIR/<id>.md``, a read-only mirror kept so notes stay readable
outside the app. Nothing reads the mirrors back except the explicit importer.
"""

from __future__ import annotations

import atexit
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import frontmatter

from . import block_store, duplicates, images, provenance, workspace
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
    write_mirror(tree["id"])
    return tree


def update(document_id: str, data: NoteUpdate) -> tuple[dict, tuple]:
    """Change title and/or status; the metadata payload and validator. KeyError if missing.

    The mirror is rewritten in the background (coalesced), not inline.
    """

    db_path, _ = _paths()
    result = block_store.update_document_metadata(
        db_path, document_id, title=data.title, status=data.status
    )
    schedule_mirror(document_id)
    return result


def trash(document_id: str) -> None:
    db_path, _ = _paths()
    block_store.delete_document(db_path, document_id)
    remove_mirror(document_id)


def restore(document_id: str) -> dict[str, object]:
    db_path, _ = _paths()
    result = workspace.restore_document(db_path, document_id)
    # A note emptied by move-into comes back with no blocks; give it one to type in.
    block_store.ensure_editable_block(db_path, document_id)
    write_mirror(document_id)
    return result


def purge(document_id: str) -> None:
    db_path, _ = _paths()
    workspace.purge_document(db_path, document_id)
    remove_mirror(document_id)


def empty_trash() -> int:
    db_path, _ = _paths()
    purged = workspace.empty_trash(db_path)
    for document_id in purged:
        remove_mirror(document_id)
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
    write_mirror(tree["id"])
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
    write_mirror(target_id)
    trash(source_id)
    return moved


# --- Living documents --------------------------------------------------------


def upsert_from_source(**kwargs):
    """Create or update the note for one source page, then refresh its mirror.

    Takes the keyword arguments of ``sources.upsert_document_from_source``
    (``source``, ``external_id``, ``title``, ``blocks``, ``folder_path``, ...).
    """

    from . import sources

    db_path, _ = _paths()
    result = sources.upsert_document_from_source(db_path, **kwargs)
    if result.status in {"created", "updated"}:
        write_mirror(result.document_id)
    return result


# --- Mirrors -----------------------------------------------------------------


def write_mirror(document_id: str) -> None:
    """Atomically rewrite one note's Markdown mirror from SQLite."""

    db_path, notes_path = _paths()
    mirror_document(db_path, notes_path, document_id)


# Mirrors of notes being edited are written in the background, coalesced: a
# burst of keystrokes rewrites the file once. (Rendering a 2,000-block note's
# Markdown took as long as the edit itself, on every transaction.)
MIRROR_DELAY_S = 0.5
_pending_mirrors: dict[tuple[Path, Path, str], float] = {}
_pending_changed = threading.Condition()
_mirror_worker: threading.Thread | None = None
_log = logging.getLogger("fortress")


def schedule_mirror(document_id: str) -> None:
    """Rewrite one note's mirror soon (coalesced); ``flush_mirrors`` forces it."""

    global _mirror_worker
    db_path, notes_path = _paths()
    with _pending_changed:
        _pending_mirrors.setdefault((db_path, notes_path, document_id), time.monotonic())
        if _mirror_worker is None or not _mirror_worker.is_alive():
            _mirror_worker = threading.Thread(
                target=_mirror_loop, name="fortress-mirrors", daemon=True
            )
            _mirror_worker.start()
        _pending_changed.notify()


def flush_mirrors() -> None:
    """Write every scheduled mirror now (shutdown, restore, tests)."""

    with _pending_changed:
        due = list(_pending_mirrors)
        _pending_mirrors.clear()
    _write_mirrors(due)


def _mirror_loop() -> None:
    while True:
        with _pending_changed:
            while not _pending_mirrors:
                _pending_changed.wait()
            oldest = min(_pending_mirrors.values())
            wait = oldest + MIRROR_DELAY_S - time.monotonic()
            if wait > 0:
                _pending_changed.wait(wait)
                continue
            due = list(_pending_mirrors)
            _pending_mirrors.clear()
        _write_mirrors(due)


def _write_mirrors(due: list[tuple[Path, Path, str]]) -> None:
    for db_path, notes_path, document_id in due:
        if not db_path.exists():
            continue  # the workspace went away (never recreate it from here)
        try:
            mirror_document(db_path, notes_path, document_id)
        except Exception:  # pragma: no cover - logged, the next edit retries
            _log.exception("mirror write failed for %s", document_id)


atexit.register(flush_mirrors)


# Read-then-write: without this, a writer that read an older tree could replace
# the mirror after a newer one was written, leaving it stale.
_mirror_lock = threading.Lock()


def mirror_document(db_path: Path, notes_path: Path, document_id: str) -> None:
    with _mirror_lock:
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
        images=images.collect_image_texts(body),
        provenance=provenance.front_matter(tree["provenance"]),
    )
    _atomic_write(target, frontmatter.dumps(frontmatter.Post(body, **metadata)))


def remove_mirror(document_id: str) -> None:
    _, notes_path = _paths()
    (notes_path / f"{document_id}.md").unlink(missing_ok=True)


def rebuild_mirrors(stash: Path) -> None:
    """Make every mirror match SQLite after a restore.

    Mirrors for documents SQLite no longer has are moved into ``stash`` rather
    than deleted, so nothing on disk is lost.
    """

    db_path, notes_path = _paths()
    active = {str(item["id"]) for item in block_store.list_documents(db_path)}
    for mirror in notes_path.glob("*.md"):
        if mirror.stem not in active:
            stash.mkdir(parents=True, exist_ok=True)
            mirror.replace(stash / mirror.name)
    for document_id in active:
        write_mirror(document_id)


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
