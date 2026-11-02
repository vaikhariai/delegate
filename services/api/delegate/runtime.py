"""Durable agent runtime: leased workers with fencing tokens and a bounded model loop.

A run needs work when ``runs.needs_step`` is true. A worker claims it by bumping ``fence`` and setting a lease.
Provider calls happen with no transaction open. Every mutation goes through ``executor.execute`` with the
worker's fence, so a worker whose lease was taken over cannot commit anything. ``needs_step`` is cleared only
if no buyer event arrived after the claim, so new input is never lost.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine

from delegate import executor, store
from delegate.clock import Clock
from delegate.db import exe, new_id, one, tx
from delegate.meta_adapter import CallRecord, MetaAdapter, ProviderError
from delegate.policy import ActionEnvelope, GateContext, evaluate, safe_message
from delegate.projections import public_context
from delegate.proposal import ProposalInvalid, parse_proposal
from delegate.settings import Settings

log = logging.getLogger("delegate.runtime")

LEASE_S = 120
STOP_CODES = {"FENCE_STALE", "RUN_NOT_LIVE", "MANDATE_REVOKED", "MANDATE_PAUSED", "MANDATE_EXPIRED", "MANDATE_NOT_ACTIVE", "OUT_OF_SCOPE"}

Proposer = Callable[[dict[str, Any]], dict[str, Any]]
_baselines: dict[str, Proposer] = {}


def register_baseline(system: str, fn: Proposer) -> None:
    """Register a no-model proposer (e.g. evaluation baseline B0) used for runs with that ``system``."""
    _baselines[system] = fn


def claim(engine: Engine, clock: Clock, worker_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    now = clock.now()
    with tx(engine) as conn:
        pick = one(
            conn,
            "SELECT run_id FROM runs WHERE needs_step AND state IN ('running', 'awaiting_buyer', 'waiting_approval', 'blocked') "
            "AND (lease_until IS NULL OR lease_until < :n)" + (" AND run_id = :rid" if run_id else "") +
            " ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED",
            n=now, rid=run_id,
        )
        if pick is None:
            return None
        return one(
            conn,
            "UPDATE runs SET lease_owner = :w, lease_until = :lu, fence = fence + 1, updated_at = :n WHERE run_id = :r RETURNING *",
            w=worker_id, lu=now + dt.timedelta(seconds=LEASE_S), n=now, r=pick["run_id"],
        )


def _extend(engine: Engine, clock: Clock, run_id: str, fence: int) -> bool:
    with tx(engine) as conn:
        return exe(conn, "UPDATE runs SET lease_until = :lu WHERE run_id = :r AND fence = :f",
                   lu=clock.now() + dt.timedelta(seconds=LEASE_S), r=run_id, f=fence) == 1


def _record_calls(engine: Engine, clock: Clock, run: dict[str, Any], step_no: int, calls: list[CallRecord]) -> str | None:
    last_id = None
    with tx(engine) as conn:
        for c in calls:
            last_id = new_id("pc")
            exe(
                conn,
                "INSERT INTO provider_calls (call_id, session_id, run_id, step_no, purpose, model, mode, prompt_version, status, "
                "http_status, latency_ms, usage, request_id, error_code, created_at) VALUES (:c, :s, :r, :st, :pu, :m, :mo, :pv, "
                ":status, :hs, :lat, CAST(:u AS jsonb), :rq, :ec, :n)",
                c=last_id, s=run["session_id"], r=run["run_id"], st=step_no, pu=c.purpose, m=c.model, mo=c.mode, pv=c.prompt_version,
                status=c.status, hs=c.http_status, lat=c.latency_ms, u=c.usage, rq=c.request_id, ec=c.error_code, n=clock.now(),
            )
        live = sum(1 for c in calls if c.mode == "live")
        exe(conn, "UPDATE runs SET provider_calls = provider_calls + :k WHERE run_id = :r", k=len(calls), r=run["run_id"])
        exe(conn, "UPDATE demo_sessions SET provider_calls = provider_calls + :k WHERE session_id = :s", k=live, s=run["session_id"])
    return last_id


def preview_fn(engine: Engine, clock: Clock, run: dict[str, Any], fence: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Read-only gate preview for the model's tool call. Never reveals thresholds; never executes."""

    def _preview(args: dict[str, Any]) -> dict[str, Any]:
        try:
            proposal = parse_proposal(args)
        except ProposalInvalid as exc:
            return {"verdict": "invalid", "reason_code": "SCHEMA_INVALID", "safe_message": str(exc)[:160]}
        with engine.connect() as conn:
            r = store.get_run(conn, run["session_id"], run["run_id"])
            m = store.mandate_row(conn, run["session_id"])
            snap = store.snapshot(conn, m)
            now = clock.now()
            env = ActionEnvelope(
                action_id="preview", session_id=run["session_id"], run_id=run["run_id"], item_id=r["item_id"],
                buyer_id=r["buyer_id"], mandate_id=m["mandate_id"], mandate_revision=snap.revision, created_at=now,
                expires_at=now + dt.timedelta(seconds=60), proposal=proposal, hold_seconds=snap.reservation_hold_seconds,
            )
            ctx: GateContext = executor._context(conn, clock, run["session_id"], r, fence, "preview", store.Principal(run["session_id"]).owner)
        d = evaluate(env, snap, ctx)
        verdict = d.verdict.value
        if verdict == "approval_required":
            return {"verdict": "allowed_after_seller_approval", "safe_message": "The seller will be asked to approve this exact reservation."}
        return {"verdict": verdict, "reason_code": d.reason_code, "safe_message": safe_message(d.reason_code) if d.reason_code else "Allowed."}

    return _preview


def _event(engine: Engine, clock: Clock, run: dict[str, Any], etype: str, actor: str, summary: str, data: dict[str, Any] | None = None, owner_only: bool = True) -> None:
    with tx(engine) as conn:
        store.append_event(conn, clock, run["session_id"], etype, actor, summary, run_id=run["run_id"], data=data, owner_only=owner_only)


def process(engine: Engine, clock: Clock, settings: Settings, adapter: MetaAdapter, run: dict[str, Any]) -> str:
    """Process one claimed step. Returns a short outcome label."""
    fence = int(run["fence"])
    claimed_at = clock.now()
    step_no = int(run["next_step"])
    session_id, run_id = run["session_id"], run["run_id"]
    outcome = "idle"
    transient_retries = 0
    attempt = 0
    denials = 0
    while attempt < 1 + settings.max_invalid_corrections:
        attempt += 1
        if not _extend(engine, clock, run_id, fence):
            return "fence_lost"
        with engine.connect() as conn:
            current = store.get_run(conn, session_id, run_id)
            if current["state"] in ("completed", "failed", "cancelled", "paused"):
                outcome = "run_not_live"
                break
            if int(current["provider_calls"]) >= settings.max_provider_calls_per_run:
                outcome = "budget_exhausted"
                break
            ctx = public_context(conn, session_id, run_id)
        origin = "live" if adapter.mode == "live" else adapter.mode
        baseline = _baselines.get(run["system"])
        calls: list[CallRecord] = []
        try:
            if baseline is not None:
                import json as _json

                raw: str | None = _json.dumps(baseline(ctx))
                calls = [CallRecord("negotiate", f"baseline-{run['system']}", "b0", "baseline")]
                origin = "b0"
            else:
                _event(engine, clock, run, "model.started", "agent", "The agent is reading the conversation.", {"mode": adapter.mode})
                result = adapter.next_action(ctx, preview_fn(engine, clock, run, fence), settings.max_previews_per_step)
                calls, raw = result.calls, result.raw_proposal
        except ProviderError as exc:
            pc = _record_calls(engine, clock, run, step_no, [CallRecord("negotiate", settings.model, adapter.mode, "delegate-negotiator-v1",
                                                                          status="error", error_code=exc.code)])
            del pc
            if exc.retryable and transient_retries < 2:
                transient_retries += 1
                attempt -= 1
                time.sleep(min(4.0, 0.8 * 2 ** transient_retries))
                continue
            with tx(engine) as conn:
                n = exe(conn, "UPDATE runs SET state = 'blocked', state_reason = :why, needs_step = false, lease_owner = NULL, lease_until = NULL, updated_at = :n "
                              "WHERE run_id = :r AND fence = :f AND state NOT IN ('completed', 'failed', 'cancelled')",
                        why=exc.code.lower(), n=clock.now(), r=run_id, f=fence)
                if n:
                    store.append_event(conn, clock, session_id, "model.failed", "agent",
                                       f"Live model unavailable: {exc}. Nothing was sent. Use Retry when ready.",
                                       run_id=run_id, data={"code": exc.code, "retryable": exc.retryable})
            return "provider_error"
        call_id = _record_calls(engine, clock, run, step_no, calls)
        if raw is None:
            raw = '{"__empty__": true}'
        action_id = executor.persist_proposal(engine, clock, session_id, run_id, step_no, attempt, raw, origin, call_id)
        res = executor.execute(engine, clock, session_id, action_id, fence=fence)
        _event(engine, clock, run, "model.completed", "agent", f"Agent step finished: {res.status}.",
               {"action_id": action_id, "verdict": res.verdict, "reason_code": res.reason_code})
        if res.verdict == "invalid":
            with tx(engine) as conn:
                exe(conn, "UPDATE runs SET invalid_corrections = invalid_corrections + 1 WHERE run_id = :r", r=run_id)
            outcome = "invalid"
            continue
        if res.verdict == "deny":
            if res.reason_code in STOP_CODES:
                outcome = "stale"
                break
            denials += 1
            outcome = "denied"
            if denials >= 2:
                break
            continue
        outcome = res.status
        break
    with tx(engine) as conn:
        newer = one(conn, "SELECT 1 AS x FROM buyer_events WHERE run_id = :r AND created_at > :c", r=run_id, c=claimed_at)
        if outcome in ("invalid", "denied"):
            exe(conn, "UPDATE runs SET state = 'blocked', state_reason = :why, updated_at = :n WHERE run_id = :r AND fence = :f "
                      "AND state IN ('running', 'awaiting_buyer')",
                why="agent_could_not_find_allowed_action", n=clock.now(), r=run_id, f=fence)
            store.append_event(conn, clock, session_id, "run.state_changed", "agent",
                               "The agent could not find an allowed reply within its retry limit. Send another buyer message or retry.",
                               run_id=run_id, owner_only=True)
        exe(conn, "UPDATE runs SET needs_step = CASE WHEN :newer THEN needs_step ELSE false END, next_step = next_step + 1, "
                  "lease_owner = NULL, lease_until = NULL, "
                  "state = CASE WHEN state = 'running' THEN 'awaiting_buyer' ELSE state END, updated_at = :n "
                  "WHERE run_id = :r AND fence = :f",
            newer=newer is not None, n=clock.now(), r=run_id, f=fence)
    return outcome


class Worker:
    def __init__(self, engine: Engine, clock: Clock, settings: Settings, adapter: MetaAdapter, n_threads: int = 3) -> None:
        self.engine, self.clock, self.settings, self.adapter = engine, clock, settings, adapter
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._loop, args=(i,), daemon=True, name=f"delegate-worker-{i}") for i in range(n_threads)]
        self._last_cleanup = 0.0

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self, idx: int) -> None:
        worker_id = f"w{idx}-{new_id('wk')}"
        while not self._stop.is_set():
            try:
                if idx == 0 and time.monotonic() - self._last_cleanup > 600:
                    self._last_cleanup = time.monotonic()
                    store.cleanup_expired(self.engine, self.clock)
                run = claim(self.engine, self.clock, worker_id)
                if run is None:
                    self._stop.wait(0.25)
                    continue
                process(self.engine, self.clock, self.settings, self.adapter, run)
            except Exception:  # noqa: BLE001 - a worker must survive any single failure
                log.exception("worker step failed")
                self._stop.wait(1.0)
