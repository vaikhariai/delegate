"""Injectable clock. Live sessions use real UTC time; deterministic tests use FixedClock."""

from __future__ import annotations

import datetime as dt
import threading


class Clock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


class FixedClock(Clock):
    def __init__(self, start: dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> dt.datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> dt.datetime:
        with self._lock:
            self._now = self._now + dt.timedelta(seconds=seconds)
            return self._now
