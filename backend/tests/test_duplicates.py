import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

PLAN = (
    "Kitchen renovation plan: replace cabinets with oak, install quartz countertops, "
    "move the sink under the window, add pendant lighting above the island, "
    "budget twelve thousand dollars, contractor Miguel starts in March."
)


class DuplicateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import documents, main

        self._directory = tempfile.TemporaryDirectory()
        notes = Path(self._directory.name)
        self.notes = notes
        settings = SimpleNamespace(
            block_db_import_on_startup=False,
            block_db_path=notes / ".fortress.sqlite3",
            notes_path=notes,
            assets_path=notes / "assets",
            embeddings_enabled=False,
            vision_enabled=False,
            auto_backup_enabled=False,
            reindex_interval_s=5.0,
        )
        settings.assets_path.mkdir()
        for item in (
            patch.object(main, "settings", settings),
            patch.object(documents, "get_settings", lambda: settings),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._directory.cleanup)

    def create(self, title: str, body: str) -> str:
        return self.client.post("/api/notes", json={"title": title, "body": body}).json()["id"]

    def test_finds_a_note_that_repeats_this_one(self) -> None:
        original = self.create("Kitchen", PLAN + "\n\nPaint colour still undecided.")
        again = self.create("Reno thoughts", PLAN.replace("March", "April"))
        self.create("Groceries", "Milk eggs bread butter cheese apples bananas rice beans "
                    "pasta tomatoes onions garlic")
        [match] = self.client.get(f"/api/block-documents/{again}/similar").json()
        self.assertEqual(match["document_id"], original)
        self.assertGreaterEqual(match["overlap"], 0.9)

    def test_short_or_unrelated_notes_are_never_flagged(self) -> None:
        self.create("Kitchen", PLAN)
        short = self.create("Short", "kitchen cabinets oak")
        other = self.create("Garden", "Plant tomatoes basil peppers along the fence, water "
                            "every morning, mulch the beds, compost weekly, prune roses")
        self.assertEqual(self.client.get(f"/api/block-documents/{short}/similar").json(), [])
        self.assertEqual(self.client.get(f"/api/block-documents/{other}/similar").json(), [])

    def test_move_into_keeps_block_ids_and_links_then_trashes_the_source(self) -> None:
        target = self.create("Kitchen", "First paragraph")
        source = self.create("Dup", "Moved one\n\nMoved two")
        source_blocks = self.client.get(f"/api/block-documents/{source}").json()["children"]
        linker = self.create("Linker", f'See (( {source_blocks[0]["id"]} ))')

        response = self.client.post(f"/api/block-documents/{source}/move-into/{target}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["moved"], 2)

        tree = self.client.get(f"/api/block-documents/{target}").json()
        self.assertEqual(
            [block["text"] for block in tree["children"]],
            ["First paragraph", "Moved one", "Moved two"],
        )
        self.assertEqual(tree["children"][1]["id"], source_blocks[0]["id"])
        backlinks = self.client.get(f"/api/block-documents/{target}/backlinks").json()
        self.assertEqual([b["source_document_id"] for b in backlinks], [linker])
        self.assertEqual(self.client.get(f"/api/block-documents/{source}").status_code, 404)
        self.assertIn(source, [item["id"] for item in self.client.get("/api/trash").json()])
        hits = self.client.get("/api/block-search", params={"q": "Moved"}).json()
        self.assertEqual({hit["document_id"] for hit in hits}, {target})
        self.assertIn("Moved two", (self.notes / f"{target}.md").read_text())

    def test_move_into_rejects_itself_and_missing_notes(self) -> None:
        note = self.create("A", "text")
        self.assertEqual(
            self.client.post(f"/api/block-documents/{note}/move-into/{note}").status_code, 422
        )
        self.assertEqual(
            self.client.post(f"/api/block-documents/{note}/move-into/missing").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
