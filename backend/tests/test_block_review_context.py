import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class BlockReviewContextApiTests(unittest.TestCase):
    def test_context_modes_resolve_targets_and_validate_document_membership(self) -> None:
        from app import main
        from app.block_store import create_block_document

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            database = notes / ".fortress.sqlite3"
            linked = create_block_document(database, "Linked", "rough", [], "Linked source text")
            linked_id = linked["children"][0]["id"]
            document = create_block_document(
                database,
                "Main",
                "rough",
                [],
                f"Focused source (({linked_id})).\n\nSecond source block.",
            )
            first_id = document["children"][0]["id"]
            second_id = document["children"][1]["id"]
            settings = SimpleNamespace(
                block_db_enabled=True,
                block_db_import_on_startup=False,
                block_db_path=database,
                notes_path=notes,
                assets_path=notes / "assets",
                embeddings_enabled=False,
                vision_enabled=False,
                reindex_interval_s=5.0,
            )
            async def fake_review(kind, context, targets, linked_context):
                return {
                    "kind": kind,
                    "context": context,
                    "summary": "Checked.",
                    "items": [],
                }

            with (
                patch.object(main, "settings", settings),
                patch.object(
                    main.llm, "review_blocks", new=AsyncMock(side_effect=fake_review)
                ) as review,
                TestClient(main.app) as client,
            ):
                current = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={"kind": "factcheck", "context": "current", "block_ids": [first_id]},
                )
                selected = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={
                        "kind": "clarify",
                        "context": "selected",
                        "block_ids": [first_id, second_id],
                    },
                )
                whole_document = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={"kind": "object", "context": "document", "block_ids": []},
                )
                linked_context = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={"kind": "factcheck", "context": "linked", "block_ids": [first_id]},
                )
                foreign_target = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={"kind": "factcheck", "context": "selected", "block_ids": [linked_id]},
                )
                wrong_shape = client.post(
                    f"/api/block-documents/{document['id']}/review-context",
                    json={"kind": "factcheck", "context": "current", "block_ids": []},
                )

            self.assertEqual(current.status_code, 200)
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(whole_document.status_code, 200)
            self.assertEqual(linked_context.status_code, 200)
            self.assertEqual(current.json()["context"], "current")
            self.assertEqual(selected.json()["context"], "selected")
            self.assertEqual(whole_document.json()["context"], "document")
            self.assertEqual(linked_context.json()["context"], "linked")
            self.assertEqual(foreign_target.status_code, 422)
            self.assertEqual(wrong_shape.status_code, 422)
            self.assertEqual(review.call_count, 4)
            current_call, selected_call, document_call, linked_call = review.call_args_list
            self.assertEqual(current_call.args[2][0]["id"], first_id)
            self.assertEqual(len(selected_call.args[2]), 2)
            self.assertEqual(len(document_call.args[2]), 2)
            self.assertEqual(linked_call.args[2][0]["id"], first_id)
            self.assertEqual([block["id"] for block in linked_call.args[3]], [linked_id])


if __name__ == "__main__":
    unittest.main()
