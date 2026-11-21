import tempfile
import unittest
from pathlib import Path

from app import block_store
from app.block_store import apply_transaction, connection_scope, create_block_document
from app.models import BlockOperation


class TransactionReplayTests(unittest.TestCase):
    """The editor resends a save whose answer it never saw (the page closed with the
    request on its way); the same transaction ID must not apply twice."""

    def test_resending_a_saved_transaction_returns_the_note_without_reapplying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = create_block_document(database, "Replay", "First line")
            block = document["children"][0]
            operations = [
                BlockOperation(
                    operation="update",
                    block_id=block["id"],
                    content=[{"type": "text", "text": "First line, then more"}],
                ),
                BlockOperation(
                    operation="insert", block_id="01JABCDEFGHJKMNPQRSTVWXYZ0", type="paragraph",
                    content=[{"type": "text", "text": "Added"}],
                ),
            ]
            first = apply_transaction(
                database, document["id"], operations, document["revision"], "tx-1"
            )
            again = apply_transaction(
                database, document["id"], operations, document["revision"], "tx-1"
            )

            self.assertEqual(again["revision"], first["revision"])
            self.assertEqual(
                [child["text"] for child in again["children"]],
                ["First line, then more", "Added"],
            )

    def test_a_reused_transaction_id_with_different_edits_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = create_block_document(database, "Replay", "Line")
            block_id = document["children"][0]["id"]
            apply_transaction(
                database,
                document["id"],
                [BlockOperation(operation="update", block_id=block_id,
                                content=[{"type": "text", "text": "One"}])],
                document["revision"],
                "tx-2",
            )
            with self.assertRaises(ValueError):
                apply_transaction(
                    database,
                    document["id"],
                    [BlockOperation(operation="update", block_id=block_id,
                                    content=[{"type": "text", "text": "Two"}])],
                    document["revision"],
                    "tx-2",
                )

    def test_receipts_older_than_a_day_are_dropped_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = create_block_document(database, "Replay", "First line")
            for revision, transaction_id in enumerate(("old", "new")):
                apply_transaction(database, document["id"], [BlockOperation(
                    operation="insert", type="paragraph",
                    content=[{"type": "text", "text": transaction_id}],
                )], revision, transaction_id)
            with connection_scope(database) as connection:
                connection.execute(
                    "UPDATE transaction_receipts SET created_at = '2020-01-01T00:00:00+00:00'"
                    " WHERE transaction_id = 'old'"
                )
            block_store.initialize(database, force=True)
            with connection_scope(database) as connection:
                kept = [row[0] for row in connection.execute(
                    "SELECT transaction_id FROM transaction_receipts"
                )]
            self.assertEqual(kept, ["new"])


if __name__ == "__main__":
    unittest.main()
