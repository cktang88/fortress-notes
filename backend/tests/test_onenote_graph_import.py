"""End to end: tools/fortress-onenote-graph's output -> the OneNote import job.

The tool downloads from an in-memory Microsoft Graph (no network), writes its
manifest zip, and the zip goes through ``POST /api/import/onenote`` exactly as
the Mac app's File › Import › From OneNote… sends it -- without the Rust
converter (``manifest_input``). Then the notes and blocks are checked.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import block_store, onenote_import
from app.block_store import document_tree
from support import import_details, make_settings, use_settings

TOOL = Path(__file__).resolve().parents[2] / "tools" / "fortress-onenote-graph"
sys.path.insert(0, str(TOOL))
from fortress_onenote_graph.download import Downloader  # noqa: E402
from fortress_onenote_graph.graph import GraphClient  # noqa: E402
from fortress_onenote_graph.manifest import build  # noqa: E402
from fortress_onenote_graph.progress import Progress  # noqa: E402
from fortress_onenote_graph.store import Store  # noqa: E402
from fortress_onenote_graph.throttle import Throttle  # noqa: E402

_spec = importlib.util.spec_from_file_location("onenote_graph_fakes", TOOL / "tests" / "fakes.py")
fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakes)


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def _inline(node, kind):
    return [item for item in node.get("content") or [] if item.get("type") == kind]


class GraphImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.settings = make_settings(self.notes, reindex_interval_s=5.0)
        self.db = self.settings.block_db_path
        from app import main

        self.jobs = onenote_import.ImportJobs()

        def no_converter(*_args, **_kwargs):
            raise AssertionError("a manifest must not go through the Rust converter")

        for item in (
            use_settings(self.settings),
            patch.object(onenote_import, "jobs", self.jobs),
            patch.object(onenote_import, "run_converter", no_converter),
            patch.dict(os.environ, {"FORTRESS_TOKEN": "test-token"}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app, headers={"X-Fortress-Token": "test-token"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.graph = fakes.sample_graph()
        self.store = Store(self.root / "cache")

    def download(self, out: Path) -> dict:
        client = GraphClient(lambda force: "t", Throttle(clock=fakes.FakeClock()), transport=self.graph)
        Downloader(client, self.store, Progress(quiet=True, stream=io.StringIO())).run()
        return build(self.store, out)

    def upload(self, path: Path) -> dict:
        with path.open("rb") as handle:
            response = self.client.post("/api/import/onenote",
                                        files={"file": (path.name, handle, "application/zip")})
        self.assertEqual(response.status_code, 202, response.text)
        self.jobs.wait(60)
        job = self.client.get(f"/api/import/onenote/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "done", job)
        return job["result"]

    def tree(self, page_id: str) -> dict:
        with block_store.connection_scope(self.db) as connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE source_kind = 'onenote_import' AND source_external_id = ?",
                (page_id,)).fetchone()
        self.assertIsNotNone(row, page_id)
        return document_tree(self.db, row["id"])

    def test_download_then_import_through_the_job_api(self) -> None:
        archive = self.root / "OneNote-Graph.zip"
        summary = self.download(archive)
        self.assertEqual(summary["pages"], 3)
        result = self.upload(archive)
        [notebook] = result["notebooks"]
        self.assertEqual((notebook["name"], notebook["pages_created"], notebook["pages_skipped"]), ("Work", 3, 0))
        self.assertEqual(notebook["failed_sections"][0]["path"], "Work › Locked")
        self.assertEqual(notebook["columns_pages"], 1)
        self.assertEqual(notebook["tables"], 2)
        self.assertGreaterEqual(result["links"]["rewritten"], 3)

        # Folders: notebook › section, notebook › group › section, in Graph's order.
        navigation = block_store.navigation(self.db)["items"]
        [work] = [item for item in navigation if item["kind"] == "folder"]
        folders = {child["name"]: child for child in work["children"] if child["kind"] == "folder"}
        self.assertEqual(set(folders), {"Alpha", "Projects"})
        self.assertEqual([c["title"] for c in folders["Alpha"]["children"] if c["kind"] == "document"],
                         ["Project plan", "Child"])

        plan = self.tree("GRAPH:0-P1!1-ABC")
        nodes = list(_walk(plan["children"]))
        types = [node["type"] for node in nodes]
        for expected in ("heading", "check_item", "bullet_item", "numbered_item", "code", "columns", "image"):
            self.assertIn(expected, types)
        self.assertNotIn("math", types)
        checks = {node["text"]: node["attrs"].get("checked") for node in nodes if node["type"] == "check_item"}
        self.assertEqual(checks, {"Write the spec": False, "Book the room": True, "Bullet with tag": False})
        image = next(node for node in nodes if node["type"] == "image")
        self.assertEqual(image["attrs"]["alt"], "Quarterly revenue chart Q1 Q2")
        rich = next(node for node in nodes if node["text"].startswith("Bold italic"))
        doc_links = _inline(rich, "docLink")
        standup = self.tree("GRAPH:0-P3!1-ABC")
        self.assertTrue(doc_links)
        self.assertTrue(all(link["id"] == standup["id"] for link in doc_links))
        marks = {mark for item in rich["content"] for mark in (item.get("marks") or {})}
        self.assertTrue({"bold", "italic", "underline", "strike", "sup", "sub", "color", "highlight"} <= marks, marks)
        details = import_details(self.db, plan["id"])
        self.assertEqual(details["source_url"], "https://source.example.org/article")
        self.assertEqual(details["extra"]["onenote"]["level"], 0)
        # The subpage list on the parent, and the subpage's link back to it.
        child = self.tree("GRAPH:0-P2!1-ABC")
        self.assertEqual(import_details(self.db, child["id"])["extra"]["onenote"]["parent_page_id"],
                         "GRAPH:0-P1!1-ABC")
        back = next(node for node in _walk(child["children"]) if node["text"].startswith("Back to"))
        self.assertEqual(_inline(back, "docLink")[0]["id"], plan["id"])
        self.assertTrue(any(link["id"] == child["id"] for node in nodes for link in _inline(node, "docLink")))

        snodes = list(_walk(standup["children"]))
        table = next(node for node in snodes if node["type"] == "table")
        self.assertTrue(any(node["type"] == "table" for node in _walk(table["children"]) if node is not table))
        files = [node for node in snodes if node["type"] == "file"]
        self.assertTrue(any(node["attrs"].get("name") == "Contract.pdf" or "Contract.pdf" in json.dumps(node)
                            for node in files))
        printouts = [node for node in snodes if node["type"] == "image" and node["attrs"].get("printout_page") is not None]
        self.assertEqual(len(printouts), 1)  # page two is not a picture: kept as a file

        # Re-running the download and the import changes nothing.
        self.graph.log.clear()
        again = self.root / "again.zip"
        self.download(again)
        self.assertEqual(self.graph.count("/content"), 0)
        second = self.upload(again)["notebooks"][0]
        self.assertEqual((second["pages_created"], second["pages_updated"], second["pages_unchanged"]), (0, 0, 3))

    def test_manifest_folder_by_path_and_unsafe_zip_entries(self) -> None:
        folder = self.root / "graph-out"
        self.download(folder)
        response = self.client.post("/api/import/onenote", json={"path": str(folder)})
        self.assertEqual(response.status_code, 202)
        self.jobs.wait(60)
        job = self.client.get(f"/api/import/onenote/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "done", job)
        self.assertTrue((folder / "manifest.json").is_file())  # a folder is read in place, never deleted

        archive = self.root / "tampered.zip"
        with zipfile.ZipFile(archive, "w") as out:
            out.write(folder / "manifest.json", "manifest.json")
            for asset in (folder / "assets").iterdir():
                out.write(asset, f"assets/{asset.name}")
            out.writestr("../escape.txt", "x")
            out.writestr("assets/../../escape2.txt", "x")
            out.writestr("other/readme.txt", "x")
        result = self.upload(archive)
        self.assertEqual(result["notebooks"][0]["pages_unchanged"], 3)
        self.assertFalse(any(self.root.rglob("escape*.txt")))
        self.assertFalse(any(self.notes.rglob("escape*.txt")))

    def test_ordinary_exports_still_use_the_converter(self) -> None:
        archive = self.root / "Notebook.zip"
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr("Work/Open Notebook.onetoc2", b"x")
        self.assertIsNone(onenote_import.manifest_input(archive, self.root / "work"))
        self.assertIsNone(onenote_import.manifest_input(self.root, self.root / "work"))


if __name__ == "__main__":
    unittest.main()
