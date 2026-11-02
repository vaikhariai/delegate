"""Atomic spend caps, charged before every real provider HTTP request.

``charger`` returns a callable that reserves one call against the run cap (if a run is given), the session cap and the
global daily cap in one transaction. If any cap is full nothing is reserved and ``ProviderError`` is raised, so no path
can make a provider call without passing all caps, and concurrent requests cannot overshoot them. The daily counter
lives in ``daily_usage`` which is never deleted with a session.

Lock order is respected: runs, then demo_sessions, then the daily row.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine

from delegate.clock import Clock
from delegate.db import one, tx
from delegate.meta_adapter import ProviderError
from delegate.settings import Settings

Charge = Callable[[], None]


def charger(engine: Engine, clock: Clock, settings: Settings, session_id: str, run_id: str | None,
            per_attempt_cap: int | None = None) -> Charge:
    used = {"n": 0}

    def charge() -> None:
        if per_attempt_cap is not None and used["n"] >= per_attempt_cap:
            raise ProviderError("MODEL_BUDGET_EXHAUSTED", "This agent step reached its model call limit.", False)
        with tx(engine) as conn:
            if run_id is not None:
                r = one(conn, "UPDATE runs SET provider_calls = provider_calls + 1 WHERE run_id = :r AND provider_calls < :cap "
                              "RETURNING provider_calls", r=run_id, cap=settings.max_provider_calls_per_run)
                if r is None:
                    raise ProviderError("MODEL_BUDGET_EXHAUSTED", "This negotiation used its model budget.", False)
            s = one(conn, "UPDATE demo_sessions SET provider_calls = provider_calls + 1 WHERE session_id = :s AND provider_calls < :cap "
                          "RETURNING provider_calls", s=session_id, cap=settings.max_provider_calls_per_session)
            if s is None:
                raise ProviderError("MODEL_BUDGET_EXHAUSTED", "This demo session used its model budget.", False)
            d = one(conn, "INSERT INTO daily_usage (day, live_calls) VALUES (:d, 1) ON CONFLICT (day) DO UPDATE "
                          "SET live_calls = daily_usage.live_calls + 1 WHERE daily_usage.live_calls < :cap RETURNING live_calls",
                    d=clock.now().date(), cap=settings.max_live_calls_per_day)
            if d is None:
                raise ProviderError("DEMO_DAILY_BUDGET_EXHAUSTED", "The public demo reached its daily model budget.", False)
        used["n"] += 1

    return charge
