"""The local API refuses browsers and, with FORTRESS_TOKEN, anyone without the token
(app/auth.py); the backend stops its converters on shutdown."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, onenote_import
from support import make_settings, use_settings


TOKEN = "s3cret-token-for-tests"
EVIL = {"Origin": "https://evil.example"}


class _BackendTestCase(unittest.TestCase):
    token: str | None = None

    def setUp(self) -> None:
        from app import documents, main

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        notes = Path(directory.name)
        settings = make_settings(notes, reindex_interval_s=5.0)
        (settings.assets_path / "a.txt").write_text("hello")
        self.notes = notes
        environment = {auth.TOKEN_ENV: self.token} if self.token else {}
        env_patch = patch.dict(os.environ, environment)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        if not self.token:
            os.environ.pop(auth.TOKEN_ENV, None)
        patcher = use_settings(settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)


class WithoutTokenTests(_BackendTestCase):
    def test_the_app_and_tests_work_as_before(self) -> None:
        self.assertEqual(self.client.get("/api/navigation").status_code, 200)
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(len(health.headers[auth.INSTANCE_HEADER]), 16)

    def test_cross_site_simple_requests_are_refused(self) -> None:
        plain = {**EVIL, "Content-Type": "text/plain"}
        for response in (
            self.client.post("/api/capture", content=json.dumps({"text": "pwned"}), headers=plain),
            self.client.post("/api/import/onenote", content=json.dumps({"path": "/etc"}), headers=plain),
            self.client.post(
                "/api/markdown-import/files",
                files={"files": ("x.md", b"# hi", "text/markdown")},
                headers=EVIL,
            ),
            self.client.post("/api/backups", headers=EVIL),
            self.client.post("/api/backups", headers={"Sec-Fetch-Site": "cross-site"}),
            self.client.options(
                "/api/capture", headers={**EVIL, "Access-Control-Request-Method": "POST"}
            ),
        ):
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.client.get("/api/backups").json(), [])

    def test_dns_rebinding_host_is_refused(self) -> None:
        response = self.client.get("/api/navigation", headers={"Host": "rebind.evil.example:8000"})
        self.assertEqual(response.status_code, 403)
        for host in ("127.0.0.1:8000", "localhost", "[::1]:8000"):
            self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 200)
        with patch.dict(os.environ, {auth.ALLOWED_HOSTS_ENV: "mac.lan, other"}):
            self.assertEqual(
                self.client.get("/api/navigation", headers={"Host": "mac.lan:8000"}).status_code, 200
            )

    def test_path_import_needs_the_token(self) -> None:
        response = self.client.post("/api/import/onenote", json={"path": str(self.notes)})
        self.assertEqual(response.status_code, 403)
        self.assertIn("token", response.json()["detail"])


class WithTokenTests(_BackendTestCase):
    token = TOKEN

    def test_only_health_is_open(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.headers[auth.INSTANCE_HEADER], hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
        )
        for method, path in (
            ("GET", "/api/navigation"),
            ("POST", "/api/backups"),
            ("GET", "/media/missing.png"),
            ("GET", "/openapi.json"),
        ):
            response = self.client.request(method, path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            wrong = self.client.request(method, path, headers={"X-Fortress-Token": TOKEN + "x"})
            self.assertEqual(wrong.status_code, 401, path)

    def test_the_token_opens_everything(self) -> None:
        self.assertEqual(
            self.client.get("/api/navigation", headers={"X-Fortress-Token": TOKEN}).status_code, 200
        )
        media = self.client.get("/media/missing.png", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(media.status_code, 404)  # past the check; the file lives elsewhere
        # Host doesn't matter with a token (it can't be guessed by a rebinding page).
        response = self.client.get(
            "/api/navigation", headers={"X-Fortress-Token": TOKEN, "Host": "mac.lan"}
        )
        self.assertEqual(response.status_code, 200)

    def test_a_browser_is_refused_even_with_the_token(self) -> None:
        response = self.client.post("/api/backups", headers={**EVIL, "X-Fortress-Token": TOKEN})
        self.assertEqual(response.status_code, 403)


class LifecycleTests(unittest.TestCase):
    def test_terminate_converters_kills_running_ones(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.kill)
        with onenote_import._converters_lock:
            onenote_import._converters.add(process)
        self.addCleanup(onenote_import._converters.discard, process)
        started = time.monotonic()
        onenote_import.terminate_converters()
        self.assertIsNotNone(process.wait(5))
        self.assertLess(time.monotonic() - started, 5)

    def test_shutdown_stops_children(self) -> None:
        from app import main

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        notes = Path(directory.name)
        settings = make_settings(notes, reindex_interval_s=5.0)
        with use_settings(settings), patch.object(auth, "stop_children") as stop:
            with TestClient(main.app):
                stop.assert_not_called()
            stop.assert_called_once()

    def test_parent_watch_needs_a_pid(self) -> None:
        with patch.dict(os.environ, {auth.PARENT_ENV: ""}):
            self.assertIsNone(auth.start_parent_watch())
        self.assertTrue(auth._alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(auth._alive(child.pid))

    def test_backend_exits_when_its_parent_does(self) -> None:
        # A real uvicorn whose "app" (a sleeping process) goes away.
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(parent.kill)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = {
            **os.environ,
            auth.PARENT_ENV: str(parent.pid),
            auth.TOKEN_ENV: TOKEN,
            "NOTES_DIR": directory.name,
            "EMBEDDINGS_ENABLED": "false",
            "VISION_ENABLED": "false",
            "AUTO_BACKUP_ENABLED": "false",
            "OPENROUTER_API_KEY": "",
        }
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "0"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(backend.kill)
        self.addCleanup(lambda: backend.stderr.closed or backend.stderr.close())
        deadline = time.monotonic() + 60
        lines = []
        while time.monotonic() < deadline:
            line = backend.stderr.readline()
            lines.append(line)
            if "startup complete" in line or not line:
                break
        self.assertIsNone(backend.poll(), "".join(lines))
        parent.kill()
        parent.wait()
        backend.wait(20)  # uvicorn re-raises SIGTERM after a clean shutdown
        rest = backend.stderr.read()
        backend.stderr.close()
        self.assertIn("is gone; stopping the backend", rest)
        self.assertIn("Application shutdown complete", rest)


if __name__ == "__main__":
    unittest.main()
