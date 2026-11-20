"""Where notes came from, note info, and re-importing a source (a refresh)."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import backups, block_store, provenance, sources
from app.block_store import (
    apply_transaction,
    connection_scope,
    create_document,
    document_tree,
    rename_document,
)
from app.models import BlockOperation
from app.portability import export_document_markdown, import_markdown_directory
from app.sources import SourceBlock, document_info, upsert_document_from_source

from support import make_settings, use_settings, mirror_file


def _texts(nodes):
    return [node["text"] for node in nodes]


def _runs(text):
    return [{"type": "text", "text": text}]


def _paragraph(text, **fields):
    return BlockOperation(operation="insert", type="paragraph", content=_runs(text), **fields)


ONENOTE = {"name": "onenote", "version": "1.0"}


class SourceStoreTests(unittest.TestCase):
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

    def test_new_documents_default_to_manual_and_blocks_have_no_provenance(self) -> None:
        create_document(self.db, "doc", "Doc", "rough", "One\n\nTwo", "t", "t")
        tree = document_tree(self.db, "doc")
        self.assertEqual(tree["provenance"], {"source": "manual"})
        self.assertNotIn("provenance", tree["children"][0])
        self.assertIsNone(tree["children"][0]["revision"])
        self.assertNotIn("last_opened_at", tree)  # in /info; not part of the note's ETag

    def test_client_provenance_is_validated_and_cannot_set_internal_fields(self) -> None:
        with self.assertRaises(provenance.ProvenanceError):
            provenance.normalize({"source": "Not A Kind"})
        with self.assertRaises(provenance.ProvenanceError):
            provenance.normalize({"source": "web", "source_url": "javascript:alert(1)"})
        self.assertEqual(
            provenance.normalize({"source": "paste", "source_hash": "x", "conflict": {}}),
            {"source": "paste"},
        )

    def test_provenance_is_not_searchable_text(self) -> None:
        self.page([SourceBlock("Plain words", external_id="{A}")],
                  provenance={"source_path": "Zebraword › Section"})
        self.assertEqual(block_store.search_blocks(self.db, "Zebraword"), [])
        self.assertEqual(len(block_store.search_blocks(self.db, "Plain")), 1)
        self.assertTrue(block_store.fts_is_consistent(self.db))

    # --- Markdown round trip -----------------------------------------------

    def test_markdown_export_and_reimport_restore_document_provenance(self) -> None:
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
            _paragraph("Typed here", block_id="01J00000000000000000000003"),
        ], tree["revision"])
        markdown = export_document_markdown(self.db, document_id)
        self.assertIn("source: onenote_import", markdown)
        self.assertIn("external_id: '{PAGE-1}'", markdown)
        self.assertNotIn("fortress:block", markdown)  # blocks carry no provenance
        self.assertIn("  - Child", markdown)
        self.assertIn(f"\nid: {document_id}\n", markdown.split("---", 2)[1])
        # Nothing refers to these blocks: no ids in the body.
        self.assertNotIn("^01", markdown)
        self.assertNotIn('"id"', markdown)

        other = self.root / "other"
        other.mkdir()
        (other / "page.md").write_text(markdown, encoding="utf-8")
        target = other / ".fortress.sqlite3"
        import_markdown_directory(target, other)
        # The note keeps its id (front matter "id").
        restored = document_tree(target, document_id)
        self.assertEqual(restored["provenance"]["source"], "onenote_import")
        self.assertEqual(restored["provenance"]["external_id"], "{PAGE-1}")
        self.assertEqual(restored["provenance"]["source_path"], "Work › Meetings › Meeting")
        self.assertEqual(_texts(restored["children"]), ["Agenda", "Item", "code", "Typed here"])
        self.assertEqual(restored["children"][1]["children"][0]["text"], "Child")
        # No comment line became a block, and a second export is identical.
        self.assertEqual(export_document_markdown(target, document_id), markdown)

        # Re-importing the export into a database that has the source page reuses
        # nothing: the copy gives up the source id instead of stealing it.
        (other / "copy.md").write_text(markdown.replace(f"id: {document_id}\n", ""), encoding="utf-8")
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
        node = tree["children"][1]  # after the empty paragraph a new note starts with
        # Links are ids; their text is the target's current title and text.
        self.assertEqual(node["text"], "See New | title and Edited target or custom words")
        self.assertEqual(node["content"][1], {"type": "docLink", "id": "target"})
        self.assertEqual(tree["children"][2]["text"], "Plain New | title")
        self.assertEqual(tree["resolved_links"]["documents"], {"target": {"title": "New | title"}})
        self.assertEqual(tree["resolved_links"]["blocks"][target_block]["text"], "Edited target")
        self.assertIn("New | title", [hit["text"] for hit in block_store.search_blocks(self.db, "title")][0])
        # Wikilinks by file name (a "|" can't be in one), labelled with the title.
        exported = export_document_markdown(self.db, "source")
        self.assertIn("See [[New title]] and [[New title#^", exported)
        self.assertIn("|Edited target]] or [custom words](https://example.com)", exported)
        self.assertEqual(len(block_store.document_backlinks(self.db, "target")), 3)


    # --- Living documents ----------------------------------------------------

    def _page_id(self, external_id="{PAGE-1}"):
        with connection_scope(self.db) as connection:
            return connection.execute(
                "SELECT id FROM documents WHERE source_external_id = ?", (external_id,)
            ).fetchone()["id"]

    def test_first_import_creates_the_note_with_source_dates_and_provenance(self) -> None:
        result = self.page([
            SourceBlock("First", external_id="{A}"),
            SourceBlock("Second", external_id="{B}"),
        ], folder_path=[("{NB}", "Work"), ("{SEC}", "Meetings")])
        self.assertEqual((result.status, result.blocks_created), ("created", 2))
        tree = document_tree(self.db, result.document_id)
        self.assertEqual(tree["created_at"], "2020-01-01T09:00:00+00:00")
        self.assertEqual(tree["updated_at"], "2020-01-02T09:00:00+00:00")
        self.assertEqual(tree["provenance"]["external_id"], "{PAGE-1}")
        self.assertEqual(tree["provenance"]["importer"], ONENOTE)
        self.assertIn("imported_at", tree["provenance"])
        info = document_info(self.db, result.document_id)
        self.assertEqual(info["source"], "onenote_import")
        self.assertEqual([item["name"] for item in info["folder_path"]], ["Work", "Meetings"])

    def test_an_unchanged_page_is_a_no_op(self) -> None:
        blocks = [SourceBlock("First", external_id="{A}"), SourceBlock("Second")]
        first = self.page(blocks)
        before = document_tree(self.db, first.document_id)
        second = self.page(blocks)
        self.assertEqual((second.status, second.document_id), ("unchanged", first.document_id))
        after = document_tree(self.db, first.document_id)
        self.assertEqual(after, before)
        with connection_scope(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_a_changed_page_is_refreshed_keeping_matching_block_ids_and_links(self) -> None:
        first = self.page([
            SourceBlock("Keep me", external_id="{A}"),
            SourceBlock("Old text", external_id="{B}", children=[SourceBlock("Child", external_id="{K}")]),
            SourceBlock("Dropped", external_id="{C}"),
        ])
        tree = document_tree(self.db, first.document_id)
        ids = {node["text"]: node["id"] for node in tree["children"]}
        child = tree["children"][1]["children"][0]["id"]
        # Another note links to the page, to one of its blocks and to a nested one.
        create_document(self.db, "linker", "Linker", "rough", "", "t", "t")
        apply_transaction(self.db, "linker", [BlockOperation(
            operation="insert", block_id="01J00000000000000000000005", type="paragraph",
            content=[{"type": "docLink", "id": first.document_id}, {"type": "text", "text": " "},
                     {"type": "blockRef", "id": ids["Old text"]}, {"type": "text", "text": " "},
                     {"type": "blockRef", "id": child}],
        )], 0)
        folder = block_store.create_folder(self.db, "Mine")
        block_store.move_document(self.db, first.document_id, folder["id"])

        result = self.page([
            SourceBlock("New first", external_id="{N}"),
            SourceBlock("Keep me", external_id="{A}", children=[SourceBlock("Child v2", external_id="{K}")]),
            SourceBlock("New text", external_id="{B}"),
        ], title="Meeting v2", modified_at="2020-02-01T00:00:00+00:00",
            folder_path=[("{NB}", "Elsewhere")])
        self.assertEqual(result.status, "updated")
        self.assertEqual((result.blocks_created, result.blocks_updated, result.blocks_deleted),
                         (1, 2, 1))
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(_texts(tree["children"]), ["New first", "Keep me", "New text"])
        self.assertEqual(tree["children"][1]["id"], ids["Keep me"])
        self.assertEqual(tree["children"][2]["id"], ids["Old text"])
        self.assertEqual(tree["children"][2]["revision"], tree["revision"])
        # The nested block moved under another parent and kept its id.
        self.assertEqual(tree["children"][1]["children"][0]["id"], child)
        # The source's title; the note's own folder.
        self.assertEqual(tree["title"], "Meeting v2")
        self.assertEqual(document_info(self.db, first.document_id)["folder_id"], folder["id"])
        self.assertEqual(tree["provenance"]["source_modified_at"], "2020-02-01T00:00:00+00:00")
        self.assertIn("synced_at", tree["provenance"])
        # Links into the refreshed blocks still resolve, and show the new text.
        self.assertEqual(len(block_store.document_backlinks(self.db, first.document_id)), 3)
        self.assertEqual(document_tree(self.db, "linker")["children"][-1]["text"],
                         "Meeting v2 New text Child v2")
        self.assertTrue(block_store.fts_is_consistent(self.db))
        self.assertEqual(len(block_store.search_blocks(self.db, "Keep me")), 1)
        self.assertEqual(self.page([
            SourceBlock("New first", external_id="{N}"),
            SourceBlock("Keep me", external_id="{A}", children=[SourceBlock("Child v2", external_id="{K}")]),
            SourceBlock("New text", external_id="{B}"),
        ], title="Meeting v2").status, "unchanged")

    def test_a_page_edited_here_is_not_refreshed(self) -> None:
        first = self.page([SourceBlock("Shared", external_id="{A}"), SourceBlock("Other", external_id="{B}")])
        tree = document_tree(self.db, first.document_id)
        apply_transaction(self.db, first.document_id, [
            _paragraph("Mine", block_id="01J00000000000000000000006"),
        ], tree["revision"])
        # Source unchanged: nothing to do.
        self.assertEqual(self.page([SourceBlock("Shared", external_id="{A}"),
                                    SourceBlock("Other", external_id="{B}")]).status, "unchanged")
        result = self.page([SourceBlock("Shared, edited in OneNote", external_id="{A}")])
        self.assertEqual(result.status, "skipped_edited")
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(_texts(tree["children"]), ["Shared", "Other", "Mine"])
        self.assertEqual(tree["revision"], 1)

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
        self.assertEqual(info["source"], "manual")
        with self.assertRaises(KeyError):
            document_info(self.db, "missing")


    # --- Migration and backups -------------------------------------------------



    def test_backup_and_restore_keep_provenance(self) -> None:
        first = self.page([SourceBlock("Saved", external_id="{A}")])
        directory = self.root / "backups"
        snapshot = backups.create_backup(self.db, directory, "manual")
        block_store.delete_document(self.db, first.document_id)
        backups.restore_backup(self.db, directory, snapshot.name)
        tree = document_tree(self.db, first.document_id)
        self.assertEqual(tree["provenance"]["external_id"], "{PAGE-1}")
        # And the restored note is still the one re-imports refresh, by the same block id.
        self.assertEqual(self.page([SourceBlock("Saved v2", external_id="{A}")]).document_id,
                         first.document_id)
        self.assertEqual(document_tree(self.db, first.document_id)["children"][0]["id"],
                         tree["children"][0]["id"])


class SourceApiTests(unittest.TestCase):
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
             "folder_id", "folder_path", "source", "counts", "picture_text"},
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
        mirror = mirror_file(self.notes, result.document_id).read_text(encoding="utf-8")
        self.assertIn("onenote_import", mirror)
        self.assertNotIn("fortress:block", mirror)  # blocks carry no provenance
        response = self.client.post(
            "/api/markdown-import/files",
            files=[("files", ("Page.md", exported.encode(), "text/markdown"))],
        ).json()
        [imported] = response["imported"]
        tree = self.client.get(f"/api/block-documents/{imported['id']}").json()
        self.assertEqual(tree["provenance"]["source"], "onenote_import")
        self.assertEqual(tree["children"][0]["text"], "Para")


if __name__ == "__main__":
    unittest.main()
