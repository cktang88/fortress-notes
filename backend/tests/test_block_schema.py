"""The block registry, inline nodes and order keys (docs/block-model.md §2-§4)."""

import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from app import block_schema as schema
from app import order_keys
from app.block_schema import BlockShapeError

from support import make_settings, use_settings


# The Mac app's copy of the registry (BlockSchema.swift) is checked against this file
# by its tests (BlockSchemaTests); this test keeps the file in step with block_schema.py.
MAC_FIXTURE = (Path(__file__).resolve().parents[2]
               / "mac/Tests/FortressKitTests/Fixtures/block-schema.json")


def registry_fixture() -> dict:
    return {
        "flow": sorted(schema.FLOW),
        "types": {
            name: {"category": block.category, "children": sorted(block.children),
                   "change_group": block.change_group}
            for name, block in sorted(schema.TYPES.items())
        },
    }


class RegistryTests(unittest.TestCase):
    def test_the_mac_apps_registry_fixture_matches(self) -> None:
        text = json.dumps(registry_fixture(), indent=2, sort_keys=True) + "\n"
        if os.environ.get("FORTRESS_WRITE_FIXTURES") == "1":
            MAC_FIXTURE.write_text(text, encoding="utf-8")
        self.assertEqual(
            MAC_FIXTURE.read_text(encoding="utf-8"), text,
            "block_schema.py changed: run this test with FORTRESS_WRITE_FIXTURES=1, then "
            "update mac/Sources/FortressKit/Editor/Model/BlockSchema.swift to match",
        )

    def test_unknown_types_and_illegal_nesting_are_refused(self) -> None:
        with self.assertRaises(BlockShapeError):
            schema.block_type("bogus")
        for parent, child in (
            (None, "table_row"), (None, "table_cell"), (None, "column"),
            ("table", "paragraph"), ("table_row", "paragraph"), ("columns", "paragraph"),
            ("code", "paragraph"), ("image", "paragraph"), ("table_cell", "columns"),
            ("column", "columns"),
        ):
            with self.subTest(parent=parent, child=child):
                with self.assertRaises(BlockShapeError):
                    schema.check_nesting(parent, child)
        for parent, child in (
            (None, "paragraph"), ("bullet_item", "table"), ("quote", "callout"),
            ("table", "table_row"), ("table_row", "table_cell"), ("table_cell", "table"),
            ("columns", "column"), ("column", "image"), ("heading", "paragraph"),
        ):
            schema.check_nesting(parent, child)

    def test_type_changes_stay_within_their_group(self) -> None:
        schema.check_type_change("paragraph", "check_item")
        schema.check_type_change("heading", "code")
        schema.check_type_change("quote", "callout")
        for old, new in (("paragraph", "image"), ("quote", "paragraph"), ("table", "columns"),
                         ("divider", "paragraph")):
            with self.assertRaises(BlockShapeError):
                schema.check_type_change(old, new)
        # The old type's own attributes go; extension attributes stay.
        self.assertEqual(
            schema.retype_attrs("heading", "check_item", {"level": 2, "due": "today"}),
            {"due": "today"},
        )

    def test_attrs_are_validated_defaulted_and_patched(self) -> None:
        self.assertEqual(schema.normalize_attrs("heading", {}), {"level": 1})
        self.assertEqual(schema.normalize_attrs("check_item", {"onenote_tags": ["x"]}),
                         {"onenote_tags": ["x"], "checked": False})
        for type_name, attrs in (
            ("heading", {"level": 7}), ("check_item", {"checked": "yes"}),
            ("table_cell", {"background": "javascript:x"}), ("paragraph", {"Bad-Key": 1}),
        ):
            with self.subTest(type_name=type_name, attrs=attrs):
                with self.assertRaises(BlockShapeError):
                    schema.normalize_attrs(type_name, attrs)
        # A PATCH sets the keys it names, deletes the ones set to null, keeps the rest.
        self.assertEqual(
            schema.patch_attrs({"url": "/media/a.png", "alt": "A", "width": 10},
                               {"alt": None, "width": 20}),
            {"url": "/media/a.png", "width": 20},
        )

    def test_the_api_refuses_illegal_nesting(self) -> None:
        from fastapi.testclient import TestClient

        from app import main

        with tempfile.TemporaryDirectory() as directory:
            notes = Path(directory)
            settings = make_settings(notes)
            with use_settings(settings), \
                    use_settings(settings), \
                    TestClient(main.app) as client:
                note = client.post("/api/block-documents", json={"title": "N", "body": "x"}).json()
                refused = client.post(
                    f"/api/block-documents/{note['id']}/transactions",
                    json={"base_revision": note["revision"],
                          "operations": [{"operation": "insert", "type": "table_row"}]},
                )
                self.assertEqual(refused.status_code, 422, refused.text)


class InlineTests(unittest.TestCase):
    def test_nodes_are_normalized_and_merged(self) -> None:
        nodes = schema.normalize_inline([
            {"type": "text", "text": "a", "marks": {"bold": True}},
            {"type": "text", "text": "b", "marks": {"bold": True}},
            {"type": "text", "text": ""},
            {"type": "link", "href": "https://example.com",
             "content": [{"type": "text", "text": "site"}]},
            {"type": "docLink", "id": "note-1"},
            {"type": "blockRef", "id": "01J00000000000000000000001"},
        ])
        self.assertEqual(nodes, [
            {"type": "text", "text": "ab", "marks": {"bold": True}},
            {"type": "link", "href": "https://example.com", "content": [{"type": "text", "text": "site"}]},
            {"type": "docLink", "id": "note-1"},
            {"type": "blockRef", "id": "01J00000000000000000000001"},
        ])
        self.assertEqual(schema.references(nodes), ({"note-1"}, {"01J00000000000000000000001"}))

        def label(kind: str, target: str) -> str:
            return {"docLink": "Note one", "blockRef": "a block"}[kind]

        self.assertEqual(schema.inline_text(nodes, label), "absiteNote onea block")
        for bad in (
            [{"type": "text", "text": "x", "marks": {"blink": True}}],
            [{"type": "mention", "id": "x"}],
            [{"type": "docLink"}],
            [{"type": "link", "href": "javascript:alert(1)", "content": [{"type": "text", "text": "x"}]}],
            [{"type": "link", "href": " Java\tScript:x", "content": [{"type": "text", "text": "x"}]}],
            "plain string",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(BlockShapeError):
                    schema.normalize_inline(bad)

    def test_code_is_plain_text_and_atomic_blocks_have_no_content(self) -> None:
        # Code is plain text: marks are dropped, links refused.
        code = schema.normalize_block("code", {}, [{"type": "text", "text": "x", "marks": {"bold": True}}])
        self.assertEqual(code.content, [{"type": "text", "text": "x"}])
        with self.assertRaises(BlockShapeError):
            schema.normalize_block("code", {}, [{"type": "docLink", "id": "x"}])
        with self.assertRaises(BlockShapeError):
            schema.normalize_block("image", {"url": "/media/a.png"}, [{"type": "text", "text": "x"}])
        with self.assertRaises(BlockShapeError):
            schema.normalize_block("math", {}, None, {"latex": "a^2"})
        html = schema.normalize_block("html", {}, None, {"html": "<b>bold</b> words"})
        self.assertEqual(schema.derive_text(html), "bold words")

    def test_canonical_hash_ignores_key_order(self) -> None:
        one = schema.normalize_block("image", {"url": "/media/a.png", "alt": "A"})
        two = schema.normalize_block("image", {"alt": "A", "url": "/media/a.png"})
        self.assertEqual(schema.canonical_hash(one), schema.canonical_hash(two))
        self.assertNotEqual(
            schema.canonical_hash(one),
            schema.canonical_hash(schema.normalize_block("image", {"url": "/media/b.png"})),
        )


class OrderKeyTests(unittest.TestCase):
    def test_appends_stay_short_and_sorted(self) -> None:
        keys = [order_keys.key_between(None, None)]
        for _ in range(5000):
            keys.append(order_keys.key_between(keys[-1], None))
        self.assertEqual(keys, sorted(keys))
        self.assertLessEqual(max(len(key) for key in keys), 4)

    def test_random_inserts_stay_unique_and_sorted(self) -> None:
        generator = random.Random(7)
        keys = [order_keys.key_between(None, None)]
        for _ in range(3000):
            index = generator.randrange(len(keys) + 1)
            low = keys[index - 1] if index > 0 else None
            high = keys[index] if index < len(keys) else None
            keys.insert(index, order_keys.key_between(low, high))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_runs_of_keys_and_bad_bounds(self) -> None:
        run = order_keys.keys_between("a0", "a1", 50)
        self.assertEqual(run, sorted(run))
        self.assertTrue(all("a0" < key < "a1" for key in run))
        with self.assertRaises(order_keys.OrderKeyError):
            order_keys.key_between("a1", "a0")


if __name__ == "__main__":
    unittest.main()
