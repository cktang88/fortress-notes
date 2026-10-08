import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class BlockApiFlowTests(unittest.TestCase):
    def test_migration_edit_search_references_export_and_restart(self) -> None:
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            (notes / "target.md").write_text(
                "---\ntitle: Target\n---\nA migrated searchable block\n",
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                block_db_enabled=True,
                block_db_import_on_startup=True,
                block_db_path=notes / ".fortress.sqlite3",
                notes_path=notes,
                assets_path=notes / "assets",
                embeddings_enabled=False,
                vision_enabled=False,
                reindex_interval_s=5.0,
            )

            from app import documents

            with patch.object(main, "settings", settings), patch.object(
                documents, "get_settings", return_value=settings
            ):
                with TestClient(main.app) as client:
                    migrated = client.get("/api/block-documents/target")
                    self.assertEqual(migrated.status_code, 200)
                    target_block_id = migrated.json()["children"][0]["id"]

                    created = client.post(
                        "/api/block-documents",
                        json={
                            "title": "Source",
                            "body": f"Draft body (({target_block_id} \"target\"))",
                        },
                    )
                    self.assertEqual(created.status_code, 201)
                    source = created.json()
                    block = source["children"][0]
                    original_text = block["text"]

                    edited = client.post(
                        f"/api/block-documents/{source['id']}/transactions",
                        json={
                            "base_revision": source["revision"],
                            "operations": [
                                {
                                    "operation": "update",
                                    "block_id": block["id"],
                                    "text": f"Edited searchable phrase (({target_block_id} \"target\"))",
                                }
                            ],
                        },
                    )
                    self.assertEqual(edited.status_code, 200)
                    self.assertEqual(edited.json()["children"][0]["text"],
                                     f"Edited searchable phrase (({target_block_id} \"target\"))")

                    undone = client.post(
                        f"/api/block-documents/{source['id']}/undo",
                        json={"base_revision": edited.json()["revision"]},
                    )
                    self.assertEqual(undone.status_code, 200)
                    self.assertTrue(undone.json()["can_redo"])
                    redone = client.post(
                        f"/api/block-documents/{source['id']}/redo",
                        json={"base_revision": undone.json()["revision"]},
                    )
                    self.assertEqual(redone.status_code, 200)
                    self.assertEqual(
                        redone.json()["children"][0]["text"],
                        f"Edited searchable phrase (({target_block_id} \"target\"))",
                    )

                    results = client.get("/api/block-search", params={"q": "searchable phrase"})
                    self.assertEqual(results.status_code, 200)
                    self.assertEqual(results.json()[0]["document_id"], source["id"])
                    backlinks = client.get("/api/block-documents/target/backlinks")
                    self.assertEqual(backlinks.status_code, 200)
                    self.assertEqual(backlinks.json()[0]["source_document_id"], source["id"])
                    exported = client.get(f"/api/block-documents/{source['id']}/markdown")
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("Edited searchable phrase", exported.text)
                    self.assertEqual(client.get("/api/markdown-export").status_code, 200)

                    malformed = client.post(
                        f"/api/block-documents/{source['id']}/transactions",
                        json={"base_revision": 1, "operations": [{"operation": "nonsense"}]},
                    )
                    self.assertEqual(malformed.status_code, 422)

                # A fresh lifespan initializes a new connection to the same database.
                settings.block_db_import_on_startup = False
                with TestClient(main.app) as restarted:
                    recovered = restarted.get(f"/api/block-documents/{source['id']}")
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(
                        recovered.json()["children"][0]["text"],
                        f"Edited searchable phrase (({target_block_id} \"target\"))",
                    )
                    restarted_undo = restarted.post(
                        f"/api/block-documents/{source['id']}/undo",
                        json={"base_revision": recovered.json()["revision"]},
                    )
                    self.assertEqual(restarted_undo.status_code, 200)
                    self.assertEqual(
                        restarted_undo.json()["children"][0]["text"],
                        original_text,
                    )
                    self.assertEqual(
                        restarted.get("/api/block-search", params={"q": "searchable phrase"}).status_code,
                        200,
                    )
