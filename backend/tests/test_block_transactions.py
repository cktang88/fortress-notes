"""Block operations (docs/block-model.md §4): placement, attrs PATCH, rejections."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.block_schema import BlockShapeError
from app.block_store import apply_transaction, create_block_document, document_tree
from app.models import BlockOperation
from support import make_settings, use_settings


def runs(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db = Path(self._directory.name) / ".fortress.sqlite3"
        self.tree = create_block_document(self.db, "T", "rough", "A\n\nB")
        self.a, self.b = (node["id"] for node in self.tree["children"])

    def apply(self, *operations: BlockOperation) -> dict:
        self.tree = apply_transaction(self.db, self.tree["id"], list(operations), self.tree["revision"])
        return self.tree

    def texts(self) -> list[str]:
        return [node["text"] for node in self.tree["children"]]

    def test_inserts_land_between_the_named_neighbours(self) -> None:
        self.apply(
            BlockOperation(operation="insert", type="paragraph", content=runs("first"), before_id=self.a),
            BlockOperation(operation="insert", type="paragraph", content=runs("middle"), after_id=self.a),
            BlockOperation(operation="insert", type="paragraph", content=runs("last")),
        )
        self.assertEqual(self.texts(), ["first", "A", "middle", "B", "last"])
        orders = [node["order"] for node in self.tree["children"]]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual([node["position"] for node in self.tree["children"]], [0, 1, 2, 3, 4])
        with self.assertRaises(ValueError):  # one anchor, not two
            self.apply(BlockOperation(operation="insert", type="paragraph",
                                      after_id=self.a, before_id=self.b))

    def test_attrs_are_a_patch(self) -> None:
        self.apply(BlockOperation(operation="insert", block_id="01J00000000000000000000001",
                                  type="image", attrs={"url": "/media/a.png", "alt": "A", "width": 10}))
        self.apply(BlockOperation(operation="update", block_id="01J00000000000000000000001",
                                  attrs={"alt": None, "width": 20}))
        self.assertEqual(self.tree["children"][-1]["attrs"], {"url": "/media/a.png", "width": 20})

    def test_illegal_shapes_are_refused_and_nothing_is_written(self) -> None:
        revision = self.tree["revision"]
        refused = [
            BlockOperation(operation="insert", type="bogus"),
            BlockOperation(operation="insert", type="table_cell"),
            BlockOperation(operation="insert", type="paragraph", parent_id=None,
                           content=[{"type": "mention"}]),
            BlockOperation(operation="update", block_id=self.a, type="image"),
            BlockOperation(operation="update", block_id=self.a, type="heading", attrs={"level": 9}),
            BlockOperation(operation="insert", type="code", parent_id=self.a,
                           content=[{"type": "docLink", "id": "x"}]),
            BlockOperation(operation="insert", type="paragraph", content=runs("x"), payload={"html": "x"}),
        ]
        for operation in refused:
            with self.subTest(operation=operation.model_dump(exclude_unset=True)):
                with self.assertRaises(BlockShapeError):
                    apply_transaction(self.db, self.tree["id"], [operation], revision)
        self.apply(BlockOperation(operation="insert", block_id="01J00000000000000000000002", type="table"))
        with self.assertRaises(BlockShapeError):  # a paragraph can't sit in a table
            self.apply(BlockOperation(operation="move", block_id=self.a,
                                      parent_id="01J00000000000000000000002"))
        tree = document_tree(self.db, self.tree["id"])
        self.assertEqual([node["text"] for node in tree["children"]], ["A", "B", ""])

    def test_legacy_type_names_are_unknown_types(self) -> None:
        revision = self.tree["revision"]
        for operation in (
            BlockOperation(operation="insert", type="checkListItem", attrs={"checked": True}),
            BlockOperation(operation="insert", type="list", attrs={"ordered": True}),
            BlockOperation(operation="update", block_id=self.b, type="bulletListItem"),
            BlockOperation(operation="update", block_id=self.b, type="codeBlock"),
        ):
            with self.subTest(operation=operation.model_dump(exclude_unset=True)):
                with self.assertRaisesRegex(BlockShapeError, "unknown block type"):
                    apply_transaction(self.db, self.tree["id"], [operation], revision)

    def test_legacy_operation_fields_are_not_part_of_the_model(self) -> None:
        for fields in (
            {"operation": "move", "block_id": "x", "position": 0},
            {"operation": "update", "block_id": "x", "text": "t"},
            {"operation": "update", "block_id": "x", "expected_updated_at": "t"},
            {"operation": "update", "block_id": "x", "content": {"blocknote": runs("t")}},
            {"operation": "update", "block_id": "x", "content": {"latex": "x^2"}},
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    BlockOperation(**fields)


class LegacyRequestShapeTests(unittest.TestCase):
    """The Mac app sends canonical operations; the old request shapes are refused (§9)."""

    def setUp(self) -> None:
        from app import main

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = use_settings(make_settings(Path(directory.name)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        note = self.client.post("/api/block-documents", json={"title": "T", "body": "A\n\nB"}).json()
        self.document = self.client.get(f"/api/block-documents/{note['id']}").json()

    def post(self, operation: dict):
        return self.client.post(
            f"/api/block-documents/{self.document['id']}/transactions",
            json={"base_revision": self.document["revision"], "operations": [operation]},
        )

    def test_legacy_shapes_get_a_422_and_change_nothing(self) -> None:
        a, b = (node["id"] for node in self.document["children"])
        refused = [
            # BlockNote type names
            {"operation": "insert", "type": "checkListItem", "attrs": {"checked": True}, "content": runs("x")},
            {"operation": "insert", "type": "codeBlock", "content": runs("x")},
            {"operation": "update", "block_id": a, "type": "numberedListItem"},
            # {"blocknote": runs} / {"latex"} / {"markdown"} content objects
            {"operation": "insert", "type": "paragraph",
             "content": {"blocknote": [{"type": "text", "text": "x", "styles": {"bold": True}}]}},
            {"operation": "update", "block_id": a, "content": {"blocknote": runs("x")}},
            {"operation": "insert", "type": "math", "content": {"latex": "x^2"}},
            {"operation": "insert", "type": "image", "attrs": {"url": "/media/a.png"},
             "content": {"markdown": "![](/media/a.png)"}},
            # BlockNote image width, inline runs on a container
            {"operation": "insert", "type": "image", "attrs": {"url": "/media/a.png", "previewWidth": 300}},
            {"operation": "insert", "type": "quote", "content": runs("quoted")},
            # numeric position instead of after_id/before_id
            {"operation": "insert", "type": "paragraph", "content": runs("x"), "position": 0},
            {"operation": "move", "block_id": b, "position": 0},
            # fields that used to be ignored
            {"operation": "update", "block_id": a, "text": "stale"},
            {"operation": "update", "block_id": a, "content": runs("x"), "expected_updated_at": "2026-01-01"},
        ]
        for operation in refused:
            with self.subTest(operation=operation):
                response = self.post(operation)
                self.assertEqual(response.status_code, 422, response.text)
        after = self.client.get(f"/api/block-documents/{self.document['id']}").json()
        self.assertEqual(after["revision"], self.document["revision"])
        self.assertEqual([node["text"] for node in after["children"]], ["A", "B"])

    def test_link_syntax_typed_as_text_stays_text(self) -> None:
        a = self.document["children"][0]["id"]
        response = self.post({"operation": "update", "block_id": a, "content": runs("see [[other]]")})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.client.get(f"/api/block-documents/{self.document['id']}").json()
        self.assertEqual(after["children"][0]["content"], runs("see [[other]]"))

if __name__ == "__main__":
    unittest.main()
