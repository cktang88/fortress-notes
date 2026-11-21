import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


# A block reference reads as its target's current text.
EDITED = "Edited searchable phrase A migrated searchable block"

from support import add_markdown_notes, make_settings, use_settings


class BlockApiFlowTests(unittest.TestCase):
    def test_migration_edit_search_references_export_and_restart(self) -> None:
        from app import block_store, main

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            (notes / "target.md").write_text(
                "---\ntitle: Target\n---\nA migrated searchable block\n",
                encoding="utf-8",
            )
            settings = make_settings(notes, reindex_interval_s=5.0)
            add_markdown_notes(notes, settings.block_db_path)


            with use_settings(settings), use_settings(settings):
                with TestClient(main.app) as client:
                    migrated = client.get("/api/block-documents/target")
                    self.assertEqual(migrated.status_code, 200)
                    target_block_id = migrated.json()["children"][0]["id"]

                    created = client.post(
                        "/api/block-documents",
                        json={
                            "title": "Source",
                            "body": f"Draft body [[#^{target_block_id}|target]]",
                        },
                    )
                    self.assertEqual(created.status_code, 201)
                    source = created.json()
                    block = source["children"][0]
                    self.assertEqual(block["text"], "Draft body A migrated searchable block")

                    edited = client.post(
                        f"/api/block-documents/{source['id']}/transactions?response=document",
                        json={
                            "base_revision": source["revision"],
                            "operations": [
                                {
                                    "operation": "update",
                                    "block_id": block["id"],
                                    "content": [
                                        {"type": "text", "text": "Edited searchable phrase "},
                                        {"type": "blockRef", "id": target_block_id},
                                    ],
                                }
                            ],
                        },
                    )
                    self.assertEqual(edited.status_code, 200)
                    self.assertEqual(edited.json()["children"][0]["text"],
                                     EDITED)

                    results = block_store.search_blocks(settings.block_db_path, "searchable phrase")
                    self.assertEqual(results[0]["document_id"], source["id"])
                    backlinks = client.get("/api/block-documents/target/backlinks")
                    self.assertEqual(backlinks.status_code, 200)
                    self.assertEqual(backlinks.json()[0]["source_document_id"], source["id"])
                    exported = client.get(f"/api/block-documents/{source['id']}/markdown")
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("Edited searchable phrase", exported.text)

                    malformed = client.post(
                        f"/api/block-documents/{source['id']}/transactions?response=document",
                        json={"base_revision": 1, "operations": [{"operation": "nonsense"}]},
                    )
                    self.assertEqual(malformed.status_code, 422)

                # A fresh lifespan initializes a new connection to the same database.
                with TestClient(main.app) as restarted:
                    recovered = restarted.get(f"/api/block-documents/{source['id']}")
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(
                        recovered.json()["children"][0]["text"],
                        EDITED,
                    )
