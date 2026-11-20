"""Download OneNote notebooks from Microsoft and import them: the in-app route.

The download itself is tools/fortress-onenote-graph, a separate, optional program with
its own locked dependencies (MSAL). This module runs it as a child process on the
``downloads`` lane (app/background.py) with the user's own Entra app id in
``FORTRESS_ONENOTE_CLIENT_ID``, reads its ``--progress json`` lines into a job record
the Mac app polls, and, when the manifest is written, hands it to the ordinary OneNote
import job (``onenote_import.jobs``), whose id the record then carries.

Jobs:

* ``download`` -- ``fortress-onenote-graph download --out <folder>`` (optionally only
  some notebooks), then the import.
* ``notebooks`` -- ``fortress-onenote-graph notebooks --json``: signs in and lists the
  notebooks, so the sheet can offer a choice.

Signing in opens the user's browser (MSAL, loopback redirect); when that does not work
the tool falls back to a device code, which the record carries (``device_code``) for
the sheet to show. Microsoft's throttling makes the tool pause; a pause of
:data:`SHOWN_WAIT_S` or more is in ``wait`` (``until`` is when it resumes). Cancelling
(or the backend stopping) ends the child's whole process group (``uv`` and Python).
Nothing is lost: the tool caches what it downloaded, so the next run resumes.

Records live in memory only: the backend stops with the app, and a new run resumes
from the tool's cache anyway.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import background, block_store, onenote_import
from .config import get_settings

TOOL_NAME = "fortress-onenote-graph"
CLIENT_ID_ENV = "FORTRESS_ONENOTE_CLIENT_ID"
ACCOUNTS = ("personal", "work")
# An Entra Application (client) ID is a GUID.
CLIENT_ID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
# Where uv usually is when the backend was started without a login shell's PATH.
UV_DIRS = ("~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin")
EXIT_RESUMABLE = 3
# Shorter pauses (the tool's own pacing) are not worth a message.
SHOWN_WAIT_S = 10.0
# An estimate needs this many pages and seconds of the page phase behind it.
ETA_MIN_PAGES = 10
ETA_MIN_S = 60.0
MAX_WARNINGS = 50
KILL_GRACE_S = 5.0
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
CANCELLED_MESSAGE = "Download stopped. Download and Import again continues where it stopped."
RESUMABLE_HINT = "Nothing is lost: Download and Import again continues where it stopped."
# Tool phase -> job status.
_PHASE_STATUS = {"signin": "signing_in", "hierarchy": "downloading", "sections": "downloading",
                 "pages": "downloading", "convert": "converting", "done": "converting"}


class GraphUnavailable(RuntimeError):
    """The tool or uv is missing; the message says how to fix it."""


def tool_dir() -> Path:
    configured = get_settings().onenote_graph_dir
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "tools" / TOOL_NAME


def search_path() -> str:
    """PATH for the child: this process's, plus where uv is usually installed."""

    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    for extra in UV_DIRS:
        expanded = str(Path(extra).expanduser())
        if expanded not in parts:
            parts.append(expanded)
    return os.pathsep.join(parts)


def find_uv() -> str | None:
    return shutil.which("uv", path=search_path())


def availability() -> dict[str, Any]:
    """``{"available": bool, "problem": str | None}`` (the problem says how to fix it)."""

    try:
        _check_available()
    except GraphUnavailable as error:
        return {"available": False, "problem": str(error)}
    return {"available": True, "problem": None}


def _check_available() -> Path:
    wrapper = tool_dir() / TOOL_NAME
    if not (wrapper.is_file() and os.access(wrapper, os.X_OK)):
        raise GraphUnavailable(
            f"The OneNote download tool isn't in this Fortress folder (expected {wrapper}). "
            "Update Fortress, or set FORTRESS_ONENOTE_GRAPH_DIR to the tools/fortress-onenote-graph folder.")
    if find_uv() is None:
        raise GraphUnavailable(
            "Downloading from Microsoft needs uv, which isn't installed. Install it with "
            "“brew install uv” (or see docs.astral.sh/uv), then try again.")
    return wrapper


def _utc(seconds_from_now: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:  # pragma: no cover - the backend runs on macOS and Linux
            process.kill()


class GraphDownloads(background.Service):
    """Download jobs, one at a time, on the ``downloads`` lane."""

    name = "downloads"
    # A download runs for minutes to hours (Microsoft's throttling); a database
    # restore must not wait for it.
    long_running = True

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._options: dict[str, dict[str, Any]] = {}
        self._queue: list[str] = []
        self._cancel: dict[str, threading.Event] = {}
        self._process: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # --- API ---------------------------------------------------------------------------

    def submit(self, *, kind: str, client_id: str, account: str, notebooks: list[str] | None = None,
               folder_id: str | None = None, db_path: Path, notes_path: Path, assets_path: Path) -> dict[str, Any]:
        """Queue a job (``kind`` is ``download`` or ``notebooks``). Raises ValueError for
        bad input, :class:`GraphUnavailable` when the tool can't run, RuntimeError when
        another download is still running."""

        client_id = client_id.strip()
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError("That isn't an Application (client) ID. Copy it from your app's Overview page "
                             "in Microsoft Entra: it looks like 1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901.")
        if account not in ACCOUNTS:
            raise ValueError("account must be 'personal' or 'work'")
        if kind not in ("download", "notebooks"):
            raise ValueError("kind must be 'download' or 'notebooks'")
        names = [name.strip() for name in notebooks or [] if isinstance(name, str) and name.strip()]
        _check_available()
        job_id = block_store.new_id()
        record = {
            "job_id": job_id, "kind": kind, "status": "queued", "account": account,
            "selected_notebooks": names or None, "folder_id": folder_id,
            "progress": {"phase": None, "done": 0, "total": 0, "current": None},
            "device_code": None, "wait": None, "eta_seconds": None,
            "warnings": [], "warning_count": 0, "notebooks": None, "summary": None,
            "import_job_id": None, "error": None, "resumable": False,
            "created_at": block_store.utc_now(), "finished_at": None,
        }
        with self._lock:
            if any(job["status"] not in TERMINAL_STATUSES for job in self._jobs.values()):
                raise RuntimeError("A download from Microsoft is already running.")
            self._jobs[job_id] = record
            self._cancel[job_id] = threading.Event()
            self._options[job_id] = {"client_id": client_id, "db_path": db_path, "notes_path": notes_path,
                                     "assets_path": assets_path}
            self._queue.append(job_id)
            snapshot = json.loads(json.dumps(record))
        background.ensure(self)
        background.wake(self.name)
        return snapshot

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return json.loads(json.dumps(record)) if record is not None else None

    def latest(self) -> dict[str, Any] | None:
        """The most recent job (the sheet picks up a running one when it opens)."""

        with self._lock:
            if not self._jobs:
                return None
            record = max(self._jobs.values(), key=lambda job: (job["created_at"], job["job_id"]))
            return json.loads(json.dumps(record))

    def cancel(self, job_id: str) -> tuple[dict[str, Any] | None, bool]:
        """Stop a job: ``(record, accepted)``; the child is ended at once."""

        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record["status"] in TERMINAL_STATUSES:
                return (json.loads(json.dumps(record)) if record else None), False
            self._cancel[job_id].set()
            process = self._process.get(job_id)
            if record["status"] == "queued":
                self._finish(record, "cancelled", error=CANCELLED_MESSAGE)
            else:
                record["cancel_requested"] = True
        if process is not None:
            self._terminate(process)
        return self.get(job_id), True

    def wait(self, timeout: float | None = None) -> None:
        """Block until no job is queued or running (tests)."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._queue and all(job["status"] in TERMINAL_STATUSES for job in self._jobs.values()):
                    return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("downloads still running")
            time.sleep(0.02)

    # --- lane --------------------------------------------------------------------------

    def step(self) -> float | None:
        with self._lock:
            if not self._queue:
                return None
            job_id = self._queue.pop(0)
            skip = self._jobs[job_id]["status"] in TERMINAL_STATUSES
            options = self._options.pop(job_id)
        if not skip:
            try:
                self._run(job_id, **options)
            except Exception as exc:  # pragma: no cover - _run records its own failures
                traceback.print_exc()
                self._update(job_id, finish="failed", error=f"The download failed: {exc}")
        return 0

    def stop(self) -> None:
        """Shutdown: end every running download (it resumes next time)."""

        with self._lock:
            running = list(self._process.values())
            for event in self._cancel.values():
                event.set()
        for process in running:
            _kill_group(process, signal.SIGKILL)

    # --- running -----------------------------------------------------------------------

    def _terminate(self, process: subprocess.Popen) -> None:
        """SIGTERM to the child's group, SIGKILL if it is still there after a grace period."""

        _kill_group(process, signal.SIGTERM)

        def finish() -> None:
            try:
                process.wait(KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                _kill_group(process, signal.SIGKILL)

        threading.Thread(target=finish, name="fortress-download-kill", daemon=True).start()

    def _command(self, wrapper: Path, kind: str, account: str, out_dir: Path | None,
                 names: list[str] | None) -> list[str]:
        command = [str(wrapper), kind, "--progress", "json", "--account", account]
        if kind == "notebooks":
            return [*command, "--json"]
        for name in names or []:
            command += ["--notebook", name]
        return [*command, "--out", str(out_dir)]

    def _environment(self, client_id: str) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("FORTRESS_TOKEN", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH")}
        environment.update(PATH=search_path(), PYTHONUNBUFFERED="1", **{CLIENT_ID_ENV: client_id})
        return environment

    def _run(self, job_id: str, *, client_id: str, db_path: Path, notes_path: Path, assets_path: Path) -> None:
        with self._lock:
            record = self._jobs[job_id]
            kind, account, names = record["kind"], record["account"], record["selected_notebooks"]
            folder_id = record["folder_id"]
        work = onenote_import.imports_dir(notes_path) / f"{job_id}-graph"
        out_dir = work / "OneNote"
        handed_over = False
        process: subprocess.Popen | None = None
        try:
            wrapper = _check_available()
            if kind == "download":
                out_dir.parent.mkdir(parents=True, exist_ok=True)
            self._update(job_id, status="starting")
            try:
                process = subprocess.Popen(
                    self._command(wrapper, kind, account, out_dir, names), cwd=str(wrapper.parent),
                    env=self._environment(client_id), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    start_new_session=True)
            except OSError as exc:
                raise GraphUnavailable(f"The OneNote download tool could not be started: {exc}") from exc
            with self._lock:
                self._process[job_id] = process
                cancelled = self._cancel[job_id].is_set()
            if cancelled:
                self._terminate(process)
            stdout: list[str] = []
            reader = threading.Thread(target=lambda: stdout.extend(process.stdout), daemon=True,
                                      name="fortress-download-stdout")
            reader.start()
            last_error = ""
            pages_started: tuple[float, int] | None = None
            for line in process.stderr:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if not isinstance(event, dict) or "event" not in event:
                    last_error = line
                    continue
                pages_started = self._apply(job_id, event, pages_started)
            process.wait()
            reader.join(10)
            with self._lock:
                self._process.pop(job_id, None)
                stopped = self._cancel[job_id].is_set()
            if stopped:
                self._update(job_id, finish="cancelled", error=CANCELLED_MESSAGE)
                return
            if process.returncode != 0:
                message = re.sub(rf"^{TOOL_NAME}:\s*", "", last_error) or f"exit code {process.returncode}"
                if process.returncode == EXIT_RESUMABLE:
                    message = f"Microsoft stopped answering for now ({message.rstrip('.')}). {RESUMABLE_HINT}"
                self._update(job_id, finish="failed", error=message,
                             resumable=process.returncode == EXIT_RESUMABLE)
                return
            output = self._last_json(stdout)
            if kind == "notebooks":
                listed = [item for item in output.get("notebooks") or [] if isinstance(item, dict)]
                self._update(job_id, finish="done", notebooks=[
                    {"name": str(item.get("name") or "Notebook"), "sections": int(item.get("sections") or 0)}
                    for item in listed])
                return
            if not (out_dir / "manifest.json").is_file():
                raise onenote_import.OneNoteImportError("The download finished but wrote no manifest.")
            self._update(job_id, status="importing", summary=output)
            imported = onenote_import.jobs.submit(
                onenote_import.jobs.new_id(), input_path=out_dir, input_name="OneNote (Microsoft)",
                owned_input=work, db_path=db_path, notes_path=notes_path, assets_path=assets_path,
                folder_id=folder_id)
            handed_over = True
            self._update(job_id, finish="done", import_job_id=imported["job_id"])
        except (GraphUnavailable, onenote_import.OneNoteImportError) as exc:
            self._update(job_id, finish="failed", error=str(exc))
        except Exception as exc:
            traceback.print_exc()
            self._update(job_id, finish="failed", error=f"The download failed: {exc}")
        finally:
            with self._lock:
                self._process.pop(job_id, None)
            if process is not None:
                if process.poll() is None:
                    _kill_group(process, signal.SIGKILL)
                    process.wait()
                for pipe in (process.stdout, process.stderr):
                    with contextlib.suppress(OSError):
                        pipe.close()
            if not handed_over:
                shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _last_json(lines: list[str]) -> dict[str, Any]:
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        return {}

    def _apply(self, job_id: str, event: dict[str, Any],
               pages_started: tuple[float, int] | None) -> tuple[float, int] | None:
        """One progress line into the record; returns when (and at which page) the
        page phase's estimate started counting."""

        kind = event.get("event")
        changes: dict[str, Any] = {}
        if kind == "phase":
            phase = str(event.get("phase") or "")
            changes.update(progress={"phase": phase, "done": 0, "total": 0, "current": None}, wait=None,
                           eta_seconds=None)
            if phase != "signin":
                changes["device_code"] = None
            if phase in _PHASE_STATUS:
                changes["status"] = _PHASE_STATUS[phase]
            if phase != "pages":
                pages_started = None
        elif kind == "progress":
            done, total = int(event.get("done") or 0), int(event.get("total") or 0)
            phase = str(event.get("phase") or "")
            changes.update(progress={"phase": phase, "done": done, "total": total, "current": event.get("current")},
                           wait=None, device_code=None)
            if phase == "pages":
                now = time.monotonic()
                if pages_started is None:
                    pages_started = (now, done)
                started_at, started_done = pages_started
                fetched, elapsed = done - started_done, now - started_at
                if fetched >= ETA_MIN_PAGES and elapsed >= ETA_MIN_S and total > done:
                    changes["eta_seconds"] = int(elapsed / fetched * (total - done))
                elif total <= done:
                    changes["eta_seconds"] = None
        elif kind == "wait":
            seconds = float(event.get("seconds") or 0)
            if seconds >= SHOWN_WAIT_S:
                changes["wait"] = {"seconds": seconds, "reason": str(event.get("reason") or ""),
                                   "until": _utc(seconds)}
        elif kind == "device_code":
            expires = float(event.get("expires_in") or 900)
            changes["device_code"] = {"user_code": str(event.get("user_code") or ""),
                                      "verification_uri": str(event.get("verification_uri") or
                                                              "https://microsoft.com/devicelogin"),
                                      "expires_at": _utc(expires)}
        elif kind == "warning":
            with self._lock:
                record = self._jobs[job_id]
                record["warning_count"] += 1
                if len(record["warnings"]) < MAX_WARNINGS:
                    record["warnings"].append(str(event.get("message") or ""))
        if changes:
            self._update(job_id, **changes)
        return pages_started

    def _update(self, job_id: str, finish: str | None = None, **changes: Any) -> None:
        with self._lock:
            record = self._jobs[job_id]
            if finish is not None:
                self._finish(record, finish, **changes)
            else:
                progress = changes.pop("progress", None)
                if progress is not None:
                    record["progress"] = {**record["progress"], **progress}
                record.update(changes)

    @staticmethod
    def _finish(record: dict[str, Any], status: str, **changes: Any) -> None:
        record.update(changes)
        record.update(status=status, finished_at=block_store.utc_now(), wait=None, device_code=None,
                      eta_seconds=None)
        record["progress"] = {**record["progress"], "current": None}


downloads = background.register(GraphDownloads())
