"""Every live document the editor opens has a block to type into.

BlockNote shows a blank paragraph for an empty document; if the server has no
block for it, the first keystrokes refer to a block it never heard of.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


class EmptyDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main, search

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        notes = Path(self._directory.name)
        self.settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=3600.0,
        )
        self.settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", self.settings),
            patch.object(documents, "get_settings", lambda: self.settings),
            patch.object(search, "get_settings", lambda: self.settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def open_and_type(self, document_id: str, text: str) -> dict:
        document = self.client.get(f"/api/block-documents/{document_id}").json()
        self.assertEqual(len(document["children"]), 1, document)
        block = document["children"][0]
        self.assertEqual((block["type"], block["text"]), ("paragraph", ""))
        response = self.client.post(
            f"/api/block-documents/{document_id}/transactions",
            json={
                "base_revision": document["revision"],
                "operations": [{"operation": "update", "block_id": block["id"], "text": text}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get(f"/api/block-documents/{document_id}").json()
        self.assertEqual([child["text"] for child in saved["children"]], [text])
        return saved

    def test_an_imported_empty_file_can_be_typed_into(self) -> None:
        from app.portability import import_markdown_directory

        source = Path(self._directory.name) / "import"
        source.mkdir()
        (source / "blank.md").write_text("---\ntitle: Blank\n---\n", encoding="utf-8")
        import_markdown_directory(self.settings.block_db_path, source)
        self.open_and_type("blank", "Now it has words")

    def test_an_uploaded_empty_file_can_be_typed_into(self) -> None:
        result = self.client.post(
            "/api/markdown-import/files",
            files=[("files", ("empty.md", b"", "text/markdown"))],
        ).json()
        self.open_and_type(result["imported"][0]["id"], "Typed after import")

    def test_a_note_emptied_by_move_into_and_restored_can_be_typed_into(self) -> None:
        source = self.client.post("/api/notes", json={"title": "Source", "body": "Moved away"}).json()
        target = self.client.post("/api/notes", json={"title": "Target", "body": "Stays"}).json()
        moved = self.client.post(f"/api/block-documents/{source['id']}/move-into/{target['id']}")
        self.assertEqual(moved.status_code, 200, moved.text)
        restored = self.client.post(f"/api/trash/{source['id']}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.open_and_type(source["id"], "Fresh start")

    def test_an_empty_note_still_exports_as_empty(self) -> None:
        from app.portability import export_document_markdown

        note = self.client.post("/api/notes", json={"title": "Empty", "body": ""}).json()
        self.client.get(f"/api/block-documents/{note['id']}")
        markdown = export_document_markdown(self.settings.block_db_path, note["id"])
        self.assertEqual(markdown.split("---", 2)[2].strip(), "")


if __name__ == "__main__":
    unittest.main()
