"""The search corpus: every active paragraph's searchable text, kept current by deltas.

Search, related notes, Ask's grep and the embedding indexer all read the same
list of ``(block_id, document_id, version, text)`` rows. It used to be rebuilt
from scratch whenever anything changed (a global fingerprint over every block,
then a full reload: ~0.3 s at 100k paragraphs, on the first request after
every keystroke). Now:

* ``change_log.seq`` / ``documents.change_seq`` (maintained by triggers in
  ``block_store``) say which notes changed since the last snapshot; only their
  rows are reloaded. Hard-deleted notes are noticed by the active-note count, a
  restored backup by the change epoch, and new image text (OCR, captions),
  which is not a block write, by ``assets.text_updated_at``.
* A snapshot (``Corpus``) is immutable and is a plain list of rows, so callers
  can iterate it and use ``is`` to tell whether anything changed. Unchanged
  notes share their ``Note`` entries across snapshots, so data derived per note
  (Ask's lower-cased "title + text" rows, cached related lists) survives edits
  to other notes. ``changed_between`` names the notes that differ between two
  snapshots, for consumers that update incrementally.

``searchable_text`` is the one place that decides what text of a block is
indexed (its text plus the text found in its images).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from . import block_store

Row = tuple[str, str, str, str]  # (block_id, document_id, version, text)

# Snapshots remember which notes changed for this many generations back.
_HISTORY = 512


def searchable_text(text: str, asset_texts: Iterable[str] = ()) -> str:
    """What search, related notes, Ask and the embeddings see of one block.

    ``text`` is the block's own plain text; ``asset_texts`` the text extracted
    from images and files it shows (OCR, captions), in a stable order.
    """

    return "\n\n".join(part for part in (text, "\n".join(t for t in asset_texts if t.strip())) if part)


@dataclass(eq=False)
class Note:
    """One active note's rows; shared by every snapshot in which it is unchanged."""

    document_id: str
    title: str
    rows: tuple[Row, ...]
    _titled: list | None = field(default=None, repr=False)

    def titled(self) -> list[tuple[str, str, str, str]]:
        """(block_id, title, text, lower-cased "title\\ntext") per row (built once)."""

        if self._titled is None:
            self._titled = [
                (block_id, self.title, text, f"{self.title}\n{text}".lower())
                for block_id, _document_id, _version, text in self.rows
            ]
        return self._titled


class Corpus(list):
    """An immutable snapshot: the rows in note order, plus the notes they came from."""

    def __init__(self, path: Path, generation: int, notes: dict[str, Note]) -> None:
        self.path = path
        self.generation = generation
        self.notes = notes
        super().__init__(chain.from_iterable(notes[key].rows for key in sorted(notes)))
        self._titled: list | None = None

    def titled(self) -> list[tuple[str, str, str, str]]:
        """Every row as (block_id, note title, text, lower-cased "title\\ntext")."""

        if self._titled is None:
            self._titled = list(
                chain.from_iterable(self.notes[key].titled() for key in sorted(self.notes))
            )
        return self._titled


@dataclass
class _State:
    corpus: Corpus | None = None
    epoch: str = ""
    seq: int = -1
    asset_mark: str = ""
    active: int = 0
    # (generation, ids of the notes that changed in it); None = everything changed.
    history: list[tuple[int, frozenset[str] | None]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


_states: dict[Path, _State] = {}
_states_lock = threading.Lock()
_generation = 0  # global, so snapshots of different databases never compare equal


def _state(path: Path) -> _State:
    key = path.resolve()
    with _states_lock:
        state = _states.get(key)
        if state is None:
            state = _states[key] = _State()
        return state


def snapshot(path: Path) -> Corpus:
    """The current corpus of ``path``: the previous snapshot if nothing changed.

    A few indexed single-row lookups when nothing changed; otherwise only the
    changed notes are read again.
    """

    global _generation
    block_store.initialize(path)
    state = _state(path)
    with state.lock:
        with block_store.connection_scope(path) as connection:
            epoch, seq = connection.execute(
                "SELECT epoch, seq FROM change_log WHERE id = 1"
            ).fetchone()
            asset_mark = connection.execute(
                "SELECT COALESCE(MAX(text_updated_at), '') FROM assets"
            ).fetchone()[0]
            current = state.corpus
            if (
                current is not None
                and epoch == state.epoch
                and seq == state.seq
                and asset_mark == state.asset_mark
            ):
                return current
            if current is None or epoch != state.epoch or seq < state.seq:
                notes = _load_notes(connection, None)
                changed: frozenset[str] | None = None
            else:
                notes, changed = _apply_delta(connection, state, current.notes, asset_mark)
        state.epoch, state.seq, state.asset_mark = epoch, seq, asset_mark
        state.active = len(notes)
        if current is not None and changed is not None and not changed:
            return current  # e.g. a folder rename: no paragraph changed
        _generation += 1
        corpus = Corpus(path, _generation, notes)
        state.corpus = corpus
        state.history.append((_generation, changed))
        del state.history[:-_HISTORY]
        return corpus


def embedding_documents(path: Path) -> list[Row]:
    """Active blocks as (block_id, document_id, version, text), current as of now."""

    return snapshot(path)


def changed_between(old: Corpus, new: Corpus) -> frozenset[str] | None:
    """Ids of the notes whose rows differ between two snapshots of one database.

    None when that is no longer known (too old, a restore, another database):
    treat everything as changed.
    """

    if old is new:
        return frozenset()
    if old.path != new.path or old.generation > new.generation:
        return None
    state = _state(new.path)
    with state.lock:
        history = list(state.history)
    if not history or history[0][0] > old.generation + 1:
        return None
    out: set[str] = set()
    for generation, ids in history:
        if generation <= old.generation:
            continue
        if generation > new.generation:
            break
        if ids is None:
            return None
        out.update(ids)
    return frozenset(out)


def _apply_delta(connection, state: _State, notes: dict[str, Note], asset_mark: str):
    rows = connection.execute(
        "SELECT id, deleted_at IS NULL AS active FROM documents WHERE change_seq > ?",
        (state.seq,),
    ).fetchall()
    reload = {str(row[0]) for row in rows if row[1]}
    gone = {str(row[0]) for row in rows if not row[1]}
    if asset_mark != state.asset_mark:
        # New image text is not a block write: reload the notes showing those images.
        reload.update(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT blocks.document_id
                     FROM assets
                     JOIN block_assets USING (content_hash, media_name)
                     JOIN blocks ON blocks.id = block_assets.block_id
                     JOIN documents ON documents.id = blocks.document_id
                    WHERE assets.text_updated_at > ? AND documents.deleted_at IS NULL""",
                (state.asset_mark,),
            )
        )
    updated = dict(notes)
    for document_id in gone:
        updated.pop(document_id, None)
    fresh = _load_notes(connection, reload) if reload else {}
    for document_id in reload:
        note = fresh.get(document_id)
        if note is None:
            updated.pop(document_id, None)
        else:
            updated[document_id] = note
    active = connection.execute(
        "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"
    ).fetchone()[0]
    if active != len(updated):
        # A note was removed outright (no row left to carry a change_seq).
        ids = {str(row[0]) for row in connection.execute(
            "SELECT id FROM documents WHERE deleted_at IS NULL"
        )}
        for document_id in [key for key in updated if key not in ids]:
            updated.pop(document_id)
            gone.add(document_id)
    changed = set(gone)
    for document_id in reload:
        old, new = notes.get(document_id), updated.get(document_id)
        if old is None or new is None or old.title != new.title or old.rows != new.rows:
            changed.add(document_id)
        else:
            updated[document_id] = old  # unchanged: keep the shared entry
    return updated, frozenset(changed)


def _load_notes(connection, document_ids: set[str] | None) -> dict[str, Note]:
    """Notes (all active ones, or just ``document_ids`` if active) with their rows."""

    where, params = "", []
    if document_ids is not None:
        where = f" AND documents.id IN ({','.join('?' * len(document_ids))})"
        params = sorted(document_ids)
    notes = {
        str(row[0]): Note(str(row[0]), str(row[1] or ""), ())
        for row in connection.execute(
            f"SELECT id, title FROM documents WHERE deleted_at IS NULL{where}", params
        )
    }
    by_note: dict[str, list[Row]] = {}
    for row in connection.execute(
        f"""SELECT blocks.id, blocks.document_id,
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
                   ), '') AS version,
                   blocks.text,
                   (SELECT group_concat(assets.text, char(10))
                      FROM block_assets
                      JOIN assets USING (content_hash, media_name)
                     WHERE block_assets.block_id = blocks.id AND trim(assets.text) <> '')
                       AS asset_text
              FROM blocks
              JOIN documents ON documents.id = blocks.document_id
             WHERE documents.deleted_at IS NULL{where}
               AND (trim(blocks.text) <> '' OR EXISTS (
                   SELECT 1 FROM block_assets
                   JOIN assets USING (content_hash, media_name)
                   WHERE block_assets.block_id = blocks.id AND trim(assets.text) <> ''
               ))
             ORDER BY blocks.document_id, blocks.order_key, blocks.id""",
        params,
    ):
        text = searchable_text(str(row[3]), [row[4]] if row[4] else ())
        by_note.setdefault(str(row[1]), []).append((str(row[0]), str(row[1]), str(row[2]), text))
    for document_id, rows in by_note.items():
        note = notes.get(document_id)
        if note is not None:
            note.rows = tuple(rows)
    return notes


def reset() -> None:
    """Forget every snapshot (tests, or after replacing a database file)."""

    with _states_lock:
        _states.clear()
