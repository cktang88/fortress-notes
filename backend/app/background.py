"""The one owner of background work.

Every job the backend does outside a request runs here, each on its own
*lane* (a dedicated thread, never the asyncio default executor that serves
search and Ask):

========== ======== ===========================================================
lane       priority what
========== ======== ===========================================================
mirrors    high     Markdown mirrors follow SQLite (``documents.MirrorWriter``)
imports    normal   OneNote import jobs, one at a time (``onenote_import.jobs``)
backups    normal   the daily snapshot (``backups.DailyBackup``)
ocr        low      text in pictures and PDFs (``ocr.OcrQueue``)
index      low      ColBERT embeddings (``embeddings.Reindexer``)
========== ======== ===========================================================

A lane runs its service's :meth:`Service.step` (one small unit of work) until
the service says there is nothing left, then sleeps until :func:`wake` or its
poll interval. Low-priority lanes wait (briefly) while a search is scoring
(:func:`foreground`), so a search never competes with OCR or indexing for the
cores.

:func:`paused` stops every lane between units (the database restore swaps the
database with no background writer active); :func:`run_inline` runs a lane's
work in the caller's thread, serialized with the lane itself (flushing mirrors
before a restore or at shutdown). :func:`stop` (lifespan shutdown) stops every
lane and the child processes their services started (the OneNote converter).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger("fortress.background")

HIGH, NORMAL, LOW = 0, 1, 2
# How long a low-priority unit waits for running searches to finish.
FOREGROUND_YIELD_S = 1.0
ERROR_BACKOFF_S = 5.0


class Busy(RuntimeError):
    """Background work that cannot be paused right now (e.g. an import is running)."""


class Service:
    """One kind of background work. Subclasses set the class attributes and ``step``."""

    name = "service"
    priority = NORMAL
    # Seconds between checks when idle (None: only when woken).
    poll: float | None = None
    # A unit that may run for minutes (an import): :func:`paused` refuses to wait for it.
    long_running = False

    def step(self) -> float | None:
        """Do one unit of work. Returns 0 to run again at once, a number of
        seconds to wait, or None to wait for :func:`wake` (or ``poll``)."""

        raise NotImplementedError

    def stop(self) -> None:
        """The backend is shutting down: end child processes, interrupt waits."""


class _Lane:
    def __init__(self, runner: Runner, service: Service) -> None:
        self.runner = runner
        self.service = service
        self.wakeup = threading.Event()
        self.step_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stopping = False
        self.working = False

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.wakeup.set()
            return
        self.stopping = False
        self.wakeup.set()  # look for work left from before at once
        self.thread = threading.Thread(
            target=self._run, name=f"fortress-{self.service.name}", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        runner = self.runner
        while True:
            with runner.changed:
                while runner.pauses and not self.stopping:
                    runner.changed.wait()
                if self.stopping:
                    return
                self.working = True
            delay: float | None
            try:
                if self.service.priority >= LOW:
                    runner.yield_to_foreground()
                with self.step_lock:
                    delay = self.service.step()
            except Exception:
                log.exception("background %s failed", self.service.name)
                delay = ERROR_BACKOFF_S
            finally:
                with runner.changed:
                    self.working = False
                    runner.changed.notify_all()
            if delay == 0:
                continue
            self.wakeup.wait(self.service.poll if delay is None else delay)
            self.wakeup.clear()

    def stop(self, timeout: float) -> None:
        with self.runner.changed:
            self.stopping = True
            self.runner.changed.notify_all()
        self.wakeup.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self.thread = None


class Runner:
    def __init__(self) -> None:
        self.lanes: dict[str, _Lane] = {}
        self.changed = threading.Condition()
        self.pauses = 0
        self._foreground = 0
        self._foreground_lock = threading.Lock()

    # --- lanes ---------------------------------------------------------------------------

    def register(self, service: Service) -> Service:
        """Add a lane for ``service`` (not started; replaces one of the same name)."""

        old = self.lanes.get(service.name)
        if old is not None:
            old.stop(5.0)
        self.lanes[service.name] = _Lane(self, service)
        return service

    def service(self, name: str) -> Service:
        return self.lanes[name].service

    def start(self, *names: str) -> None:
        for name in names:
            self.lanes[name].start()

    def ensure(self, service: Service) -> None:
        """Run ``service`` on its lane: registered (replacing another of its name)
        and started, if it was not already."""

        lane = self.lanes.get(service.name)
        if lane is None or lane.service is not service:
            self.register(service)
        self.start(service.name)

    def halt(self, name: str, timeout: float = 5.0) -> None:
        """Stop one lane (it can be started again)."""

        lane = self.lanes.get(name)
        if lane is not None:
            lane.stop(timeout)

    def running(self, name: str) -> bool:
        lane = self.lanes.get(name)
        return lane is not None and lane.thread is not None and lane.thread.is_alive()

    def wake(self, name: str) -> None:
        """Work arrived for ``name``: run it soon (no-op while that lane is stopped)."""

        lane = self.lanes.get(name)
        if lane is not None:
            lane.wakeup.set()

    def busy(self, name: str) -> bool:
        lane = self.lanes.get(name)
        return bool(lane and lane.working)

    def run_inline(self, name: str, max_steps: int = 1_000_000) -> None:
        """Do ``name``'s pending work now in this thread (serialized with the lane)."""

        lane = self.lanes[name]
        with lane.step_lock:
            for _ in range(max_steps):
                if lane.service.step() != 0:
                    return

    @contextmanager
    def exclusive(self, name: str) -> Iterator[Service]:
        """Hold ``name``'s lane between units: its work is done here meanwhile."""

        lane = self.lanes[name]
        with lane.step_lock:
            yield lane.service

    def stop(self, timeout: float = 5.0) -> None:
        """Stop every lane: child processes first, then the threads."""

        for lane in self.lanes.values():
            try:
                lane.service.stop()
            except Exception:  # pragma: no cover - shutdown goes on
                log.exception("stopping %s failed", lane.service.name)
        deadline = time.monotonic() + timeout
        for lane in self.lanes.values():
            lane.stop(max(0.0, deadline - time.monotonic()))

    # --- pause ---------------------------------------------------------------------------

    @contextmanager
    def paused(self, timeout: float = 30.0) -> Iterator[None]:
        """No lane runs a unit while inside (they finish the unit in progress first).

        Raises :class:`Busy` when a long-running unit (an import) is in progress,
        or the units in progress don't finish within ``timeout``.
        """

        with self.changed:
            self.pauses += 1
        try:
            deadline = time.monotonic() + timeout
            with self.changed:
                while True:
                    working = [lane for lane in self.lanes.values() if lane.working]
                    if not working:
                        break
                    long = [lane.service.name for lane in working if lane.service.long_running]
                    left = deadline - time.monotonic()
                    if long or left <= 0:
                        names = long or [lane.service.name for lane in working]
                        raise Busy(f"background work is running: {', '.join(names)}")
                    self.changed.wait(left)
            yield
        finally:
            with self.changed:
                self.pauses -= 1
                self.changed.notify_all()
            for lane in self.lanes.values():
                lane.wakeup.set()

    def wait_idle(self, name: str, idle: Callable[[], bool], timeout: float = 30.0) -> bool:
        """Wait until ``name`` is between units and ``idle()`` says nothing is left."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lane = self.lanes.get(name)
            if lane is None or (not lane.working and not lane.wakeup.is_set() and idle()):
                return True
            time.sleep(0.02)
        return False

    # --- foreground ----------------------------------------------------------------------

    @contextmanager
    def foreground(self) -> Iterator[None]:
        """Mark CPU-heavy request work (scoring a search): low lanes hold off meanwhile."""

        with self._foreground_lock:
            self._foreground += 1
        try:
            yield
        finally:
            with self._foreground_lock:
                self._foreground -= 1

    def foreground_active(self) -> bool:
        return self._foreground > 0

    def yield_to_foreground(self, limit: float = FOREGROUND_YIELD_S) -> None:
        deadline = time.monotonic() + limit
        while self._foreground and time.monotonic() < deadline:
            time.sleep(0.005)


runner = Runner()

register = runner.register
start = runner.start
ensure = runner.ensure
halt = runner.halt
running = runner.running
wake = runner.wake
busy = runner.busy
run_inline = runner.run_inline
exclusive = runner.exclusive
stop = runner.stop
paused = runner.paused
foreground = runner.foreground
foreground_active = runner.foreground_active
yield_to_foreground = runner.yield_to_foreground
