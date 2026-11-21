"""Download cache (incremental re-runs, resume after interruption) and the manifest
written from it (SCHEMA.md contract: ids, links, assets, reports, zip)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fortress_onenote_graph import cli
from fortress_onenote_graph.download import Downloader
from fortress_onenote_graph.graph import GraphClient, GraphError
from fortress_onenote_graph.manifest import build
from fortress_onenote_graph.progress import Progress
from fortress_onenote_graph.store import Store
from fortress_onenote_graph.throttle import Throttle

from .fakes import CHILD_GUID, PDF, PLAN_GUID, PNG, STANDUP_GUID, FakeClock, sample_graph


class _Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.graph = sample_graph()
        self.store = Store(self.root / "cache")
        self.clock = FakeClock()
        self.events = io.StringIO()

    def download(self, names=None):
        client = GraphClient(lambda force: "t", Throttle(clock=self.clock), transport=self.graph)
        return Downloader(client, self.store, Progress("json", stream=self.events)).run(names)


class DownloadTests(_Base):
    def test_first_run_downloads_everything_once(self):
        stats = self.download()
        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["pages_fetched"], 3)
        self.assertEqual(stats["sections_failed"], 1)  # the locked section
        self.assertEqual(self.graph.count("/content"), 3)
        self.assertEqual(self.graph.count("/resources/"), 5)  # 4 cached + 1 gone (404)
        self.assertEqual(stats["resources_missing"], 1)
        lines = [json.loads(line) for line in self.events.getvalue().splitlines()]
        self.assertTrue(all("event" in line for line in lines))
        self.assertIn({"event": "progress", "phase": "pages", "done": 3, "total": 3, "current": None}, lines)

    def test_rerun_fetches_only_changed_pages(self):
        self.download()
        self.graph.log.clear()
        stats = self.download()
        self.assertEqual((stats["pages_fetched"], stats["pages_unchanged"]), (0, 3))
        self.assertEqual(self.graph.count("/content"), 0)
        self.assertEqual(self.graph.count("/resources/"), 0)
        # One page edited in OneNote: only it is fetched again; its cached pictures are reused.
        self.graph.pages["0-SA!1-ABC"][0]["lastModifiedDateTime"] = "2025-04-01T00:00:00Z"
        self.graph.content["0-P1!1-ABC"] = self.graph.content["0-P1!1-ABC"].replace("Left column", "Edited column")
        self.graph.log.clear()
        stats = self.download()
        self.assertEqual(stats["pages_fetched"], 1)
        self.assertEqual(self.graph.count("/content"), 1)
        self.assertEqual(self.graph.count("/resources/"), 0)
        self.assertIn("Edited column", self.store.page_html("0-P1!1-ABC"))

    def test_resume_after_interruption(self):
        # Microsoft keeps refusing the printout picture: the run stops (resumable)...
        self.graph.fail.append(["bbb223", 429, 1000])
        with self.assertRaises(GraphError):
            self.download()
        done_before = self.graph.count("/content")
        # ...and the next run continues: finished pages and resources are not fetched again.
        self.graph.fail.clear()
        self.graph.log.clear()
        stats = self.download()
        self.assertEqual(self.graph.count("/content"), 3 - done_before + 0)
        self.assertLessEqual(self.graph.count("/resources/"), 3)
        self.assertEqual(self.graph.count("aaa222"), 0)  # the plan page's picture was cached
        self.assertEqual(stats["pages"], 3)
        self.assertTrue(all(self.store.page_is_current(page) for pages in self.graph.pages.values() for page in pages))

    def test_interruption_mid_resource_keeps_page_incomplete(self):
        self.graph.fail.append(["bbb111", "raise", 1000])
        with self.assertRaises(GraphError):
            self.download()
        page = self.graph.pages["0-SM!1-ABC"][0]
        self.assertFalse(self.store.page_is_current(page))
        self.assertIsNotNone(self.store.page_html("0-P3!1-ABC"))
        self.graph.fail.clear()
        self.graph.log.clear()
        self.download()
        self.assertEqual(self.graph.count("/pages/0-P3!1-ABC/content"), 0)  # HTML reused, only resources fetched
        self.assertTrue(self.store.page_is_current(page))

    def test_unknown_notebook_name(self):
        with self.assertRaises(SystemExit):
            self.download(["Nope"])


class ManifestTests(_Base):
    def setUp(self):
        super().setUp()
        self.download()

    def manifest(self, out=None):
        out = out or self.root / "out"
        summary = build(self.store, out)
        return summary, json.loads((out / "manifest.json").read_text(encoding="utf-8")), out

    def pages(self, manifest):
        found = {}

        def walk(entries):
            for entry in entries:
                if entry["kind"] == "section":
                    for page in entry["pages"]:
                        found[page["title"]] = page
                else:
                    walk(entry["children"])
        for notebook in manifest["notebooks"]:
            walk(notebook["children"])
        return found

    def test_top_level_contract(self):
        summary, manifest, out = self.manifest()
        self.assertEqual(manifest["format"], "fortress-onenote-manifest")
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["units"], {"text_offsets": "code_points", "geometry": "half_inch",
                                             "px_per_geometry_unit": 48.0})
        self.assertEqual(summary["pages"], 3)
        [notebook] = manifest["notebooks"]
        self.assertEqual((notebook["id"], notebook["file_identity"], notebook["source_kind"]),
                         ("graph:0-NB1!1-ABC", "graph:0-NB1!1-ABC", "graph"))
        names = [(entry["kind"], entry["name"]) for entry in notebook["children"]]
        self.assertEqual(names, [("section", "Alpha"), ("section_group", "Projects")])
        self.assertEqual(notebook["report"]["failed_sections"][0]["path"], ["Work", "Locked"])
        self.assertIn("password", notebook["report"]["failed_sections"][0]["reason"])
        self.assertTrue(manifest["report"]["input_warnings"])  # ink is not downloaded

    def test_pages_ids_levels_and_links(self):
        _, manifest, _ = self.manifest()
        pages = self.pages(manifest)
        plan, child, standup = pages["Project plan"], pages["Child"], pages["Standup"]
        self.assertEqual((plan["id"], plan["link_target_id"]), ("graph:0-P1!1-ABC", "{" + PLAN_GUID + "}"))
        self.assertEqual((child["level"], child["parent_id"], child["order"]), (1, "graph:0-P1!1-ABC", 1))
        self.assertEqual(standup["link_target_id"], "{" + STANDUP_GUID + "}")
        self.assertEqual(child["link_target_id"], "{" + CHILD_GUID + "}")
        self.assertEqual(standup["created"], "2025-03-01T08:00:00.123Z")
        self.assertEqual(standup["source_path"], ["Work", "Projects", "Meetings", "Standup"])
        self.assertEqual(plan["source_url"], "https://source.example.org/article")
        self.assertEqual(plan["layout"][1]["kind"], "columns")
        self.assertEqual(plan["stats"]["blocks"], sum(len(item["blocks"]) for item in plan["items"]))

    def test_assets_are_content_addressed(self):
        _, manifest, out = self.manifest()
        assets = manifest["assets"]
        png = hashlib.sha256(PNG).hexdigest() + ".png"
        pdf = hashlib.sha256(PDF).hexdigest() + ".pdf"
        self.assertIn(png, assets)
        self.assertIn(pdf, assets)
        self.assertEqual(assets[pdf], {"path": f"assets/{pdf}", "sha256": pdf[:64], "size": len(PDF),
                                       "mime": "application/pdf", "ext": "pdf"})
        for name, info in assets.items():
            self.assertEqual(hashlib.sha256((out / info["path"]).read_bytes()).hexdigest(), info["sha256"])
        standup = self.pages(manifest)["Standup"]
        blocks = standup["items"][0]["blocks"]
        invalid = next(b for b in blocks if b.get("printout_page_number") == 1)
        self.assertEqual(invalid["data_status"], "invalid")
        self.assertTrue(invalid["asset"].endswith(".bin"))
        missing = manifest["notebooks"][0]["report"]["missing_data"]
        self.assertEqual(sorted(item["data_status"] for item in missing), ["invalid", "missing", "missing"])

    def test_counts(self):
        _, manifest, _ = self.manifest()
        counts = manifest["notebooks"][0]["report"]["counts"]
        self.assertEqual(counts["pages"], 3)
        self.assertEqual(counts["subpages"], 1)
        self.assertEqual(counts["tables"], 2)
        self.assertEqual(counts["printout_pages"], 2)
        self.assertEqual(counts["attachments"], 3)
        self.assertEqual(counts["check_items"], 3)
        self.assertEqual(counts["column_layouts"], 1)
        self.assertGreaterEqual(counts["links_onenote"], 3)

    def test_zip_output(self):
        summary, _, _ = self.manifest()
        target = self.root / "export.zip"
        build(self.store, target)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        self.assertEqual(names[0], "manifest.json")
        self.assertTrue(all(name == "manifest.json" or name.startswith("assets/") for name in names))
        self.assertEqual(len(names), summary["assets"] + 1)
        self.assertFalse((self.root / "export.zip.partial").exists())

    def test_notebook_filter(self):
        summary = build(self.store, self.root / "none", notebook_names=["Other"])
        self.assertEqual(summary["notebooks"], 0)


class CliTests(_Base):
    def test_convert_works_offline_from_the_cache(self):
        self.download()
        (self.root / "cache-root").mkdir()
        cache_dir = self.root / "cache-root"
        store = Store.for_account(cache_dir, "user-1")
        for path in self.store.root.rglob("*"):
            if path.is_file():
                target = store.root / path.relative_to(self.store.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        (cache_dir / "last_account.json").write_text(json.dumps({"key": store.root.name}))
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch("sys.stderr", io.StringIO()):
            code = cli.main(["convert", "--cache-dir", str(cache_dir), "--out", str(self.root / "o.zip")])
        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(summary["pages"], 3)
        self.assertTrue((self.root / "o.zip").is_file())

    def test_notebooks_as_json(self):
        class FakeSession:
            def __init__(self, *_args, **_kwargs):
                pass

            def token(self, force_refresh=False):
                return "t"

        def client(token, _throttle, log=None):
            return GraphClient(token, Throttle(clock=self.clock), transport=self.graph, log=log)

        stdout = io.StringIO()
        with patch("fortress_onenote_graph.auth.Session", FakeSession), patch.object(cli, "GraphClient", client), \
                redirect_stdout(stdout), patch("sys.stderr", io.StringIO()):
            code = cli.main(["notebooks", "--json", "--progress", "json", "--client-id", "c",
                             "--cache-dir", str(self.root / "c"), "--config-dir", str(self.root / "cfg")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"notebooks": [{"name": "Work", "sections": 3}]})

    def test_missing_client_id_is_a_clear_error(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", {"FORTRESS_ONENOTE_CLIENT_ID": ""}), patch("sys.stderr", stderr):
            code = cli.main(["download", "--cache-dir", str(self.root / "c")])
        self.assertEqual(code, 1)
        self.assertIn("client id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
