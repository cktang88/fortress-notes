"""The in-app OneNote download (app/onenote_graph.py): the backend runs
tools/fortress-onenote-graph as a child and follows its ``--progress json`` lines.

A fake tool (a small Python script standing in for the ``fortress-onenote-graph``
wrapper) prints the same lines the real one does; how it behaves is chosen per test
through the environment. Its manifest is a real one, written by the real tool from its
in-memory Graph (tests/fakes.py there), so the import that follows is the real import.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import onenote_graph, onenote_import
from support import make_settings, use_settings

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

CLIENT_ID = "1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"

# Stands in for the tool's wrapper. FAKE_MODE picks the behaviour; FAKE_DIR holds the
# manifest to copy, the "go" file a paused run waits for, and what it reports back.
FAKE_TOOL = r'''
import json, os, shutil, subprocess, sys, time
args = sys.argv[1:]
mode = os.environ["FAKE_MODE"]
work = os.environ["FAKE_DIR"]

def emit(**event):
    sys.stderr.write(json.dumps(event) + "\n")
    sys.stderr.flush()

def wait_for_go():
    while not os.path.exists(os.path.join(work, "go")):
        time.sleep(0.02)

with open(os.path.join(work, "seen.json"), "w") as handle:
    json.dump({"args": args, "client_id": os.environ.get("FORTRESS_ONENOTE_CLIENT_ID"),
               "token": os.environ.get("FORTRESS_TOKEN")}, handle)
emit(event="phase", phase="signin")
if mode == "device_code":
    emit(event="device_code", user_code="ABCD-EFGH", verification_uri="https://microsoft.com/devicelogin",
         expires_in=900, message="Go to https://microsoft.com/devicelogin and enter ABCD-EFGH")
    wait_for_go()
if args[0] == "notebooks":
    print(json.dumps({"notebooks": [{"name": "Work", "sections": 3}, {"name": "Home", "sections": 1}]}))
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("fortress-onenote-graph: Sign-in failed: AADSTS700016: application not found\n")
    sys.exit(1)
emit(event="phase", phase="hierarchy")
emit(event="phase", phase="pages")
emit(event="progress", phase="pages", done=0, total=3, current="Work › Alpha › Project plan")
if mode == "throttle":
    emit(event="wait", seconds=60.0, reason="throttled by Microsoft (429), backing off")
    wait_for_go()
if mode == "resumable":
    sys.stderr.write("fortress-onenote-graph: HTTP 429 throttled: too many requests\n"
                     "Nothing is lost: run the same command again to continue.\n")
    sys.exit(3)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.path.join(work, "pids.json"), "w") as handle:
        json.dump([os.getpid(), child.pid], handle)
    time.sleep(120)
emit(event="progress", phase="pages", done=3, total=3, current=None)
emit(event="warning", message="Work › Locked: pages could not be listed")
emit(event="phase", phase="convert")
out = args[args.index("--out") + 1]
shutil.copytree(os.path.join(work, "manifest"), out)
emit(event="phase", phase="done")
print(json.dumps({"manifest": out, "format_version": 1, "notebooks": 1, "pages": 3}))
'''


def _alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            return "\nState:\tZ" not in status.read_text()
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class GraphDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._shared = tempfile.TemporaryDirectory()
        root = Path(cls._shared.name)
        client = GraphClient(lambda force: "t", Throttle(clock=fakes.FakeClock()), transport=fakes.sample_graph())
        store = Store(root / "cache")
        Downloader(client, store, Progress(quiet=True, stream=io.StringIO())).run()
        cls.manifest = root / "manifest"
        build(store, cls.manifest)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._shared.cleanup()

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.fake = self.root / "fake"
        self.tool = self.root / "tool"
        self.fake.mkdir()
        self.tool.mkdir()
        shutil.copytree(self.manifest, self.fake / "manifest")
        wrapper = self.tool / "fortress-onenote-graph"
        wrapper.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
        wrapper.chmod(0o755)
        self.settings = make_settings(self.notes, onenote_graph_dir=str(self.tool))
        from app import main

        self.jobs = onenote_import.ImportJobs()
        self.downloads = onenote_graph.GraphDownloads()
        for item in (
            use_settings(self.settings),
            patch.object(onenote_import, "jobs", self.jobs),
            patch.object(onenote_graph, "downloads", self.downloads),
            patch.object(onenote_graph, "find_uv", lambda: "/usr/bin/uv"),
            patch.object(onenote_graph, "ETA_MIN_S", 0.0),
            patch.object(onenote_graph, "KILL_GRACE_S", 1.0),
            patch.dict(os.environ, {"FORTRESS_TOKEN": "test-token", "FAKE_MODE": "ok", "FAKE_DIR": str(self.fake)}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app, headers={"X-Fortress-Token": "test-token"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def start(self, mode: str = "ok", path: str = "/api/import/onenote-graph", **body) -> dict:
        os.environ["FAKE_MODE"] = mode
        response = self.client.post(path, json={"client_id": CLIENT_ID, "account": "personal", **body})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def job(self, job_id: str) -> dict:
        response = self.client.get(f"/api/import/onenote-graph/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def until(self, job_id: str, check, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            record = self.job(job_id)
            if check(record):
                return record
            if time.monotonic() > deadline:
                self.fail(f"timed out; job is {record}")
            time.sleep(0.02)

    def seen(self) -> dict:
        return json.loads((self.fake / "seen.json").read_text())

    def test_download_then_import(self) -> None:
        started = self.start(notebooks=["Work"])
        self.assertEqual(started["status"], "queued")
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "done", record)
        self.assertEqual(record["summary"]["pages"], 3)
        self.assertEqual(record["warnings"], ["Work › Locked: pages could not be listed"])
        self.assertIsNone(record["wait"])
        seen = self.seen()
        self.assertEqual(seen["client_id"], CLIENT_ID)
        self.assertIsNone(seen["token"], "the backend's token must not reach the tool")
        args = seen["args"]
        self.assertEqual(args[:5], ["download", "--progress", "json", "--account", "personal"])
        self.assertEqual(args[args.index("--notebook") + 1], "Work")

        # The result goes through the ordinary import job.
        self.jobs.wait(60)
        imported = self.client.get(f"/api/import/onenote/{record['import_job_id']}").json()
        self.assertEqual(imported["status"], "done", imported)
        [notebook] = imported["result"]["notebooks"]
        self.assertEqual((notebook["name"], notebook["pages_created"]), ("Work", 3))
        # The downloaded folder is gone once imported.
        self.assertFalse((onenote_import.imports_dir(self.notes) / f"{record['job_id']}-graph").exists())
        self.assertEqual(self.client.get("/api/import/onenote-graph").json()["job"]["job_id"], record["job_id"])

    def test_throttle_wait_is_reported_then_cleared(self) -> None:
        started = self.start("throttle")
        record = self.until(started["job_id"], lambda job: job["wait"] is not None)
        self.assertEqual(record["status"], "downloading")
        self.assertEqual(record["progress"], {"phase": "pages", "done": 0, "total": 3,
                                              "current": "Work › Alpha › Project plan"})
        self.assertEqual(record["wait"]["seconds"], 60.0)
        self.assertIn("429", record["wait"]["reason"])
        self.assertTrue(record["wait"]["until"].endswith("Z"))
        (self.fake / "go").touch()
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "done")
        self.assertIsNone(record["wait"])
        self.jobs.wait(60)

    def test_device_code_is_shown_until_signed_in(self) -> None:
        started = self.start("device_code")
        record = self.until(started["job_id"], lambda job: job["device_code"] is not None)
        self.assertEqual(record["status"], "signing_in")
        self.assertEqual(record["device_code"]["user_code"], "ABCD-EFGH")
        self.assertEqual(record["device_code"]["verification_uri"], "https://microsoft.com/devicelogin")
        (self.fake / "go").touch()
        self.downloads.wait(30)
        self.assertIsNone(self.job(started["job_id"])["device_code"])
        self.jobs.wait(60)

    def test_failure_shows_the_tool_message(self) -> None:
        started = self.start("fail")
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "Sign-in failed: AADSTS700016: application not found")
        self.assertFalse(record["resumable"])
        self.assertIsNone(record["import_job_id"])
        self.assertEqual(self.jobs.list(self.notes), [])

    def test_interrupted_download_says_run_again(self) -> None:
        started = self.start("resumable")
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["resumable"])
        self.assertIn("Download and Import again", record["error"])

    def test_cancel_kills_the_tool_and_its_children(self) -> None:
        started = self.start("hang")
        pids_file = self.fake / "pids.json"
        deadline = time.monotonic() + 20
        while not pids_file.exists() or not pids_file.read_text():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        self.assertTrue(all(_alive(pid) for pid in pids))
        response = self.client.post(f"/api/import/onenote-graph/{started['job_id']}/cancel")
        self.assertEqual(response.status_code, 202, response.text)
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("continues where it stopped", record["error"])
        deadline = time.monotonic() + 10
        while any(_alive(pid) for pid in pids):
            self.assertLess(time.monotonic(), deadline, "the tool is still running")
            time.sleep(0.05)
        self.assertIsNone(record["import_job_id"])
        again = self.client.post(f"/api/import/onenote-graph/{started['job_id']}/cancel")
        self.assertEqual(again.status_code, 409)

    def test_shutdown_kills_a_running_download(self) -> None:
        started = self.start("hang")
        pids_file = self.fake / "pids.json"
        deadline = time.monotonic() + 20
        while not pids_file.exists() or not pids_file.read_text():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        self.downloads.stop()
        self.downloads.wait(30)
        self.assertEqual(self.job(started["job_id"])["status"], "cancelled")
        deadline = time.monotonic() + 10
        while any(_alive(pid) for pid in pids):
            self.assertLess(time.monotonic(), deadline, "the tool is still running")
            time.sleep(0.05)

    def test_list_notebooks(self) -> None:
        started = self.start(path="/api/import/onenote-graph/notebooks", account="work")
        self.downloads.wait(30)
        record = self.job(started["job_id"])
        self.assertEqual(record["status"], "done", record)
        self.assertEqual(record["notebooks"], [{"name": "Work", "sections": 3}, {"name": "Home", "sections": 1}])
        self.assertIsNone(record["import_job_id"])
        self.assertEqual(self.seen()["args"], ["notebooks", "--progress", "json", "--account", "work", "--json"])

    def test_one_download_at_a_time(self) -> None:
        started = self.start("throttle")
        os.environ["FAKE_MODE"] = "throttle"
        second = self.client.post("/api/import/onenote-graph", json={"client_id": CLIENT_ID})
        self.assertEqual(second.status_code, 409)
        self.client.post(f"/api/import/onenote-graph/{started['job_id']}/cancel")
        self.downloads.wait(30)

    def test_bad_input_and_missing_tool(self) -> None:
        bad = self.client.post("/api/import/onenote-graph", json={"client_id": "not an id"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Application (client) ID", bad.json()["detail"])
        self.assertEqual(self.client.post("/api/import/onenote-graph",
                                          json={"client_id": CLIENT_ID, "account": "school"}).status_code, 400)
        self.assertTrue(self.client.get("/api/import/onenote-graph").json()["available"])
        with patch.object(onenote_graph, "find_uv", lambda: None):
            status = self.client.get("/api/import/onenote-graph").json()
            self.assertFalse(status["available"])
            self.assertIn("brew install uv", status["problem"])
            refused = self.client.post("/api/import/onenote-graph", json={"client_id": CLIENT_ID})
            self.assertEqual(refused.status_code, 503)
        with use_settings(make_settings(self.notes, onenote_graph_dir=str(self.root / "nowhere"))):
            status = self.client.get("/api/import/onenote-graph").json()
            self.assertFalse(status["available"])
            self.assertIn("FORTRESS_ONENOTE_GRAPH_DIR", status["problem"])

    def test_eta_after_enough_pages(self) -> None:
        downloads = onenote_graph.GraphDownloads()
        downloads._jobs["j"] = {"progress": {}, "warnings": [], "warning_count": 0}
        started = downloads._apply("j", {"event": "progress", "phase": "pages", "done": 0, "total": 100}, None)
        started = (started[0] - 100.0, 0)  # 100 s ago
        downloads._apply("j", {"event": "progress", "phase": "pages", "done": 20, "total": 100}, started)
        self.assertEqual(downloads._jobs["j"]["eta_seconds"], 400)
        downloads._apply("j", {"event": "wait", "seconds": 2.0, "reason": "pacing"}, started)
        self.assertIsNone(downloads._jobs["j"]["wait"])

    def test_the_real_tool_is_found_in_the_repository(self) -> None:
        with use_settings(make_settings(self.notes)):
            self.assertEqual(onenote_graph.tool_dir(), TOOL)
            self.assertTrue((onenote_graph.tool_dir() / "fortress-onenote-graph").is_file())


if __name__ == "__main__":
    unittest.main()
