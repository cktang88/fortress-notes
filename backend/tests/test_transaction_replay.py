import tempfile
import unittest
from pathlib import Path

from app.block_store import apply_transaction, create_block_document
from app.models import BlockOperation


class TransactionReplayTests(unittest.TestCase):
    """The editor resends a save whose answer it never saw (the page closed with the
    request on its way); the same transaction ID must not apply twice."""

    def test_resending_a_saved_transaction_returns_the_note_without_reapplying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = create_block_document(database, "Replay", "rough", [], "First line")
            block = document["children"][0]
            operations = [
                BlockOperation(
                    operation="update",
                    block_id=block["id"],
                    text="First line, then more",
                    expected_updated_at=block["updated_at"],
                ),
                BlockOperation(operation="insert", block_id="01JABCDEFGHJKMNPQRSTVWXYZ0", text="Added"),
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
            document = create_block_document(database, "Replay", "rough", [], "Line")
            block_id = document["children"][0]["id"]
            apply_transaction(
                database,
                document["id"],
                [BlockOperation(operation="update", block_id=block_id, text="One")],
                document["revision"],
                "tx-2",
            )
            with self.assertRaises(ValueError):
                apply_transaction(
                    database,
                    document["id"],
                    [BlockOperation(operation="update", block_id=block_id, text="Two")],
                    document["revision"],
                    "tx-2",
                )


if __name__ == "__main__":
    unittest.main()
