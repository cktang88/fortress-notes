"""Background work (app/background.py), Markdown mirrors, settings and the block_store API."""

import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import background, block_store, documents, portability
from app.config import BACKEND_DIR, Settings
from app.models import NoteCreate
from support import make_settings, mirror_file, use_settings


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.notes = Path(directory)
        self.settings = make_settings(self.notes)
        self.enterContext(use_settings(self.settings))

    def mirror(self, document_id: str) -> Path:
        """The note's mirror (where it was last written, once it is gone)."""

        known = self.__dict__.setdefault("_mirrors", {})
        # (read from disk: a test may write with a writer of its own)
        path = mirror_file(self.notes, document_id)
        if path.exists():
            known[document_id] = path
        return known.get(document_id, self.notes / f"{document_id}.md")


class MirrorTests(_Workspace):
    def test_trash_during_a_slow_mirror_write_never_leaves_the_mirror(self) -> None:
        # scratchpad/scripts/mirror_race.py: a write that read the note before it
        # was trashed used to finish after the trash had removed the mirror.
        note = documents.create(NoteCreate(title="t", body="hello"))
        documents.flush_mirrors()
        self.assertTrue(self.mirror(note["id"]).exists())
        documents.update(note["id"], documents.NoteUpdate(title="t"))
        rendering = threading.Event()
        original = portability._render_markdown

        def slow(*args, **kwargs):
            rendering.set()
            time.sleep(0.3)
            return original(*args, **kwargs)

        with patch.object(portability, "_render_markdown", slow):
            writer = threading.Thread(target=documents.flush_mirrors)
            writer.start()
            self.assertTrue(rendering.wait(5))
            documents.trash(note["id"])  # while the old tree is being written
            writer.join()
        documents.flush_mirrors()
        self.assertFalse(self.mirror(note["id"]).exists())

    def test_trash_then_purge_before_a_pass_removes_the_mirror(self) -> None:
        note = documents.create(NoteCreate(title="Gone", body="soon"))
        documents.flush_mirrors()
        documents.trash(note["id"])
        documents.purge(note["id"])  # the row is gone before the writer saw the trash
        documents.flush_mirrors()
        self.assertFalse(self.mirror(note["id"]).exists())

        other = documents.create(NoteCreate(title="Also gone", body="x"))
        documents.flush_mirrors()
        documents.trash(other["id"])
        self.assertEqual(documents.empty_trash(), 1)
        documents.flush_mirrors()
        self.assertFalse(self.mirror(other["id"]).exists())

    def test_writes_never_touch_mirrors_and_restore_from_trash_brings_it_back(self) -> None:
        note = documents.create(NoteCreate(title="Later", body="text"))
        self.assertFalse(self.mirror(note["id"]).exists())  # nothing written inline
        documents.flush_mirrors()
        self.assertIn("title: Later", self.mirror(note["id"]).read_text(encoding="utf-8"))
        documents.trash(note["id"])
        documents.flush_mirrors()
        self.assertFalse(self.mirror(note["id"]).exists())
        documents.restore(note["id"])
        documents.flush_mirrors()
        self.assertTrue(self.mirror(note["id"]).exists())

    def test_a_restart_rewrites_only_the_mirrors_of_notes_changed_meanwhile(self) -> None:
        first = documents.create(NoteCreate(title="One", body="a"))
        documents.create(NoteCreate(title="Two", body="b"))
        documents.flush_mirrors()
        self.assertTrue((self.notes / documents.MIRROR_STATE).exists())
        documents.update(first["id"], documents.NoteUpdate(title="One again"))
        restarted = documents.MirrorWriter()  # a new process: its position is read back
        self.assertEqual(restarted.sync(self.settings.block_db_path, self.notes), 1)
        self.assertIn("One again", self.mirror(first["id"]).read_text(encoding="utf-8"))
        self.assertEqual(restarted.sync(self.settings.block_db_path, self.notes), 0)

    def test_the_lane_writes_mirrors_by_itself(self) -> None:
        background.start(documents.MirrorWriter.name)
        self.addCleanup(background.halt, documents.MirrorWriter.name)
        note = documents.create(NoteCreate(title="Lane", body="x"))
        deadline = time.monotonic() + 5
        while not self.mirror(note["id"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.mirror(note["id"]).exists())


class _Blocking(background.Service):
    name = "test-blocking"

    def __init__(self, long_running: bool = False) -> None:
        self.long_running = long_running
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = False
        self.steps = 0

    def step(self):
        self.steps += 1
        self.started.set()
        self.release.wait(5)
        return None

    def stop(self) -> None:
        self.stopped = True
        self.release.set()


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = background.Runner()
        self.addCleanup(self.runner.stop, 2.0)

    def test_pause_waits_for_the_unit_in_progress_and_holds_the_lanes(self) -> None:
        service = self.runner.register(_Blocking())
        self.runner.start(service.name)
        self.assertTrue(service.started.wait(5))
        threading.Timer(0.1, service.release.set).start()
        with self.runner.paused(timeout=5):
            self.assertFalse(self.runner.busy(service.name))
            steps = service.steps
            self.runner.wake(service.name)
            time.sleep(0.1)
            self.assertEqual(service.steps, steps)  # nothing runs while paused
        deadline = time.monotonic() + 5
        while service.steps == steps and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(service.steps, steps)  # and work resumes afterwards

    def test_pause_refuses_to_wait_for_a_long_running_unit(self) -> None:
        service = self.runner.register(_Blocking(long_running=True))
        self.runner.start(service.name)
        self.assertTrue(service.started.wait(5))
        with self.assertRaises(background.Busy), self.runner.paused(timeout=5):
            pass
        service.release.set()

    def test_stop_stops_child_work_and_threads(self) -> None:
        service = self.runner.register(_Blocking())
        self.runner.start(service.name)
        self.assertTrue(service.started.wait(5))
        self.runner.stop(2.0)
        self.assertTrue(service.stopped)
        self.assertFalse(self.runner.running(service.name))

    def test_low_priority_units_wait_for_running_searches(self) -> None:
        ran = threading.Event()

        class Low(background.Service):
            name = "test-low"
            priority = background.LOW

            def step(self):
                ran.set()
                return None

        self.runner.register(Low())
        with self.runner.foreground():
            self.runner.start("test-low")
            self.assertFalse(ran.wait(0.2))
        self.assertTrue(ran.wait(5))

    def test_lanes_never_run_on_the_asyncio_default_executor(self) -> None:
        names = []

        class Probe(background.Service):
            name = "test-probe"

            def step(self):
                names.append(threading.current_thread().name)
                return None

        self.runner.register(Probe())
        self.runner.start("test-probe")
        self.assertTrue(self.runner.wait_idle("test-probe", lambda: bool(names), 5))
        self.assertEqual(names[0], "fortress-test-probe")


class RestoreTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        from app import main

        self.client = self.enterContext(TestClient(main.app))

    def test_restore_is_refused_while_an_import_runs(self) -> None:
        from app import onenote_import

        name = self.client.post("/api/backups").json()["name"]
        jobs = onenote_import.jobs
        with patch.object(jobs.__class__, "step", lambda _self: time.sleep(0.5)):
            background.wake(jobs.name)
            deadline = time.monotonic() + 5
            while not background.busy(jobs.name) and time.monotonic() < deadline:
                time.sleep(0.01)
            response = self.client.post(f"/api/backups/{name}/restore")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("imports", response.json()["detail"])

    def test_restore_writes_pending_mirrors_then_follows_the_backup(self) -> None:
        kept = self.client.post("/api/block-documents", json={"title": "Kept"}).json()
        name = self.client.post("/api/backups").json()["name"]
        later = self.client.post("/api/block-documents", json={"title": "Later"}).json()
        response = self.client.post(f"/api/backups/{name}/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.mirror(kept["id"]).exists())
        self.assertFalse(self.mirror(later["id"]).exists())
        stash = self.notes / ".fortress-backups" / (
            "mirrors-before-" + Path(response.json()["safety_backup"]).stem
        )
        self.assertTrue((stash / "Later.md").exists())  # moved aside, not lost


class SettingsTests(unittest.TestCase):
    def test_relative_paths_are_relative_to_the_backend_not_the_current_directory(self) -> None:
        with patch.dict(os.environ, {"NOTES_DIR": "../somewhere", "FORTRESS_ONENOTE_BIN": "bin/conv"}):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.notes_path, (BACKEND_DIR / "../somewhere").resolve())
        self.assertEqual(settings.block_db_path, settings.notes_path / ".fortress.sqlite3")
        self.assertEqual(settings.onenote_bin, str(BACKEND_DIR / "bin/conv"))

    def test_converter_settings_come_from_the_environment(self) -> None:
        with patch.dict(os.environ, {"FORTRESS_ONENOTE_TIMEOUT_S": "12"}):
            self.assertEqual(Settings(_env_file=None).onenote_timeout_s, 12.0)


class BlockStoreApiTests(unittest.TestCase):
    def test_no_module_uses_block_store_private_names(self) -> None:
        offenders = []
        for path in sorted((BACKEND_DIR / "app").glob("*.py")):
            if path.name == "block_store.py":
                continue
            text = path.read_text(encoding="utf-8")
            code = re.sub(r"#[^\n]*", "", text)
            offenders += [f"{path.name}: {m}" for m in re.findall(r"block_store\._\w+", code)]
            for imported in re.findall(r"from \.block_store import \(?([^)]*?)\)?\n(?!\s)", code, re.S):
                offenders += [
                    f"{path.name}: {name.strip()}" for name in imported.split(",")
                    if name.strip().startswith("_")
                ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
