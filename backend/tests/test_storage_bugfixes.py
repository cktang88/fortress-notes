"""Regression tests for storage-core bugs (block moves, positions, trash, migrations)."""

import tempfile
import unittest
from pathlib import Path

from app.block_store import (
    apply_transaction,
    create_block_document,
    undo_transaction,
)
from app.models import BlockOperation


def _shape(nodes: list[dict]) -> list[tuple[str, int, list]]:
    return [(node["text"], node["position"], _shape(node["children"])) for node in nodes]


class BlockMoveTests(unittest.TestCase):
    def test_outdenting_a_middle_child_keeps_sibling_positions_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(
                database, "T", "rough", [], "- a\n  - c0\n  - c1\n  - c2\n- b\n"
            )
            c1 = created["children"][0]["children"][1]["id"]

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=c1, parent_id=None, position=1)],
                created["revision"],
            )

            self.assertEqual(
                _shape(moved["children"]),
                [
                    ("- a", 0, [("- c0", 0, []), ("- c2", 1, [])]),
                    ("- c1", 1, []),
                    ("- b", 2, []),
                ],
            )
            undone = undo_transaction(database, created["id"], moved["revision"])
            self.assertEqual(_shape(undone["children"]), _shape(created["children"]))

    def test_positions_past_the_end_append_without_leaving_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            created = create_block_document(database, "T", "rough", [], "one\n\ntwo\n")
            first = created["children"][0]["id"]

            inserted = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="insert", position=50, text="three")],
                created["revision"],
            )
            self.assertEqual([n["position"] for n in inserted["children"]], [0, 1, 2])

            moved = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="move", block_id=first, position=99)],
                inserted["revision"],
            )
            self.assertEqual(
                [(n["text"], n["position"]) for n in moved["children"]],
                [("two", 0), ("three", 1), ("one", 2)],
            )

            duplicated = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="duplicate", block_id=first, position=99)],
                moved["revision"],
            )
            self.assertEqual([n["position"] for n in duplicated["children"]], [0, 1, 2, 3])

            # Contiguous positions keep "merge with next sibling" working.
            merged = apply_transaction(
                database,
                created["id"],
                [BlockOperation(operation="merge", block_id=duplicated["children"][1]["id"])],
                duplicated["revision"],
            )
            self.assertEqual([n["text"] for n in merged["children"]], ["two", "threeone", "one"])


if __name__ == "__main__":
    unittest.main()
