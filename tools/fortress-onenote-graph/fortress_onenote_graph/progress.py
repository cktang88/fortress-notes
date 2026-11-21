"""Progress output on stderr: readable lines, or JSON lines (``--progress json``) a
host app can parse. Every JSON line is one object with an ``event`` field:

* ``{"event": "phase", "phase": "signin|hierarchy|sections|pages|convert|done"}``
* ``{"event": "progress", "phase": "sections|pages", "done": n, "total": n, "current": "Notebook › Section › Page"}``
* ``{"event": "wait", "seconds": s, "reason": "..."}`` (throttling)
* ``{"event": "warning", "message": "..."}``
* ``{"event": "requests", "sent": n}`` at the end of a download
* ``{"event": "device_code", "user_code": "…", "verification_uri": "…", "expires_in": s}``
  when the browser sign-in did not work and the user must enter a code instead
* ``{"event": "info", "message": "..."}``

The final summary goes to stdout as one JSON line (like fortress-onenote).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO


class Progress:
    def __init__(self, mode: str = "text", stream: TextIO | None = None, quiet: bool = False):
        self.mode = mode
        self.stream = stream or sys.stderr
        self.quiet = quiet
        self._last_line = 0.0

    def _emit(self, event: dict[str, Any], text: str | None, important: bool = True) -> None:
        if self.mode == "json":
            self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.stream.flush()
        elif text and not (self.quiet and not important):
            self.stream.write(text + "\n")
            self.stream.flush()

    def phase(self, phase: str, text: str | None = None) -> None:
        self._emit({"event": "phase", "phase": phase}, text, important=False)

    def step(self, phase: str, done: int, total: int, current: str | None = None) -> None:
        now = time.monotonic()
        text = None
        if done == total or now - self._last_line >= 0.5:
            self._last_line = now
            text = f"[{done}/{total}] {current}" if current else f"[{done}/{total}]"
        self._emit({"event": "progress", "phase": phase, "done": done, "total": total, "current": current},
                   text, important=False)

    def wait(self, seconds: float, reason: str) -> None:
        self._emit({"event": "wait", "seconds": round(seconds, 1), "reason": reason},
                   f"waiting {seconds:.0f} s: {reason}", important=seconds >= 30)

    def warning(self, message: str) -> None:
        self._emit({"event": "warning", "message": message}, f"warning: {message}")

    def info(self, message: str) -> None:
        self._emit({"event": "info", "message": message}, message, important=False)

    def device_code(self, flow: dict[str, Any]) -> None:
        if self.mode == "json":
            self._emit({"event": "device_code", "user_code": flow.get("user_code"),
                        "verification_uri": flow.get("verification_uri"),
                        "expires_in": flow.get("expires_in"), "message": flow.get("message")}, None)

    def event(self, event: dict[str, Any]) -> None:
        self._emit(event, None)
