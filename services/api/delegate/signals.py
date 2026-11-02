"""In-process wake-up for agent workers (single-instance deployment). Polling remains as a slow fallback."""

from __future__ import annotations

import threading

_work = threading.Condition()
_pending = 0


def notify_work() -> None:
    """Call after committing buyer input or anything else that sets runs.needs_step."""
    global _pending
    with _work:
        _pending += 1
        _work.notify()


def wait_for_work(timeout: float) -> None:
    """Block until notified or until ``timeout`` seconds pass."""
    global _pending
    with _work:
        if _pending == 0:
            _work.wait(timeout)
        if _pending > 0:
            _pending -= 1
