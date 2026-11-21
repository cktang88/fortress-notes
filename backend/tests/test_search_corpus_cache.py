import tempfile
import unittest
from pathlib import Path

from app import corpus
from app.block_store import (
    apply_transaction,
    connection_scope,
    create_document,
    create_folder,
    delete_document,
    document_tree,
    move_document,
    new_change_epoch,
    update_document_metadata,
)
from app.corpus import embedding_documents
from app.models import BlockOperation

from support import add_markdown_notes


class CorpusCacheTests(unittest.TestCase):
    def test_edits_show_up_even_when_another_note_is_dated_in_the_future(self) -> None:
        # An imported note from a machine with a skewed clock pins MAX(updated_at),
        # so a fingerprint built from timestamps alone never changes again.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            (notes / "future.md").write_text(
                "---\nid: future\ntitle: Future\nupdated_at: 2099-01-01T00:00:00Z\n---\n"
                "From the future\n",
                encoding="utf-8",
            )
            (notes / "today.md").write_text(
                "---\nid: today\ntitle: Today\n---\nOld words\n", encoding="utf-8"
            )
            database = root / ".fortress.sqlite3"
            add_markdown_notes(notes, database)
            self.assertIn("Old words", [text for *_rest, text in embedding_documents(database)])

            tree = document_tree(database, "today")
            [block] = tree["children"]
            apply_transaction(
                database,
                "today",
                [BlockOperation(operation="update", block_id=block["id"], content=[{"type": "text", "text": "New words"}])],
                tree["revision"],
            )

            texts = [text for *_rest, text in embedding_documents(database)]
            self.assertIn("New words", texts)
            self.assertNotIn("Old words", texts)


def _edit(database, document_id: str, text: str, index: int = 0) -> None:
    tree = document_tree(database, document_id)
    apply_transaction(
        database,
        document_id,
        [BlockOperation(operation="update", block_id=tree["children"][index]["id"],
                        content=[{"type": "text", "text": text}])],
        tree["revision"],
    )


class IncrementalCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database = Path(self._directory.name) / ".fortress.sqlite3"
        for name in ("a", "b", "c"):
            create_document(
                self.database, name, name.upper(), f"{name} one\n\n{name} two",
                "2025-01-01", "2025-01-01",
            )

    def _texts(self, corpus):
        return [row[3] for row in corpus]

    def test_unchanged_reads_return_the_same_snapshot(self) -> None:
        first = corpus.snapshot(self.database)
        self.assertIs(corpus.snapshot(self.database), first)
        # Changes that don't touch any paragraph keep it too (no cache misses downstream).
        folder = create_folder(self.database, "Folder")
        move_document(self.database, "a", folder_id=folder["id"])
        self.assertIs(corpus.snapshot(self.database), first)
        self.assertEqual(corpus.changed_between(first, first), frozenset())

    def test_an_edit_reloads_only_its_note(self) -> None:
        first = corpus.snapshot(self.database)
        _edit(self.database, "b", "b edited")
        second = corpus.snapshot(self.database)
        self.assertIsNot(second, first)
        self.assertEqual(self._texts(second), ["a one", "a two", "b edited", "b two", "c one", "c two"])
        self.assertEqual(corpus.changed_between(first, second), {"b"})
        # Unchanged notes share their entries (and anything derived from them).
        self.assertIs(second.notes["a"], first.notes["a"])
        self.assertIsNot(second.notes["b"], first.notes["b"])
        self.assertIs(second.notes["c"].titled(), first.notes["c"].titled())
        self.assertEqual(second.titled()[2], (second[2][0], "B", "b edited", "b\nb edited"))

    def test_title_changes_trash_and_hard_deletes_are_seen(self) -> None:
        first = corpus.snapshot(self.database)
        update_document_metadata(self.database, "a", title="Renamed")
        delete_document(self.database, "b")
        with connection_scope(self.database) as connection:
            connection.execute("DELETE FROM documents WHERE id = 'c'")
        latest = corpus.snapshot(self.database)
        self.assertEqual(self._texts(latest), ["a one", "a two"])
        self.assertEqual(latest.titled()[0][1], "Renamed")
        self.assertEqual(set(latest.notes), {"a"})
        self.assertEqual(corpus.changed_between(first, latest), {"a", "b", "c"})

    def test_a_restore_reloads_everything(self) -> None:
        first = corpus.snapshot(self.database)
        with connection_scope(self.database) as connection:
            connection.execute("UPDATE blocks SET text = 'restored' WHERE text = 'a one'")
        new_change_epoch(self.database)
        latest = corpus.snapshot(self.database)
        self.assertEqual(latest[0][3], "restored")
        self.assertIsNone(corpus.changed_between(first, latest))

    def test_matches_a_full_reload_after_many_edits(self) -> None:
        corpus.snapshot(self.database)
        for step in range(6):
            _edit(self.database, "abc"[step % 3], f"edit {step}", step % 2)
            create_document(
                self.database, f"n{step}", f"N{step}", f"new {step}", "2025-01-01", "2025-01-01"
            )
        incremental = list(corpus.snapshot(self.database))
        corpus.reset()
        self.assertEqual(incremental, list(corpus.snapshot(self.database)))


if __name__ == "__main__":
    unittest.main()
