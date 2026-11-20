"""The notes folder as an Obsidian vault (app/vault.py, documents.MirrorWriter)."""

import shutil
import tempfile
import unittest
from pathlib import Path

import frontmatter
from fastapi.testclient import TestClient

from app import block_store, documents, vault
from app.block_store import apply_transaction, document_tree
from app.models import BlockOperation, NoteCreate
from support import import_markdown_files, make_settings, mirror_file, use_settings


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


class LayoutTests(unittest.TestCase):
    def test_names_folders_clashes_and_link_targets(self) -> None:
        folders = [
            {"id": "f1", "parent_id": None, "name": "Work", "position": 0},
            {"id": "f2", "parent_id": "f1", "name": "Q3: plans", "position": 0},
            {"id": "f3", "parent_id": None, "name": "assets", "position": 1},
            {"id": "f4", "parent_id": None, "name": "work", "position": 2},
        ]
        documents_ = [
            {"id": "a", "title": "Plans", "folder_id": None, "created_at": "1"},
            {"id": "b", "title": "plans", "folder_id": None, "created_at": "2"},
            {"id": "c", "title": "Plans", "folder_id": "f2", "created_at": "3"},
            {"id": "d", "title": "What? #1 [draft]", "folder_id": "f3", "created_at": "4"},
            {"id": "e", "title": "", "folder_id": "f4", "created_at": "5"},
        ]
        layout = vault.compute_layout(documents_, folders)
        self.assertEqual(layout.paths, {
            "a": "Plans.md", "b": "plans 2.md", "c": "Work/Q3 plans/Plans.md",
            "d": "assets 2/What 1 draft.md", "e": "work 2/Untitled.md",
        })
        # The shortest name that is unique in the vault, as Obsidian links.
        self.assertEqual(layout.targets["a"], "Plans")
        self.assertEqual(layout.targets["c"], "Work/Q3 plans/Plans")
        self.assertEqual(layout.targets["b"], "plans 2")
        self.assertEqual(layout.media_prefix("c"), "../../assets/")
        self.assertEqual(layout.media_prefix("a"), "assets/")

    def test_someone_elses_file_is_never_taken(self) -> None:
        layout = vault.compute_layout(
            [{"id": "a", "title": "Ideas", "folder_id": None, "created_at": "1"}], [],
            taken=lambda path, _id: path == "Ideas.md",
        )
        self.assertEqual(layout.paths["a"], "Ideas 2.md")


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.notes = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = make_settings(self.notes)
        self.enterContext(use_settings(self.settings))
        self.db = self.settings.block_db_path

    def mirror(self, document_id: str) -> str:
        documents.flush_mirrors()
        return mirror_file(self.notes, document_id).read_text(encoding="utf-8")


class VaultMirrorTests(_Workspace):
    def test_long_titles_in_other_scripts_fit_a_file_name(self) -> None:
        # 100 CJK characters are 300 bytes, over the 255-byte file name limit: the
        # mirror pass used to fail on that note, and with it every other mirror.
        long_title = documents.create(NoteCreate(title="会議" * 60, body="内容"))
        emoji = documents.create(NoteCreate(title="🎉" * 80, body="party"))
        plain = documents.create(NoteCreate(title="Plain", body="still mirrored"))
        self.assertIn("still mirrored", self.mirror(plain["id"]))
        for note in (long_title, emoji):
            path = mirror_file(self.notes, note["id"])
            self.assertTrue(path.is_file(), path)
            self.assertLessEqual(len(path.name.encode("utf-8")), 255)

    def make_linked_notes(self):
        target = documents.create(NoteCreate(title="Roadmap", body="# Q3 goals\n\nShip the beta.\n\nOther"))
        heading, goal, _other = target["children"]
        source = documents.create(NoteCreate(title="Weekly", body="Intro"))
        tree = apply_transaction(self.db, source["id"], [
            BlockOperation(operation="insert", type="paragraph", content=[
                {"type": "text", "text": "See "}, {"type": "docLink", "id": target["id"]},
                {"type": "text", "text": ", "}, {"type": "blockRef", "id": goal["id"]},
                {"type": "text", "text": " and "}, {"type": "blockRef", "id": heading["id"]},
            ]),
            BlockOperation(operation="insert", type="paragraph", content=[
                {"type": "text", "text": "Mark", "marks": {"highlight": "yellow"}},
            ]),
            BlockOperation(operation="insert", type="callout", attrs={"icon": "💡"}),
            BlockOperation(operation="insert", type="image",
                           attrs={"url": "/media/01J00000000000000000000AB1.png", "alt": "chart",
                                  "width": 300}),
        ], source["revision"])
        callout = tree["children"][3]
        apply_transaction(self.db, source["id"], [
            BlockOperation(operation="insert", type="paragraph", parent_id=callout["id"],
                           content=[{"type": "text", "text": "Inside"}]),
        ], tree["revision"])
        return target, goal, heading, source

    def test_the_mirror_is_plain_obsidian_markdown(self) -> None:
        target, goal, heading, source = self.make_linked_notes()
        weekly = self.mirror(source["id"])
        self.assertEqual(mirror_file(self.notes, source["id"]), self.notes / "Weekly.md")
        post = frontmatter.loads(weekly)
        # Plain keys only: nothing Fortress-only (provenance, block metadata) is written.
        self.assertEqual(list(post.metadata), ["title", "created", "updated", "id"])
        self.assertEqual(post.content, (
            "Intro\n\n"
            f"See [[Roadmap]], [[Roadmap#^{goal['id']}|Ship the beta.]] and [[Roadmap#Q3 goals|Q3 goals]]\n\n"
            "==Mark==\n\n"
            "> [!note]\n> Inside\n\n"
            "![chart|300](assets/01J00000000000000000000AB1.png)"
        ))
        roadmap = frontmatter.loads(self.mirror(target["id"])).content
        # Only the blocks something refers to carry an id (a heading is linked by its text).
        self.assertEqual(roadmap, f"# Q3 goals\n\nShip the beta. ^{goal['id']}\n\nOther")

    def test_renames_and_moves_rename_files_and_rewrite_the_links_to_them(self) -> None:
        target, _goal, _heading, source = self.make_linked_notes()
        documents.flush_mirrors()
        documents.update(target["id"], documents.NoteUpdate(title="Plan: 2027"))
        self.assertIn("See [[Plan 2027|Plan: 2027]]", self.mirror(source["id"]))
        self.assertFalse((self.notes / "Roadmap.md").exists())
        self.assertTrue((self.notes / "Plan 2027.md").exists())
        self.assertEqual(frontmatter.loads(self.mirror(target["id"])).metadata["aliases"], ["Plan: 2027"])

        folder = block_store.create_folder(self.db, "Projects")
        block_store.move_document(self.db, target["id"], folder["id"], position=0)
        self.mirror(target["id"])
        self.assertEqual(mirror_file(self.notes, target["id"]), self.notes / "Projects" / "Plan 2027.md")
        block_store.rename_folder(self.db, folder["id"], "Archive")
        self.mirror(target["id"])
        self.assertEqual(mirror_file(self.notes, target["id"]), self.notes / "Archive" / "Plan 2027.md")
        self.assertFalse((self.notes / "Projects").exists())  # emptied folders go

        # A second note with the same name: links use the path to tell them apart.
        documents.create(NoteCreate(title="Plan: 2027", body="other"))
        self.assertIn("See [[Archive/Plan 2027|Plan: 2027]]", self.mirror(source["id"]))

    def test_a_newly_referenced_block_gets_its_id_in_its_own_note(self) -> None:
        target = documents.create(NoteCreate(title="Target", body="Quote me"))
        self.assertEqual(frontmatter.loads(self.mirror(target["id"])).content, "Quote me")
        source = documents.create(NoteCreate(title="Source", body=""))
        block = target["children"][0]["id"]
        apply_transaction(self.db, source["id"], [
            BlockOperation(operation="update", block_id=source["children"][0]["id"],
                           content=[{"type": "blockRef", "id": block}]),
        ], source["revision"])
        self.assertEqual(frontmatter.loads(self.mirror(target["id"])).content, f"Quote me ^{block}")

    def test_someone_elses_file_in_the_folder_is_left_alone(self) -> None:
        (self.notes / "Ideas.md").write_text("mine", encoding="utf-8")
        note = documents.create(NoteCreate(title="Ideas", body="theirs"))
        self.mirror(note["id"])
        self.assertEqual((self.notes / "Ideas.md").read_text(encoding="utf-8"), "mine")
        self.assertEqual(mirror_file(self.notes, note["id"]), self.notes / "Ideas 2.md")

    def test_the_vault_reads_back_as_plain_markdown(self) -> None:
        target, goal, heading, source = self.make_linked_notes()
        documents.flush_mirrors()
        copy = self.notes.parent / f"{self.notes.name}-copy"
        self.addCleanup(shutil.rmtree, copy, True)
        shutil.copytree(self.notes, copy, ignore=shutil.ignore_patterns(".fortress*", "assets"))
        database = copy / ".fortress.sqlite3"
        imported = {
            document_tree(database, document_id)["title"]: document_tree(database, document_id)
            for document_id in import_markdown_files(database, copy)
        }
        for original in (target, source):
            before = document_tree(self.db, original["id"])
            after = imported[before["title"]]
            self.assertEqual([(node["type"], node["text"]) for node in _walk(after["children"])],
                             [(node["type"], node["text"]) for node in _walk(before["children"])])
        # The links between the notes are links again.
        roadmap = imported["Roadmap"]
        self.assertEqual(len(block_store.document_backlinks(database, roadmap["id"])), 3)


class LinkedBodyTests(_Workspace):
    def test_wikilinks_typed_in_a_capture_link_to_existing_notes(self) -> None:
        target = documents.create(NoteCreate(title="Groceries", body="milk"))
        captured = documents.capture("Add eggs to [[groceries]], see [[Nothing yet]]")
        [block] = document_tree(self.db, captured["document_id"])["children"]
        self.assertEqual(block["content"], [
            {"type": "text", "text": "Add eggs to "}, {"type": "docLink", "id": target["id"]},
            {"type": "text", "text": ", see [[Nothing yet]]"},
        ])


class UploadTests(_Workspace):
    def test_files_uploaded_together_link_to_each_other(self) -> None:
        from app import main

        client = self.enterContext(TestClient(main.app))
        existing = documents.create(NoteCreate(title="Already here", body="x"))
        response = client.post("/api/markdown-import/files", files=[
            ("files", ("Alpha.md", b"Hi [[Beta#^b1|beta]] and [[Already here]] [[beta]]", "text/markdown")),
            ("files", ("Beta.md", b"---\naliases: [B]\n---\nTarget ^b1\n\nBack to [[Alpha]]", "text/markdown")),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        ids = {item["name"]: item["id"] for item in response.json()["imported"]}
        alpha = document_tree(self.db, ids["Alpha.md"])
        beta = document_tree(self.db, ids["Beta.md"])
        self.assertEqual(alpha["children"][0]["content"], [
            {"type": "text", "text": "Hi "}, {"type": "blockRef", "id": beta["children"][0]["id"]},
            {"type": "text", "text": " and "}, {"type": "docLink", "id": existing["id"]},
            {"type": "text", "text": " "}, {"type": "docLink", "id": beta["id"]},
        ])
        self.assertEqual(beta["children"][1]["content"][1], {"type": "docLink", "id": alpha["id"]})


if __name__ == "__main__":
    unittest.main()
