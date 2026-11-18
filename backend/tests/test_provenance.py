"""Provenance, metadata and living (re-importable) documents."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import backups, block_store, provenance, sources
from app.block_store import (
    apply_transaction,
    connection_scope,
    create_document,
    document_tree,
    initialize,
    redo_transaction,
    rename_document,
    undo_transaction,
)
from app.models import BlockOperation
from app.portability import export_document_markdown, import_markdown_directory
from app.sources import SourceBlock, document_info, upsert_document_from_source

from support import make_settings, use_settings


def _texts(nodes):
    return [node["text"] for node in nodes]


def _runs(text):
    return [{"type": "text", "text": text}]


def _paragraph(text, **fields):
    return BlockOperation(operation="insert", type="paragraph", content=_runs(text), **fields)


def _find(nodes, text):
    for node in nodes:
        if node["text"] == text:
            return node
        found = _find(node["children"], text)
        if found is not None:
            return found
    return None


ONENOTE = {"name": "onenote", "version": "1.0"}


class ProvenanceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.db = self.root / ".fortress.sqlite3"

    def page(self, blocks, **kwargs):
        options = dict(
            source="onenote_import",
            external_id="{PAGE-1}",
            title="Meeting",
            blocks=blocks,
            created_at="2020-01-01T09:00:00+00:00",
            modified_at="2020-01-02T09:00:00+00:00",
            provenance={"source_path": "Work › Meetings › Meeting"},
            importer=ONENOTE,
        )
        options.update(kwargs)
        return upsert_document_from_source(self.db, **options)

    # --- Basic provenance --------------------------------------------------

    def test_new_documents_default_to_manual_and_blocks_inherit(self) -> None:
        create_document(self.db, "doc", "Doc", "rough", "One\n\nTwo", "t", "t")
        tree = document_tree(self.db, "doc")
        self.assertEqual(tree["provenance"], {"source": "manual"})
        self.assertEqual(
            tree["children"][0]["provenance"], {"source": "manual", "inherited": True}
        )
        self.assertIsNone(tree["children"][0]["revision"])
        self.assertNotIn("last_opened_at", tree)  # in /info; not part of the note's ETag

    def test_editor_inserts_are_manual_or_what_the_client_says_and_survive_undo(self) -> None:
        create_document(self.db, "doc", "Doc", "rough", "One", "t", "t",
                        provenance={"source": "capture"})
        tree = apply_transaction(self.db, "doc", [
            _paragraph("typed", block_id="01J00000000000000000000001"),
            _paragraph("pasted", block_id="01J00000000000000000000002",
                       provenance={"source": "web", "source_url": "https://example.com/a"}),
        ], 0)
        typed, pasted = tree["children"][1], tree["children"][2]
        self.assertEqual(typed["provenance"], {"source": "manual"})
        self.assertEqual(pasted["provenance"]["source_url"], "https://example.com/a")
        self.assertEqual((typed["revision"], pasted["revision"]), (1, 1))
        self.assertEqual(tree["children"][0]["provenance"]["source"], "capture")

        # Editing keeps provenance; the block's revision follows the edit.
        tree = apply_transaction(self.db, "doc", [
            BlockOperation(operation="update", block_id=pasted["id"], content=_runs("pasted, edited")),
        ], 1)
        self.assertEqual(tree["children"][2]["provenance"]["source"], "web")
        self.assertEqual(tree["children"][2]["revision"], 2)
        self.assertEqual(tree["children"][1]["revision"], 1)

        tree = undo_transaction(self.db, "doc", 2)
        self.assertEqual(tree["children"][2]["text"], "pasted")
        self.assertEqual(tree["children"][2]["revision"], 1)
        self.assertEqual(tree["children"][2]["provenance"]["source"], "web")
        tree = undo_transaction(self.db, "doc", 3)
        self.assertEqual(len(tree["children"]), 1)
        tree = redo_transaction(self.db, "doc", 4)
        self.assertEqual(tree["children"][2]["provenance"]["source_url"], "https://example.com/a")
        self.assertEqual(tree["children"][2]["revision"], 1)


    def test_client_provenance_is_validated_and_cannot_set_internal_fields(self) -> None:
        with self.assertRaises(provenance.ProvenanceError):
            provenance.normalize({"source": "Not A Kind"})
        with self.assertRaises(provenance.ProvenanceError):
            provenance.normalize({"source": "web", "source_url": "javascript:alert(1)"})
        self.assertEqual(
            provenance.normalize({"source": "paste", "source_hash": "x", "conflict": {}}),
            {"source": "paste"},
        )


    def test_move_into_keeps_each_blocks_source_and_records_the_old_note(self) -> None:
        from app import duplicates

        create_document(self.db, "captured", "Captured", "rough", "Thought", "t", "t",
                        provenance={"source": "capture"})
        create_document(self.db, "target", "Target", "rough", "Body", "t", "t")
        duplicates.move_into(self.db, "captured", "target")
        tree = document_tree(self.db, "target")
        moved = tree["children"][1]
        self.assertEqual(moved["provenance"]["source"], "capture")
        self.assertNotIn("inherited", moved["provenance"])
        self.assertEqual(moved["provenance"]["origin"]["operation"], "move_into")
        self.assertEqual(moved["provenance"]["origin"]["document_id"], "captured")
        self.assertEqual(moved["provenance"]["origin"]["document_title"], "Captured")
        self.assertEqual(moved["revision"], tree["revision"])
        self.assertEqual(tree["children"][0]["provenance"]["inherited"], True)

    def test_provenance_is_not_searchable_text(self) -> None:
        self.page([SourceBlock("Plain words", external_id="{A}")],
                  provenance={"source_path": "Zebraword › Section"})
        self.assertEqual(block_store.search_blocks(self.db, "Zebraword"), [])
        self.assertEqual(len(block_store.search_blocks(self.db, "Plain")), 1)
        self.assertTrue(block_store.fts_is_consistent(self.db))

    # --- Markdown round trip -----------------------------------------------

    def test_markdown_export_and_reimport_restore_document_and_block_provenance(self) -> None:
        self.page([
            SourceBlock("# Agenda", external_id="{H}"),
            SourceBlock("- Item", external_id="{L}", children=[
                SourceBlock("- Child", external_id="{C}"),
            ]),
            SourceBlock("```\ncode\n```", external_id="{K}"),
        ])
        document_id = self._page_id()
        tree = document_tree(self.db, document_id)
        apply_transaction(self.db, document_id, [
            _paragraph("Typed here", block_id="01J00000000000000000000003", provenance={"source": "paste"}),
        ], tree["revision"])
        markdown = export_document_markdown(self.db, document_id)
        self.assertIn("source: onenote_import", markdown)
        self.assertIn("external_id: '{PAGE-1}'", markdown)
        self.assertIn('"provenance":{"external_id":"{C}","source":"onenote_import"}', markdown)
        self.assertIn("  - Child <!-- fortress:block", markdown)  # nested child keeps its indent
        self.assertNotIn(document_id, markdown.split("---", 2)[2])

        other = self.root / "other"
        other.mkdir()
        (other / "page.md").write_text(markdown, encoding="utf-8")
        target = other / ".fortress.sqlite3"
        import_markdown_directory(target, other)
        restored = document_tree(target, "page")
        self.assertEqual(restored["provenance"]["source"], "onenote_import")
        self.assertEqual(restored["provenance"]["external_id"], "{PAGE-1}")
        self.assertEqual(restored["provenance"]["source_path"], "Work › Meetings › Meeting")
        self.assertEqual(_texts(restored["children"]), ["Agenda", "Item", "code", "Typed here"])
        # Block ids travel with the export.
        self.assertEqual([node["id"] for node in restored["children"]],
                         [node["id"] for node in document_tree(self.db, document_id)["children"]])
        child = restored["children"][1]["children"][0]
        self.assertEqual(child["text"], "Child")
        self.assertEqual(child["provenance"]["external_id"], "{C}")
        self.assertEqual(restored["children"][3]["provenance"], {"source": "paste"})
        # No comment line became a block, and a second export is identical.
        self.assertEqual(export_document_markdown(target, "page"), markdown)

        # Re-importing the export into a database that has the source page reuses
        # nothing: the copy gives up the source id instead of stealing it.
        (other / "copy.md").write_text(markdown, encoding="utf-8")
        import_markdown_directory(self.db, other)
        copy = document_tree(self.db, "copy")
        self.assertNotIn("external_id", copy["provenance"])
        self.assertEqual(copy["provenance"]["extra"]["copy_of_external_id"], "{PAGE-1}")


    def test_plain_markdown_imports_are_marked_markdown_import(self) -> None:
        (self.root / "note.md").write_text(
            "---\ntitle: Hi\ncreated_at: '2019-05-01T00:00:00+00:00'\n---\nBody\n", encoding="utf-8"
        )
        import_markdown_directory(self.db, self.root)
        tree = document_tree(self.db, "note")
        self.assertEqual(tree["provenance"]["source"], "markdown_import")
        self.assertEqual(tree["provenance"]["source_path"], "note.md")
        self.assertEqual(tree["provenance"]["source_created_at"], "2019-05-01T00:00:00+00:00")
        self.assertEqual(tree["provenance"]["importer"]["name"], "fortress-markdown")

    # --- Links follow renames ----------------------------------------------

    def test_note_links_show_the_current_title_after_a_rename(self) -> None:
        create_document(self.db, "target", "Old title", "rough", "Target body", "t", "t")
        create_document(self.db, "source", "Source", "rough", "", "t", "t")
        target_block = document_tree(self.db, "target")["children"][0]["id"]
        apply_transaction(self.db, "source", [
            BlockOperation(
                operation="insert", block_id="01J00000000000000000000004", type="paragraph",
                content=[
                    {"type": "text", "text": "See "}, {"type": "docLink", "id": "target"},
                    {"type": "text", "text": " and "}, {"type": "blockRef", "id": target_block},
                    {"type": "text", "text": " or "},
                    {"type": "link", "href": "https://example.com",
                     "content": [{"type": "text", "text": "custom words"}]},
                ],
            ),
            BlockOperation(
                operation="insert", block_id="01J0000000000000000000000A", type="paragraph",
                content=[{"type": "text", "text": "Plain "}, {"type": "docLink", "id": "target"}],
            ),
        ], 0)
        rename_document(self.db, "target", "New | title")
        apply_transaction(self.db, "target", [
            BlockOperation(operation="update", block_id=target_block, content=_runs("Edited target")),
        ], 0)
        tree = document_tree(self.db, "source")
        node = tree["children"][0]
        # Links are ids; their text is the target's current title and text.
        self.assertEqual(node["text"], "See New | title and Edited target or custom words")
        self.assertEqual(node["content"][1], {"type": "docLink", "id": "target"})
        self.assertEqual(tree["children"][1]["text"], "Plain New | title")
        self.assertEqual(tree["resolved_links"]["documents"], {"target": {"title": "New | title"}})
        self.assertEqual(tree["resolved_links"]["blocks"][target_block]["text"], "Edited target")
        self.assertIn("New | title", [hit["text"] for hit in block_store.search_blocks(self.db, "title")][0])
        self.assertIn("[[target]]", export_document_markdown(self.db, "source"))
        self.assertEqual(len(block_store.document_backlinks(self.db, "target")), 3)


    # --- Living documents ----------------------------------------------------

    def _page_id(self, external_id="{PAGE-1}"):
        with connection_scope(self.db) as connection:
            return connection.execute(
                "SELECT id FROM documents WHERE source_external_id = ?", (external_id,)
            ).fetchone()["id"]

    def test_first_import_creates_the_note_with_source_dates_and_provenance(self) -> None:
        result = self.page([
            SourceBlock("First", external_id="{A}", created_at="2020-01-01T09:05:00+00:00"),
            SourceBlock("Second", external_id="{B}"),
        ], folder_path=[("{NB}", "Work"), ("{SEC}", "Meetings")])
        self.assertEqual(result.status, "created")
        self.assertEqual(result.blocks_created, 2)
        tree = document_tree(self.db, result.document_id)
        self.assertEqual(tree["created_at"], "2020-01-01T09:00:00+00:00")
        self.assertEqual(tree["updated_at"], "2020-01-02T09:00:00+00:00")
        self.assertEqual(tree["provenance"]["external_id"], "{PAGE-1}")
        self.assertEqual(tree["provenance"]["importer"], ONENOTE)
        self.assertIn("imported_at", tree["provenance"])
        first = tree["children"][0]
        self.assertEqual(first["created_at"], "2020-01-01T09:05:00+00:00")
        self.assertEqual(first["provenance"]["external_id"], "{A}")
        info = document_info(self.db, result.document_id)
        self.assertEqual([item["name"] for item in info["folder_path"]], ["Work", "Meetings"])

    def test_reimport_is_idempotent(self) -> None:
        blocks = [SourceBlock("First", external_id="{A}"), SourceBlock("Second")]
        first = self.page(blocks)
        before = document_tree(self.db, first.document_id)
        second = self.page(blocks)
        self.assertEqual((second.status, second.document_id), ("unchanged", first.document_id))
        after = document_tree(self.db, first.document_id)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["children"], before["children"])
        self.assertEqual(after["provenance"], before["provenance"])
        with connection_scope(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_untouched_blocks_follow_the_source_and_ids_links_history_survive(self) -> None:
        first = self.page([
            SourceBlock("Keep me", external_id="{A}"),
            SourceBlock("Old text", external_id="{B}"),
            SourceBlock("Dropped", external_id="{C}"),
        ])
        tree = document_tree(self.db, first.document_id)
        ids = {node["text"]: node["id"] for node in tree["children"]}
        # Another note links to the page and to one of its blocks.
        create_document(self.db, "linker", "Linker", "rough", "", "t", "t")
        apply_transaction(self.db, "linker", [BlockOperation(
            operation="insert", block_id="01J00000000000000000000005", type="paragraph",
            content=[{"type": "docLink", "id": first.document_id}, {"type": "text", "text": " "},
                     {"type": "blockRef", "id": ids["Old text"]}],
        )], 0)

        result = self.page([
            SourceBlock("New first", external_id="{N}"),
            SourceBlock("Keep me", external_id="{A}"),
            SourceBlock("New text", external_id="{B}"),
        ], modified_at="2020-02-01T00:00:00+00:00")
        self.assertEqual(result.status, "updated")
        self.assertEqual(
            (result.blocks_created, result.blocks_updated, result.blocks_deleted), (1, 1, 1)
        )
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(_texts(tree["children"]), ["New first", "Keep me", "New text"])
        self.assertEqual(tree["children"][2]["id"], ids["Old text"])
        self.assertEqual(tree["children"][2]["revision"], tree["revision"])
        self.assertEqual(len(block_store.document_backlinks(self.db, first.document_id)), 2)
        self.assertEqual(document_tree(self.db, "linker")["children"][0]["text"], "Meeting New text")
        self.assertTrue(block_store.fts_is_consistent(self.db))
        self.assertEqual(len(block_store.search_blocks(self.db, "Keep me")), 1)
        self.assertEqual(tree["provenance"]["source_modified_at"], "2020-02-01T00:00:00+00:00")
        self.assertIn("synced_at", tree["provenance"])

        # The whole re-import is one undo step.
        tree = undo_transaction(self.db, first.document_id, tree["revision"])
        self.assertEqual(_texts(tree["children"]), ["Keep me", "Old text", "Dropped"])
        self.assertEqual(tree["children"][1]["id"], ids["Old text"])
        tree = redo_transaction(self.db, first.document_id, tree["revision"])
        self.assertEqual(_texts(tree["children"]), ["New first", "Keep me", "New text"])


    def test_local_edits_win_and_conflicts_are_recorded_then_resolved(self) -> None:
        first = self.page([SourceBlock("Shared", external_id="{A}"), SourceBlock("Other", external_id="{B}")])
        tree = document_tree(self.db, first.document_id)
        block_id = tree["children"][0]["id"]
        tree = apply_transaction(self.db, first.document_id, [
            BlockOperation(operation="update", block_id=block_id, content=_runs("Shared, edited here")),
        ], tree["revision"])

        # The source didn't change this block: nothing to report.
        same = self.page([SourceBlock("Shared", external_id="{A}"), SourceBlock("Other", external_id="{B}")])
        self.assertEqual(same.status, "unchanged")
        self.assertEqual(same.conflicts, [])

        changed = self.page([SourceBlock("Shared, edited in OneNote", external_id="{A}"),
                             SourceBlock("Other", external_id="{B}")])
        self.assertEqual(changed.status, "updated")
        self.assertEqual([item["block_id"] for item in changed.conflicts], [block_id])
        tree = document_tree(self.db, first.document_id)
        node = tree["children"][0]
        self.assertEqual(node["text"], "Shared, edited here")
        self.assertNotIn("conflict", node["provenance"])  # sync state is not provenance
        info = document_info(self.db, first.document_id)
        self.assertEqual(info["conflicts"][0]["kind"], "block")
        self.assertEqual(info["conflicts"][0]["local_text"], "Shared, edited here")
        self.assertEqual(info["conflicts"][0]["source_text"], "Shared, edited in OneNote")
        self.assertEqual(info["conflicts"][0]["source"]["type"], "paragraph")
        self.assertEqual(info["sync"]["blocks_edited_here"], 1)

        # Same source version again: still one conflict, not a new one.
        again = self.page([SourceBlock("Shared, edited in OneNote", external_id="{A}"),
                           SourceBlock("Other", external_id="{B}")])
        self.assertEqual((again.status, again.conflicts), ("unchanged", []))

        tree = sources.resolve_block_conflict(self.db, first.document_id, block_id, "source")
        node = tree["children"][0]
        self.assertEqual(node["text"], "Shared, edited in OneNote")
        self.assertEqual(document_info(self.db, first.document_id)["conflicts"], [])
        self.assertEqual(document_info(self.db, first.document_id)["sync"]["blocks_edited_here"], 0)
        # Now untouched again: the next source change applies.
        self.page([SourceBlock("Third version", external_id="{A}"), SourceBlock("Other", external_id="{B}")])
        self.assertEqual(document_tree(self.db, first.document_id)["children"][0]["text"], "Third version")


    def test_keeping_the_local_version_silences_that_source_version(self) -> None:
        first = self.page([SourceBlock("Shared", external_id="{A}")])
        tree = document_tree(self.db, first.document_id)
        block_id = tree["children"][0]["id"]
        apply_transaction(self.db, first.document_id, [
            BlockOperation(operation="update", block_id=block_id, content=_runs("Mine")),
        ], tree["revision"])
        self.page([SourceBlock("Theirs", external_id="{A}")])
        sources.resolve_block_conflict(self.db, first.document_id, block_id, "local")
        self.assertEqual(document_info(self.db, first.document_id)["conflicts"], [])
        self.assertEqual(self.page([SourceBlock("Theirs", external_id="{A}")]).conflicts, [])
        newer = self.page([SourceBlock("Theirs v2", external_id="{A}")])
        self.assertEqual(len(newer.conflicts), 1)
        self.assertEqual(document_tree(self.db, first.document_id)["children"][0]["text"], "Mine")


    def test_local_additions_stay_put_and_local_deletions_stick(self) -> None:
        first = self.page([
            SourceBlock("A", external_id="{A}"),
            SourceBlock("B", external_id="{B}"),
            SourceBlock("C", external_id="{C}"),
        ])
        tree = document_tree(self.db, first.document_id)
        a, b, c = (node["id"] for node in tree["children"])
        tree = apply_transaction(self.db, first.document_id, [
            _paragraph("mine after A", block_id="01J00000000000000000000006", after_id=a),
            _paragraph("mine under C", block_id="01J00000000000000000000007", parent_id=c),
            BlockOperation(operation="delete", block_id=b),
        ], tree["revision"])
        result = self.page([
            SourceBlock("Z", external_id="{Z}"),
            SourceBlock("C", external_id="{C}"),
            SourceBlock("A", external_id="{A}"),
            SourceBlock("B changed", external_id="{B}"),
        ])
        self.assertEqual(result.blocks_skipped, 1)
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(_texts(tree["children"]), ["Z", "C", "A", "mine after A"])
        self.assertEqual(_texts(tree["children"][1]["children"]), ["mine under C"])


    def test_blocks_removed_from_the_source_go_unless_edited_here(self) -> None:
        first = self.page([
            SourceBlock("Stay", external_id="{A}"),
            SourceBlock("Untouched", external_id="{B}"),
            SourceBlock("Edited", external_id="{C}"),
        ])
        tree = document_tree(self.db, first.document_id)
        edited = tree["children"][2]["id"]
        apply_transaction(self.db, first.document_id, [
            BlockOperation(operation="update", block_id=edited, content=_runs("Edited locally")),
        ], tree["revision"])
        self.page([SourceBlock("Stay", external_id="{A}")])
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(_texts(tree["children"]), ["Stay", "Edited locally"])
        self.assertEqual(
            document_info(self.db, first.document_id)["sync"]["blocks_removed_in_source"], [edited]
        )


    def test_nested_source_blocks_and_markdown_bodies(self) -> None:
        result = self.page("# Title\n\n- Parent\n  - Child\n\nTail")
        tree = document_tree(self.db, result.document_id)
        self.assertEqual(_texts(tree["children"]), ["Title", "Parent", "Tail"])
        self.assertEqual(_texts(tree["children"][1]["children"]), ["Child"])
        child_id = tree["children"][1]["children"][0]["id"]
        # Content-derived ids: an unchanged block keeps its Fortress id.
        self.assertEqual(self.page("# Title\n\n- Parent\n  - Child\n\nTail").status, "unchanged")
        self.page("# Title\n\n- Parent\n  - Child\n\nNew tail")
        tree = document_tree(self.db, result.document_id)
        self.assertEqual(tree["children"][1]["children"][0]["id"], child_id)
        self.assertEqual(_texts(tree["children"]), ["Title", "Parent", "New tail"])


    def test_title_and_folder_follow_the_source_unless_changed_here(self) -> None:
        first = self.page([SourceBlock("x", external_id="{A}")],
                          folder_path=[("{NB}", "Work"), ("{S1}", "Section 1")])
        self.page([SourceBlock("x", external_id="{A}")], title="Meeting v2",
                  folder_path=[("{NB}", "Work"), ("{S2}", "Section 2")])
        info = document_info(self.db, first.document_id)
        self.assertEqual(info["title"], "Meeting v2")
        self.assertEqual([item["name"] for item in info["folder_path"]], ["Work", "Section 2"])

        rename_document(self.db, first.document_id, "My name")
        block_store.move_document(self.db, first.document_id, None)
        result = self.page([SourceBlock("x", external_id="{A}")], title="Meeting v3",
                           folder_path=[("{NB}", "Work (renamed)"), ("{S1}", "Section 1")])
        self.assertEqual(result.conflicts[0]["field"], "title")
        info = document_info(self.db, first.document_id)
        self.assertEqual(info["title"], "My name")
        self.assertEqual(info["folder_path"], [])
        self.assertEqual(info["conflicts"][0]["source_title"], "Meeting v3")
        # The notebook folder followed the source's rename (no local rename).
        folders = block_store.navigation(self.db)["items"]
        self.assertIn("Work (renamed)", [item.get("name") for item in folders])

    def test_folder_paths_find_folders_by_source_id_after_local_changes(self) -> None:
        folder = sources.ensure_folder_path(self.db, "onenote_import", [("{NB}", "Work")])
        block_store.rename_folder(self.db, folder, "Job")
        again = sources.ensure_folder_path(
            self.db, "onenote_import", [{"external_id": "{NB}", "name": "Work 2"}]
        )
        self.assertEqual(again, folder)
        self.assertEqual(block_store.navigation(self.db)["items"][0]["name"], "Job")

    def test_trashed_notes_are_not_resurrected_but_purged_ones_are_reimported(self) -> None:
        from app import workspace

        first = self.page([SourceBlock("x", external_id="{A}")])
        block_store.delete_document(self.db, first.document_id)
        self.assertEqual(self.page([SourceBlock("y", external_id="{A}")]).status, "skipped_deleted")
        workspace.purge_document(self.db, first.document_id)
        again = self.page([SourceBlock("y", external_id="{A}")])
        self.assertEqual(again.status, "created")
        self.assertNotEqual(again.document_id, first.document_id)

    # --- Metadata -------------------------------------------------------------

    def test_document_info_counts(self) -> None:
        create_document(self.db, "a", "A", "rough", "Hello world, it's me.\n\nSecond block", "t", "t")
        create_document(self.db, "b", "B", "rough", "", "t", "t")
        a_block = document_tree(self.db, "a")["children"][0]["id"]
        apply_transaction(self.db, "b", [BlockOperation(
            operation="insert", block_id="01J00000000000000000000008", type="paragraph",
            content=[{"type": "docLink", "id": "a"}, {"type": "text", "text": " and "},
                     {"type": "blockRef", "id": a_block}],
        )], 0)
        apply_transaction(self.db, "a", [BlockOperation(
            operation="insert", block_id="01J00000000000000000000009", type="paragraph",
            content=[{"type": "docLink", "id": "b"}],
        )], 0)
        info = document_info(self.db, "a")
        # Hello world it's me / Second block / B (a link counts as its label).
        self.assertEqual(info["counts"]["words"], 7)
        self.assertEqual(info["counts"]["blocks"], 3)
        self.assertEqual(info["counts"]["outgoing_links"], 1)
        self.assertEqual(info["counts"]["backlinks"], 2)
        self.assertEqual(info["counts"]["edits"], 1)
        self.assertEqual(info["blocks_by_source"], {"manual": 3})
        self.assertEqual(info["provenance"], {"source": "manual"})
        self.assertIsNone(info["sync"])
        self.assertEqual(info["conflicts"], [])
        with self.assertRaises(KeyError):
            document_info(self.db, "missing")


    # --- Migration and backups -------------------------------------------------

    def test_migration_backfills_capture_markdown_import_and_manual(self) -> None:
        # A schema-12 database written by the code of that time.
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(
                (Path(__file__).parent / "fixtures" / "legacy_v12.sql").read_text(encoding="utf-8")
            )
        backup = self.root / ".fortress-import-backups" / "20260101T000000000000Z"
        backup.mkdir(parents=True)
        (backup / "imported.md").write_text("x", encoding="utf-8")

        initialize(self.db)
        sources_by_id = {
            item["id"]: item["source"] for item in block_store.list_documents(self.db)
        }
        self.assertEqual(
            sources_by_id, {"captured": "capture", "imported": "markdown_import", "written": "manual"}
        )
        tree = document_tree(self.db, "captured")
        self.assertEqual(tree["provenance"], {"source": "capture", "backfilled": True})
        self.assertEqual(tree["children"][0]["provenance"]["source"], "capture")
        with connection_scope(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 14
            )



    def test_backup_and_restore_keep_provenance(self) -> None:
        first = self.page([SourceBlock("Saved", external_id="{A}")])
        directory = self.root / "backups"
        snapshot = backups.create_backup(self.db, directory, "manual")
        block_store.delete_document(self.db, first.document_id)
        backups.restore_backup(self.db, directory, snapshot.name)
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(tree["provenance"]["external_id"], "{PAGE-1}")
        self.assertEqual(tree["children"][0]["provenance"]["external_id"], "{A}")
        # And the restored note is still the one re-imports update.
        self.assertEqual(self.page([SourceBlock("Saved v2", external_id="{A}")]).document_id,
                         first.document_id)


class ProvenanceApiTests(unittest.TestCase):
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

    def test_capture_and_ai_notes_carry_their_source(self) -> None:
        captured = self.client.post(
            "/api/capture",
            json={"text": "A thought", "provenance": {"source_url": "https://example.com/x"}},
        ).json()
        tree = self.client.get(f"/api/block-documents/{captured['document_id']}").json()
        self.assertEqual(tree["provenance"], {"source": "capture", "source_url": "https://example.com/x"})
        self.assertEqual(
            self.client.post(f"/api/block-documents/{captured['document_id']}/opened").status_code,
            204,
        )
        info = self.client.get(f"/api/block-documents/{captured['document_id']}/info").json()
        self.assertIsNotNone(info["last_opened_at"])
        navigation = self.client.get("/api/navigation").json()
        self.assertEqual(navigation["recent"][0]["source"], "capture")

        answer = self.client.post("/api/block-documents", json={
            "title": "Answer", "body": "The answer",
            "provenance": {"source": "ai", "extra": {"question": "What?"}},
        })
        self.assertEqual(answer.status_code, 201)
        self.assertEqual(answer.json()["provenance"]["source"], "ai")
        bad = self.client.post("/api/block-documents", json={"provenance": {"source": "BAD"}})
        self.assertEqual(bad.status_code, 422)

    def test_info_endpoint_shape(self) -> None:
        created = self.client.post("/api/block-documents", json={"title": "N", "body": "one two"}).json()
        info = self.client.get(f"/api/block-documents/{created['id']}/info")
        self.assertEqual(info.status_code, 200)
        body = info.json()
        self.assertEqual(
            set(body),
            {"id", "title", "status", "created_at", "updated_at", "last_opened_at", "revision",
             "folder_id", "folder_path", "provenance", "counts", "blocks_by_source", "can_undo",
             "can_redo", "sync", "conflicts", "picture_text"},
        )
        self.assertEqual(body["counts"]["words"], 2)
        self.assertEqual(self.client.get("/api/block-documents/nope/info").status_code, 404)

    def test_uploaded_markdown_restores_exported_provenance_and_mirrors_carry_it(self) -> None:
        from app import documents

        result = documents.upsert_from_source(
            source="onenote_import", external_id="{P}", title="Page",
            blocks=[SourceBlock("Para", external_id="{B}")],
        )
        exported = self.client.get(f"/api/block-documents/{result.document_id}/markdown").text
        documents.flush_mirrors()
        mirror = (self.notes / f"{result.document_id}.md").read_text(encoding="utf-8")
        self.assertIn("onenote_import", mirror)
        self.assertIn("fortress:block", mirror)
        response = self.client.post(
            "/api/markdown-import/files",
            files=[("files", ("Page.md", exported.encode(), "text/markdown"))],
        ).json()
        [imported] = response["imported"]
        tree = self.client.get(f"/api/block-documents/{imported['id']}").json()
        self.assertEqual(tree["provenance"]["source"], "onenote_import")
        self.assertEqual(tree["children"][0]["provenance"]["external_id"], "{B}")

    def test_conflict_endpoint(self) -> None:
        from app import documents

        result = documents.upsert_from_source(
            source="onenote_import", external_id="{P}", title="Page",
            blocks=[SourceBlock("Para", external_id="{B}")],
        )
        tree = self.client.get(f"/api/block-documents/{result.document_id}").json()
        block_id = tree["children"][0]["id"]
        response = self.client.post(
            f"/api/block-documents/{result.document_id}/blocks/{block_id}/source-conflict",
            json={"keep": "source"},
        )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
