import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from support import make_settings, use_settings, mirror_file


class QuickCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.notes = Path(self._directory.name)
        settings = make_settings(self.notes, reindex_interval_s=5.0)
        patcher = use_settings(settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def folder(self) -> dict:
        items = self.client.get("/api/navigation").json()["items"]
        [folder] = [item for item in items if item["kind"] == "folder"]
        return folder

    def test_simultaneous_first_captures_share_one_uncategorized_folder(self) -> None:
        from app import documents

        barrier = threading.Barrier(6)

        def capture(index: int) -> None:
            barrier.wait()
            documents.capture(f"Thought {index}")

        threads = [threading.Thread(target=capture, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(self.folder()["children"]), 6)

    def test_each_capture_is_its_own_note_in_uncategorized_newest_first(self) -> None:
        self.client.post("/api/block-documents", json={"title": "Existing", "body": "x"})
        first = self.client.post("/api/capture", json={"text": "Call the plumber"})
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            "/api/capture", json={"text": "Idea: shared calendar\nwith the whole family"}
        ).json()
        self.assertEqual(second["title"], "Idea: shared calendar")
        self.assertEqual(second["folder"], "Uncategorized")

        folder = self.folder()
        self.assertEqual(folder["name"], "Uncategorized")
        self.assertEqual(folder["position"], 0)  # sits at the top of the sidebar
        self.assertEqual(
            [child["title"] for child in folder["children"]],
            ["Idea: shared calendar", "Call the plumber"],
        )
        note = self.client.get(f"/api/block-documents/{second['document_id']}").json()
        self.assertIn("with the whole family", " ".join(b["text"] for b in note["children"]))
        from app import documents

        documents.flush_mirrors()
        self.assertTrue(mirror_file(self.notes, second["document_id"]).exists())
        from app import block_store

        hits = block_store.search_blocks(self.notes / ".fortress.sqlite3", "plumber")
        self.assertEqual({hit["document_id"] for hit in hits}, {first.json()["document_id"]})

    def test_a_deleted_folder_is_recreated_and_blank_text_is_refused(self) -> None:
        note = self.client.post("/api/capture", json={"text": "one"}).json()
        self.client.post(
            f"/api/block-documents/{note['document_id']}/move", json={"folder_id": None}
        )
        self.client.delete(f"/api/folders/{self.folder()['id']}")
        self.client.post("/api/capture", json={"text": "two"})
        self.assertEqual([c["title"] for c in self.folder()["children"]], ["two"])
        self.assertEqual(self.client.post("/api/capture", json={"text": " \n "}).status_code, 422)

    def test_captured_and_created_titles_drop_list_and_quote_markers(self) -> None:
        # Same rule as the auto title that follows the first line while editing.
        todo = self.client.post(
            "/api/capture", json={"text": "- [ ] Buy oat milk\nfrom the corner shop"}
        ).json()
        self.assertEqual(todo["title"], "Buy oat milk")
        quote = self.client.post("/api/block-documents", json={"title": "", "body": "> Stay hungry"}).json()
        self.assertEqual(quote["title"], "Stay hungry")
        long = self.client.post("/api/capture", json={"text": "word " * 40}).json()
        self.assertTrue(long["title"].endswith("word…"), long["title"])


if __name__ == "__main__":
    unittest.main()
