"""Schema 15: Fortress has no math or ink, and stored ones lose nothing.

A math block becomes a code block holding its LaTeX (language ``latex``), an
image imported from ink drops only its ``ink`` marker; re-import
hashes and conflicts follow.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import block_schema as schema
from app import block_store

MATH = {"latex": "e=mc^2", "mathml": "<math><mi>e</mi></math>"}
INK = {"url": "/media/01J0000000000000000000000A.svg", "name": "Ink drawing",
       "alt": "handwritten words", "width": 120, "ink": True}


class RetireMathAndInkTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / ".fortress.sqlite3"
        block_store.create_document(self.db, "D1", "Note", "rough", "first\n\nsecond\n",
                                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
        tree = block_store.document_tree(self.db, "D1")
        assert tree is not None
        self.first, self.math = (node["id"] for node in tree["children"])
        self.ink = block_store.new_id()
        old_math = schema.Block("math", {"onenote_object": "x"}, None, MATH)
        # What version 14 could store: written with SQL, as the API no longer can.
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """UPDATE blocks SET type = 'math', attrs_json = ?, content_json = NULL,
                       payload_json = ?, text = 'e=mc^2' WHERE id = ?""",
                (json.dumps(old_math.attrs), json.dumps(MATH), self.math),
            )
            connection.execute(
                """INSERT INTO blocks(id, document_id, parent_id, order_key, type, attrs_json,
                       content_json, payload_json, text, created_at, updated_at)
                   VALUES (?, 'D1', NULL, 'zz', 'image', ?, NULL, NULL, 'handwritten words',
                           '2026-01-01', '2026-01-01')""",
                (self.ink, json.dumps(INK)),
            )
            # Version 14 had re-import state per block (retired since).
            connection.execute(
                """CREATE TABLE block_sync (block_id TEXT PRIMARY KEY, source_hash TEXT,
                       dismissed_hash TEXT, conflict_json TEXT, source_removed INTEGER NOT NULL DEFAULT 0)"""
            )
            connection.execute("DELETE FROM schema_migrations")
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (14, '')")
        connection.close()
        block_store.initialize(self.db, force=True)

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            result = connection.execute(sql, args).fetchall()
        connection.close()
        return result

    def test_math_becomes_a_latex_code_block(self) -> None:
        self.assertEqual(self.rows("SELECT MAX(version) AS v FROM schema_migrations")[0]["v"], 15)
        tree = block_store.document_tree(self.db, "D1")
        assert tree is not None
        code = next(node for node in tree["children"] if node["id"] == self.math)
        self.assertEqual(code["type"], "code")
        self.assertEqual(code["attrs"], {"onenote_object": "x", "language": "latex"})
        self.assertEqual(code["content"], [{"type": "text", "text": "e=mc^2"}])
        self.assertIsNone(code.get("payload"))
        self.assertEqual(code["text"], "e=mc^2")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM blocks WHERE type = 'math'")[0]["n"], 0)
        self.assertTrue(block_store.fts_is_consistent(self.db))

    def test_ink_image_keeps_its_picture_and_text(self) -> None:
        tree = block_store.document_tree(self.db, "D1")
        assert tree is not None
        image = next(node for node in tree["children"] if node["id"] == self.ink)
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["attrs"], {k: v for k, v in INK.items() if k != "ink"})
        self.assertEqual(image["text"], "handwritten words")


if __name__ == "__main__":
    unittest.main()
