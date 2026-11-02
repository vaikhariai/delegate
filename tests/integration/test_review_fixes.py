"""Regression tests for the independent backend review (3 Nov 2026). One test per finding."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from delegate import executor, runtime, store
from delegate.app import client_ip
from delegate.clock import Clock
from delegate.db import exe, one, rows, tx
from tests.integration.helpers import Harness
from tests.integration.test_authority_and_races import approve, reservations, to_pending_approval

PAST = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture()
def h(engine: Engine, real_clock: Clock) -> Iterator[Harness]:
    harness = Harness(engine, real_clock)
    yield harness
    harness.close()
    for name in ("before_locks", "after_locks", "before_commit", "after_commit"):
        executor.set_hook(name, None)


def _run(h: Harness, run_id: str) -> dict[str, Any]:
    with h.engine.connect() as c:
        r = one(c, "SELECT * FROM runs WHERE run_id = :r", r=run_id)
    assert r is not None
    return r


# Finding 1: buyer input that arrives while a step runs must not be dropped.
def test_buyer_message_during_a_step_keeps_needs_step(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "Is it available?")
    claimed = runtime.claim(h.engine, h.clock, "w1", run_id)
    assert claimed is not None
    h.say(run_id, "And how much memory?")  # arrives mid-step
    runtime.process(h.engine, h.clock, h.settings, h.adapter, claimed)
    assert _run(h, run_id)["needs_step"] is True
    assert h.step(run_id) == "succeeded"
    assert _run(h, run_id)["needs_step"] is False


# Finding 2: revoke must not deadlock with a buyer turn holding the run row.
def test_revoke_does_not_deadlock_with_a_concurrent_buyer_turn(h: Harness) -> None:
    st = to_pending_approval(h)
    holding, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def buyer_like_tx() -> None:  # same order as store.buyer_turn: run row, then the session row
        try:
            with tx(h.engine) as c:
                c.execute(text("SELECT 1 FROM runs WHERE run_id = :r FOR UPDATE"), {"r": st["run_id"]})
                holding.set()
                assert release.wait(10)
                c.execute(text("UPDATE demo_sessions SET buyer_turns = buyer_turns + 1 WHERE session_id = :s"), {"s": st["session_id"]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=buyer_like_tx)
    t.start()
    assert holding.wait(10)
    mid = h.current()["mandate"]["mandate_id"]
    result: dict[str, Any] = {}
    t2 = threading.Thread(target=lambda: result.setdefault("r", h.client.post(f"/v1/mandates/{mid}/revoke", headers=h.h())))
    t2.start()
    t2.join(1.0)
    release.set()
    t.join(10)
    t2.join(10)
    assert not errors
    assert result["r"].status_code == 200 and result["r"].json()["status"] == "revoked"


# Finding 3: a lapsed hold frees the item for another buyer without a 500.
def test_lapsed_hold_lets_another_buyer_reserve(h: Harness) -> None:
    a = to_pending_approval(h, "buyer-riya")
    assert approve(h, a["approval"]).json()["receipt_verdict"] == "verified"
    with tx(h.engine) as c:
        exe(c, "UPDATE marketplace_reservations SET created_at = :c, expires_at = :e WHERE session_id = :s",
            c=PAST - dt.timedelta(minutes=15), e=PAST, s=a["session_id"])
    b = to_pending_approval(h, "buyer-arjun", new_session=False)
    r = approve(h, b["approval"])
    assert r.status_code == 200 and r.json()["receipt_verdict"] == "verified"
    statuses = sorted(x["status"] for x in reservations(h.engine, a["session_id"]))
    assert statuses == ["active", "expired"]


# Finding 4: an expired approval is recorded as expired and the agent can ask again.
def test_expired_approval_is_persisted_and_does_not_wedge_the_run(h: Harness) -> None:
    st = to_pending_approval(h)
    with tx(h.engine) as c:
        exe(c, "UPDATE approvals SET expires_at = :e WHERE approval_id = :a", e=PAST, a=st["approval"]["approval_id"])
    assert approve(h, st["approval"]).status_code == 410
    with h.engine.connect() as c:
        ap = one(c, "SELECT status FROM approvals WHERE approval_id = :a", a=st["approval"]["approval_id"])
    assert ap is not None and ap["status"] == "expired"
    assert _run(h, st["run_id"])["state"] == "awaiting_buyer"
    assert h.step(st["run_id"]) == "waiting_approval"  # the unused acceptance gets a fresh approval request
    fresh = h.run_view(st["run_id"])["pending_approval"]
    assert fresh is not None and fresh["approval_id"] != st["approval"]["approval_id"]


def test_sweep_expires_stale_approvals_without_a_click(h: Harness) -> None:
    st = to_pending_approval(h)
    with tx(h.engine) as c:
        exe(c, "UPDATE approvals SET expires_at = :e WHERE approval_id = :a", e=PAST, a=st["approval"]["approval_id"])
    assert store.sweep(h.engine, h.clock)["approvals_expired"] == 1
    assert _run(h, st["run_id"])["state"] == "awaiting_buyer"


# Finding 5: an expired mandate ends live runs instead of leaving them stuck.
def test_mandate_expiry_ends_live_runs(h: Harness) -> None:
    st = to_pending_approval(h)
    with tx(h.engine) as c:
        exe(c, "UPDATE mandates SET expires_at = :e WHERE session_id = :s", e=PAST, s=st["session_id"])
    assert store.sweep(h.engine, h.clock)["mandates_expired"] == 1
    v = h.run_view(st["run_id"])
    assert v["state"] == "cancelled" and v["state_reason"] == "mandate_expired" and v["pending_approval"] is None
    assert h.client.get(f"/v1/runs/{st['run_id']}/buyer-view", headers=h.h()).json()["negotiation_open"] is False


# Finding 6: the client-supplied left-most X-Forwarded-For value is ignored.
def test_client_ip_uses_the_platform_appended_hop() -> None:
    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")], "client": ("10.0.0.1", 1234)}
    assert client_ip(Request(scope)) == "203.0.113.9"
    assert client_ip(Request({"type": "http", "headers": [], "client": ("198.51.100.4", 1)})) == "198.51.100.4"


def test_spoofed_forwarded_for_cannot_bypass_the_session_limit(engine: Engine, real_clock: Clock) -> None:
    from tests.integration.helpers import mock_settings

    hh = Harness(engine, real_clock, mock_settings(sessions_per_ip_per_hour=2))
    try:
        codes = [hh.client.post("/v1/demo-sessions", headers={"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7"}).status_code
                 for i in range(3)]
        assert codes == [201, 201, 429]
    finally:
        hh.close()


# Finding 7: no model call while paused; resuming answers the waiting buyer.
def test_pause_defers_buyer_input_and_resume_answers_it(h: Harness) -> None:
    h.new_session()
    mid = h.activate()
    run_id = h.start_run()
    assert h.client.post(f"/v1/mandates/{mid}/pause", headers=h.h()).status_code == 200
    h.say(run_id, "How much memory?")
    assert runtime.claim(h.engine, h.clock, "w", run_id) is None  # not claimable: no provider call while paused
    assert h.client.post(f"/v1/mandates/{mid}/resume", headers=h.h()).status_code == 200
    assert h.step(run_id) == "succeeded"
    assert [c for c in h.run_view(run_id)["conversation"] if c["role"] == "seller"]


# Finding 8: an idempotency key reused with a different body is a conflict, not a fake success.
def test_idempotency_key_reuse_with_different_body_is_rejected(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    assert h.say(run_id, "first", key="same-key").status_code == 202
    r = h.say(run_id, "second", key="same-key")
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


# Finding 9: a verification interrupted after commit is completed by the next duplicate request.
def test_duplicate_approve_completes_an_interrupted_verification(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    st = to_pending_approval(h)
    real_verify = executor.verify
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT", {}, Exception("connection lost during verification"))
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(executor, "verify", flaky)
    first = executor.execute(h.engine, h.clock, st["session_id"], st["approval"]["action_id"], fence=None,
                             owner_approval=(st["approval"]["approval_id"], st["approval"]["action_hash"]))
    assert first.receipt_verdict is None and first.status == "succeeded"
    again = executor.execute(h.engine, h.clock, st["session_id"], st["approval"]["action_id"], fence=None)
    assert again.duplicate and again.receipt_verdict == "verified"
    assert h.run_view(st["run_id"])["state"] == "completed"
    assert len(reservations(h.engine, st["session_id"])) == 1


def test_mismatching_receipt_blocks_the_run_visibly(h: Harness) -> None:
    st = to_pending_approval(h)
    approve(h, st["approval"])
    with h.engine.connect() as c:
        op = one(c, "SELECT operation_id FROM operations WHERE action_id = :a", a=st["approval"]["action_id"])
    assert op is not None
    with tx(h.engine) as c:
        exe(c, "UPDATE runs SET state = 'running' WHERE run_id = :r", r=st["run_id"])
        exe(c, "UPDATE marketplace_reservations SET price_minor = price_minor + 1 WHERE source_operation_id = :o", o=op["operation_id"])
    assert executor.verify(h.engine, h.clock, st["session_id"], op["operation_id"])["verdict"] == "mismatch"
    v = h.run_view(st["run_id"])
    assert v["state"] == "blocked" and v["state_reason"] == "receipt_mismatch"


# Finding 11: a stale worker's denial does not overwrite the live run's feedback.
def test_stale_worker_does_not_write_feedback(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "Is it available?")
    old = runtime.claim(h.engine, h.clock, "old", run_id)
    assert old is not None
    with tx(h.engine) as c:
        exe(c, "UPDATE runs SET lease_until = :p, last_feedback = CAST(:f AS jsonb) WHERE run_id = :r",
            p=PAST, f='{"code": "PRICE_OUT_OF_BOUNDS", "safe_message": "x"}', r=run_id)
    assert runtime.claim(h.engine, h.clock, "new", run_id) is not None
    bad = {"kind": "send_message", "message_intent": "counter_offer", "fact_ids": [], "price_minor": 1, "currency": "USD",
           "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None, "language": "en", "buyer_risk_signals": [],
           "rationale": "stale"}
    aid = executor.persist_proposal(h.engine, h.clock, old["session_id"], run_id, 1, 1, bad, "injected_test")
    assert executor.execute(h.engine, h.clock, old["session_id"], aid, fence=int(old["fence"])).reason_code == "FENCE_STALE"
    assert _run(h, run_id)["last_feedback"]["code"] == "PRICE_OUT_OF_BOUNDS"


# Finding 12/13: malformed headers and oversized bodies are client errors, not 500s.
def test_malformed_headers_and_bounds_are_4xx(h: Harness) -> None:
    h.new_session()
    m = h.current()["mandate"]
    r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"min_price_minor": 52000}, headers={**h.h(), "If-Match": "²".encode("latin-1")})  # type: ignore[dict-item]
    assert r.status_code == 428
    assert h.client.get("/v1/events?after=99999999999999999999", headers=h.h()).status_code == 422
    big = h.client.post("/v1/mandate-drafts", content=b"x" * 10, headers=h.h(**{"Content-Length": str(6 * 1024 * 1024),
                                                                                 "Content-Type": "application/json"}))
    assert big.status_code == 413


# Finding 14: the public asking price cannot drift from the listing after activation.
def test_asking_price_is_fixed_after_activation(h: Harness) -> None:
    h.new_session()
    m = h.current()["mandate"]
    r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"asking_price_minor": 62000}, headers=h.h(**{"If-Match": "1"}))
    assert r.status_code == 200
    h.client.post(f"/v1/mandates/{m['mandate_id']}/activate", json={"revision": 2}, headers=h.h())
    assert h.current()["listing"]["asking_price_minor"] == 62000  # synced on first activation
    r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"asking_price_minor": 60000}, headers=h.h(**{"If-Match": "2"}))
    assert r.status_code == 422 and r.json()["code"] == "ASKING_PRICE_FIXED"
    with h.engine.connect() as c:
        assert rows(c, "SELECT 1 FROM listings WHERE asking_price_minor = 62000")


def test_unknown_api_route_returns_the_error_envelope(h: Harness) -> None:
    r = h.client.get("/v1/no-such-route")
    assert r.status_code == 404 and set(r.json()) == {"code", "message", "retryable", "request_id"} and r.json()["code"] == "NOT_FOUND"
