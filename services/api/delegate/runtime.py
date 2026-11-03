"""Durable agent runtime: leased workers with fencing tokens and a bounded model loop.

A run needs work when ``runs.needs_step`` is true. A worker claims it by bumping ``fence`` and setting a lease.
Provider calls happen with no transaction open. Every mutation goes through ``executor.execute`` with the
worker's fence, so a worker whose lease was taken over cannot commit anything. ``needs_step`` is cleared only
if no buyer event arrived after the claim, so new input is never lost.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import DataError

from delegate import budget, executor, obs, signals, store
from delegate.clock import Clock
from delegate.db import exe, new_id, one, rows, tx
from delegate.meta_adapter import PROMPT_VERSION, CallRecord, MetaAdapter, ProviderError
from delegate.policy import ActionEnvelope, GateContext, evaluate, safe_message
from delegate.projections import public_context
from delegate.proposal import ProposalInvalid, parse_proposal
from delegate.settings import Settings

log = logging.getLogger("delegate.runtime")

LEASE_S = 300
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
            "SELECT r.run_id FROM runs r JOIN mandates m ON m.mandate_id = r.mandate_id "
            "WHERE r.needs_step AND r.state IN ('running', 'awaiting_buyer', 'waiting_approval', 'blocked') "
            "AND m.status = 'active' AND (r.lease_until IS NULL OR r.lease_until < :n)"
            + (" AND r.run_id = :rid" if run_id else "") +
            " ORDER BY r.updated_at LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
            n=now, rid=run_id,
        )
        if pick is None:
            return None
        return one(
            conn,
            "UPDATE runs SET lease_owner = :w, lease_until = :lu, fence = fence + 1, claimed_input_seq = input_seq, updated_at = :n "
            "WHERE run_id = :r RETURNING *",
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
        for c in calls:
            log.info("provider_call", extra={
                "event": "provider_call", "session": obs.session_ref(run["session_id"]), "purpose": c.purpose, "model": c.model,
                "mode": c.mode, "status": c.status, "http_status": c.http_status, "latency_ms": c.latency_ms,
                "prompt_tokens": (c.usage or {}).get("prompt_tokens"), "completion_tokens": (c.usage or {}).get("completion_tokens"),
                "request_id": c.request_id, "error_code": c.error_code,
            })
    # Counters are not incremented here: budget.charger reserves each live call atomically before it is sent.
    return last_id


NULLABLE_FIELDS = ("message_intent", "price_minor", "currency", "clarify_topic", "decline_reason", "acceptance_event_id")


def preview_args(args: Any) -> Any:
    """Read the literal string "null" as null in the nullable fields of preview_action arguments.

    The provider does not constrain tool arguments to the schema (also not with ``strict``): live, muse-spark-1.3 wrote
    "null" for empty enum fields, every preview failed, and the model then wrapped its final JSON in prose. The final
    proposal is still parsed strictly; this applies to the read-only preview only.
    """
    if not isinstance(args, dict):
        return args
    return {k: (None if k in NULLABLE_FIELDS and v == "null" else v) for k, v in args.items()}


def preview_fn(engine: Engine, clock: Clock, run: dict[str, Any], fence: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Read-only gate preview for the model's tool call. Never executes.

    The preview evaluates prices without the private minimum (only the public asking price bounds it), so it cannot be
    used to binary-search the floor. The real gate, with the minimum, runs at execution.
    """

    def _preview(args: dict[str, Any]) -> dict[str, Any]:
        try:
            proposal = parse_proposal(preview_args(args))
        except ProposalInvalid as exc:
            return {"verdict": "invalid", "reason_code": "SCHEMA_INVALID", "safe_message": str(exc)[:160]}
        with engine.connect() as conn:
            r = store.get_run(conn, run["session_id"], run["run_id"])
            m = store.mandate_row(conn, run["session_id"])
            snap = dataclasses.replace(store.snapshot(conn, m), min_price_minor=1)
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
            sess = one(conn, "SELECT provider_calls FROM demo_sessions WHERE session_id = :s", s=session_id)
            if int(current["provider_calls"]) >= settings.max_provider_calls_per_run or (
                sess is not None and int(sess["provider_calls"]) >= settings.max_provider_calls_per_session
            ):
                outcome = "budget_exhausted"
                break
            m = store.mandate_row(conn, session_id)
            if m["status"] != "active" or (m["expires_at"] is not None and clock.now() >= m["expires_at"]):
                outcome = "mandate_not_active"
                break
            day = one(conn, "SELECT live_calls AS n FROM daily_usage WHERE day = :d", d=clock.now().date())
            if day is not None and int(day["n"]) >= settings.max_live_calls_per_day:
                outcome = "global_budget_exhausted"
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
                charge = budget.charger(engine, clock, settings, session_id, run_id, per_attempt_cap=settings.max_provider_calls_per_step)
                result = adapter.next_action(ctx, preview_fn(engine, clock, run, fence), settings.max_previews_per_step, charge=charge)
                calls, raw = result.calls, result.raw_proposal
        except ProviderError as exc:
            _record_calls(engine, clock, run, step_no, exc.calls or [CallRecord("negotiate", settings.model, adapter.mode, PROMPT_VERSION,
                                                                                 status="error", error_code=exc.code)])
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
        if not _extend(engine, clock, run_id, fence):
            return "fence_lost"
        if raw is None:
            raw = '{"__empty__": true}'
        try:
            action_id = executor.persist_proposal(engine, clock, session_id, run_id, step_no, attempt, raw, origin, call_id)
        except (ValueError, UnicodeError, DataError) as exc:  # e.g. control characters the database cannot store
            log.warning("unstorable proposal", extra={"event": "invalid_output", "session": obs.session_ref(session_id), "error": type(exc).__name__})
            raw = json.dumps({"unstorable_output": True})
            action_id = executor.persist_proposal(engine, clock, session_id, run_id, step_no, attempt, raw, origin, call_id)
        res = executor.execute(engine, clock, session_id, action_id, fence=fence)
        _event(engine, clock, run, "model.completed", "agent", f"Agent step finished: {res.status}.",
               {"action_id": action_id, "verdict": res.verdict, "reason_code": res.reason_code})
        if res.reason_code == "FENCE_STALE":
            return "fence_lost"  # another worker owns the run now; leave needs_step and state to it
        if res.verdict == "invalid":
            with tx(engine) as conn:
                exe(conn, "UPDATE runs SET invalid_corrections = invalid_corrections + 1 WHERE run_id = :r AND fence = :f", r=run_id, f=fence)
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
        # Lock the run first: buyer input and this release serialize on the row, and input_seq decides atomically.
        mine = one(conn, "SELECT fence FROM runs WHERE run_id = :r FOR UPDATE", r=run_id)
        if mine is None or int(mine["fence"]) != fence:
            return "fence_lost"
        if outcome == "mandate_not_active":
            exe(conn, "UPDATE runs SET lease_owner = NULL, lease_until = NULL WHERE run_id = :r", r=run_id)
            return outcome  # needs_step stays set; the run is picked up again when the mandate is active
        if outcome in ("budget_exhausted", "global_budget_exhausted"):
            why = "model_budget_exhausted" if outcome == "budget_exhausted" else "demo_daily_budget_exhausted"
            exe(conn, "UPDATE runs SET state = 'blocked', state_reason = :why, updated_at = :n WHERE run_id = :r", why=why, n=clock.now(), r=run_id)
            store.append_event(conn, clock, session_id, "run.state_changed", "system",
                               "This demo negotiation used its model budget. Start a new scenario to continue."
                               if outcome == "budget_exhausted" else
                               "The public demo reached its daily model budget. Please try again tomorrow.", run_id=run_id)
        if outcome in ("invalid", "denied"):
            exe(conn, "UPDATE runs SET state = 'blocked', state_reason = :why, updated_at = :n WHERE run_id = :r "
                      "AND state IN ('running', 'awaiting_buyer')",
                why="agent_could_not_find_allowed_action", n=clock.now(), r=run_id)
            store.append_event(conn, clock, session_id, "run.state_changed", "agent",
                               "The agent could not find an allowed reply within its retry limit. Send another buyer message or retry.",
                               run_id=run_id, owner_only=True)
        exe(conn, "UPDATE runs SET needs_step = (input_seq <> COALESCE(claimed_input_seq, -1)), next_step = next_step + 1, "
                  "lease_owner = NULL, lease_until = NULL, "
                  "state = CASE WHEN state = 'running' THEN 'awaiting_buyer' ELSE state END, updated_at = :n "
                  "WHERE run_id = :r",
            n=clock.now(), r=run_id)
    return outcome


def ops_summary(engine: Engine, clock: Clock) -> dict[str, Any]:
    """Counts for dashboards and alerts: sessions, runs by state, provider calls, tokens and errors in the last hour."""
    hour = clock.now() - dt.timedelta(hours=1)
    with engine.connect() as conn:
        s = one(conn, "SELECT count(*) AS n FROM demo_sessions WHERE expires_at > :n", n=clock.now())
        by_state = {r["state"]: int(r["n"]) for r in rows(conn, "SELECT state, count(*) AS n FROM runs GROUP BY state")}
        calls = one(conn, "SELECT count(*) FILTER (WHERE mode = 'live') AS live, count(*) FILTER (WHERE status <> 'ok') AS errors, "
                          "COALESCE(sum((usage->>'prompt_tokens')::bigint), 0) AS prompt_tokens, "
                          "COALESCE(sum((usage->>'completion_tokens')::bigint), 0) AS completion_tokens "
                          "FROM provider_calls WHERE created_at > :h", h=hour)
    assert s is not None and calls is not None
    return {"active_sessions": int(s["n"]), "runs_by_state": by_state, "live_calls_last_hour": int(calls["live"]),
            "provider_errors_last_hour": int(calls["errors"]), "prompt_tokens_last_hour": int(calls["prompt_tokens"]),
            "completion_tokens_last_hour": int(calls["completion_tokens"])}


class Worker:
    def __init__(self, engine: Engine, clock: Clock, settings: Settings, adapter: MetaAdapter, n_threads: int = 3) -> None:
        self.engine, self.clock, self.settings, self.adapter = engine, clock, settings, adapter
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._loop, args=(i,), daemon=True, name=f"delegate-worker-{i}") for i in range(n_threads)]
        self._last_cleanup = 0.0
        self._last_sweep = 0.0
        self._last_stats = 0.0

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            signals.notify_work()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self, idx: int) -> None:
        worker_id = f"w{idx}-{new_id('wk')}"
        while not self._stop.is_set():
            try:
                if idx == 0 and time.monotonic() - self._last_sweep > 10:
                    self._last_sweep = time.monotonic()
                    store.sweep(self.engine, self.clock)
                if idx == 0 and time.monotonic() - self._last_stats > 60:
                    self._last_stats = time.monotonic()
                    log.info("ops_summary", extra={"event": "ops_summary", **ops_summary(self.engine, self.clock)})
                if idx == 0 and time.monotonic() - self._last_cleanup > 600:
                    self._last_cleanup = time.monotonic()
                    store.cleanup_expired(self.engine, self.clock)
                run = claim(self.engine, self.clock, worker_id)
                if run is None:
                    signals.wait_for_work(2.0)  # woken on new buyer input; the timeout is a fallback poll
                    continue
                process(self.engine, self.clock, self.settings, self.adapter, run)
            except Exception:  # noqa: BLE001 - a worker must survive any single failure
                log.exception("worker step failed")
                self._stop.wait(1.0)
