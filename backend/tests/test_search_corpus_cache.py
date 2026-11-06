import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    apply_transaction,
    bootstrap_markdown,
    document_tree,
    embedding_documents,
)
from app.models import BlockOperation


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
            bootstrap_markdown(notes, database)
            self.assertIn("Old words", [text for *_rest, text in embedding_documents(database)])

            tree = document_tree(database, "today")
            [block] = tree["children"]
            apply_transaction(
                database,
                "today",
                [BlockOperation(operation="update", block_id=block["id"], text="New words")],
                tree["revision"],
            )

            texts = [text for *_rest, text in embedding_documents(database)]
            self.assertIn("New words", texts)
            self.assertNotIn("Old words", texts)


if __name__ == "__main__":
    unittest.main()
