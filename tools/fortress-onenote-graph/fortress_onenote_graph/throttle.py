"""Self-paced request budget for the OneNote part of Microsoft Graph.

OneNote limits delegated access to **120 requests per minute, 400 per hour and
5 concurrent requests** per app and user, and its 429 answers carry no
``Retry-After`` (docs/onenote-import.md §2a). So the tool paces itself:

* a sliding-window budget a little under each limit (110/min, 380/h), shared
  with earlier runs through a small state file, so a restart within the hour
  does not blow the hourly budget;
* one request at a time (well under the concurrency limit);
* adaptive backoff: a 429 pauses everything (60 s, doubling per consecutive
  429 up to 10 min; a ``Retry-After`` is honoured if Graph ever sends one) and
  adds spacing between requests, which decays again on success;
* ``x-ms-throttle-limit-percentage`` above 80 % adds spacing before a 429 happens.

The clock and ``sleep`` are injectable so tests run instantly.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MINUTE_LIMIT = 110
HOUR_LIMIT = 380
BACKOFF_FIRST_S = 60.0
BACKOFF_MAX_S = 600.0
SPACING_STEP_S = 0.5
SPACING_MAX_S = 5.0


@dataclass
class Clock:
    """Wall clock (``time.time``/``time.sleep``); tests pass a fake."""

    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Throttle:
    clock: Clock = field(default_factory=Clock)
    minute_limit: int = MINUTE_LIMIT
    hour_limit: int = HOUR_LIMIT
    state_path: Path | None = None
    on_wait: Callable[[float, str], None] | None = None  # (seconds, reason): progress output
    _sent: deque = field(default_factory=deque)
    _spacing: float = 0.0
    _paused_until: float = 0.0
    _consecutive_429: int = 0
    _last: float = 0.0

    def __post_init__(self) -> None:
        if self.state_path is not None:
            try:
                stored = json.loads(self.state_path.read_text(encoding="utf-8"))
                now = self.clock.now()
                self._sent.extend(sorted(t for t in stored.get("sent", []) if isinstance(t, (int, float))
                                         and now - 3600 < t <= now))
                self._paused_until = float(stored.get("paused_until") or 0.0)
                self._spacing = min(float(stored.get("spacing") or 0.0), SPACING_MAX_S)
            except (OSError, ValueError, TypeError):
                pass

    # --- budget ----------------------------------------------------------------------

    def delay(self) -> tuple[float, str]:
        """Seconds to wait before the next request may go out, and why."""

        now = self.clock.now()
        self._expire(now)
        waits = [(0.0, "")]
        if self._paused_until > now:
            waits.append((self._paused_until - now, "throttled by Microsoft (429), backing off"))
        if self._spacing and self._last:
            waits.append((self._last + self._spacing - now, "spacing requests"))
        minute = [t for t in self._sent if t > now - 60]
        if len(minute) >= self.minute_limit:
            waits.append((minute[len(minute) - self.minute_limit] + 60 - now, "per-minute limit"))
        if len(self._sent) >= self.hour_limit:
            waits.append((self._sent[len(self._sent) - self.hour_limit] + 3600 - now,
                          f"hourly limit ({self.hour_limit} requests per hour)"))
        return max(waits, key=lambda item: item[0])

    def acquire(self) -> None:
        """Block until a request may be sent, then count it."""

        while True:
            wait, reason = self.delay()
            if wait <= 0:
                break
            if self.on_wait is not None and wait >= 1:
                self.on_wait(wait, reason)
            self.clock.sleep(wait)
        now = self.clock.now()
        self._sent.append(now)
        self._last = now
        self._save()

    def _expire(self, now: float) -> None:
        while self._sent and self._sent[0] <= now - 3600:
            self._sent.popleft()

    # --- feedback ----------------------------------------------------------------------

    def success(self, limit_percentage: float | None = None) -> None:
        self._consecutive_429 = 0
        if limit_percentage is not None and limit_percentage >= 80:
            self._spacing = min(SPACING_MAX_S, self._spacing + SPACING_STEP_S)
        else:
            self._spacing = self._spacing * 0.9 if self._spacing > 0.05 else 0.0

    def throttled(self, retry_after: float | None = None) -> float:
        """A 429 (or 503 with Retry-After): pause; returns the pause in seconds."""

        backoff = min(BACKOFF_MAX_S, BACKOFF_FIRST_S * (2 ** self._consecutive_429))
        if retry_after is not None and retry_after > 0:
            backoff = min(BACKOFF_MAX_S, max(retry_after, 1.0))
        self._consecutive_429 += 1
        self._spacing = min(SPACING_MAX_S, self._spacing + SPACING_STEP_S)
        self._paused_until = max(self._paused_until, self.clock.now() + backoff)
        self._save()
        return backoff

    @property
    def spacing(self) -> float:
        return self._spacing

    def _save(self) -> None:
        if self.state_path is None:
            return
        data = {"sent": list(self._sent), "paused_until": self._paused_until, "spacing": self._spacing}
        try:
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError:
            pass
