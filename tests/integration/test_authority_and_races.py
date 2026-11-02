"""Authority, ordering and duplication properties (families D17–D32) against real PostgreSQL.

Races use explicit barriers in the executor (before_locks / after_locks / before_commit / after_commit), not sleeps.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from delegate import executor, runtime
from delegate.clock import Clock
from delegate.db import exe, one, rows, tx
from tests.integration.helpers import Harness


@pytest.fixture()
def h(engine: Engine, real_clock: Clock) -> Iterator[Harness]:
    harness = Harness(engine, real_clock)
    yield harness
    harness.close()
    for name in ("before_locks", "after_locks", "before_commit", "after_commit"):
        executor.set_hook(name, None)


def to_pending_approval(h: Harness, buyer: str = "buyer-riya", new_session: bool = True) -> dict[str, Any]:
    if new_session:
        h.new_session()
        h.activate()
    run_id = h.start_run(buyer)
    h.say(run_id, "I can pay $600 and pick it up today.")
    h.step(run_id)
    offer = h.run_view(run_id)["open_offer"]
    r = h.client.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": offer["offer_id"]}, headers=h.h())
    assert r.status_code == 202, r.text
    assert h.step(run_id) == "waiting_approval"
    ap = h.run_view(run_id)["pending_approval"]
    assert ap is not None
    return {"run_id": run_id, "approval": ap, "session_id": h.current()["session_id"]}


def approve(h: Harness, ap: dict[str, Any], token: str | None = None) -> Any:
    return h.client.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": "approve", "action_hash": ap["action_hash"]},
                         headers=h.h(token))


def reservations(engine: Engine, session_id: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return rows(c, "SELECT * FROM marketplace_reservations WHERE session_id = :s", s=session_id)


# ------------------------------------------------------------------ D21–D24 revocation ordering


def test_revoke_commits_first_then_late_approval_cannot_reserve(h: Harness) -> None:
    st = to_pending_approval(h)
    at_barrier, release = threading.Event(), threading.Event()

    def barrier(_n: str, _i: dict[str, Any]) -> None:
        at_barrier.set()
        assert release.wait(10)

    executor.set_hook("before_locks", barrier)
    result: dict[str, Any] = {}
    t = threading.Thread(target=lambda: result.setdefault("r", approve(h, st["approval"])))
    t.start()
    assert at_barrier.wait(10)
    m = h.current()["mandate"]
    rv = h.client.post(f"/v1/mandates/{m['mandate_id']}/revoke", headers=h.h())
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    release.set()
    t.join(10)
    assert result["r"].status_code == 409  # approval was cancelled by the revocation
    assert reservations(h.engine, st["session_id"]) == []
    assert h.run_view(st["run_id"])["state"] == "cancelled"


def test_mutation_commits_first_then_revocation_stops_future_effects(h: Harness) -> None:
    st = to_pending_approval(h)
    locked, release = threading.Event(), threading.Event()

    def barrier(_n: str, _i: dict[str, Any]) -> None:
        locked.set()
        assert release.wait(10)

    executor.set_hook("after_locks", barrier)
    result: dict[str, Any] = {}
    t = threading.Thread(target=lambda: result.setdefault("r", approve(h, st["approval"])))
    t.start()
    assert locked.wait(10)
    m = h.current()["mandate"]
    revoke_result: dict[str, Any] = {}
    t2 = threading.Thread(target=lambda: revoke_result.setdefault(
        "r", h.client.post(f"/v1/mandates/{m['mandate_id']}/revoke", headers=h.h())))
    t2.start()
    t2.join(0.5)
    assert t2.is_alive(), "revocation must wait for the mandate lock held by the in-flight mutation"
    executor.set_hook("after_locks", None)
    release.set()
    t.join(10)
    t2.join(10)
    assert result["r"].status_code == 200 and result["r"].json()["receipt_verdict"] == "verified"
    res = reservations(h.engine, st["session_id"])
    assert len(res) == 1
    revoked_at = dt.datetime.fromisoformat(revoke_result["r"].json()["revoked_at"])
    assert revoked_at > res[0]["created_at"], "the effect is ordered before the revocation"
    # Further buyer turns are refused after revocation.
    assert h.say(st["run_id"], "one more question").status_code == 409


def test_late_model_output_after_revocation_is_stale(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "Is it still available?")
    run = runtime.claim(h.engine, h.clock, "w-late", run_id)
    assert run is not None
    # The model is "thinking"; the owner revokes meanwhile.
    m = h.current()["mandate"]
    assert h.client.post(f"/v1/mandates/{m['mandate_id']}/revoke", headers=h.h()).status_code == 200
    proposal = {"kind": "send_message", "message_intent": "answer_listing", "fact_ids": ["f_availability"], "price_minor": None,
                "currency": None, "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None, "language": "en",
                "buyer_risk_signals": [], "rationale": "late"}
    aid = executor.persist_proposal(h.engine, h.clock, run["session_id"], run_id, 1, 1, proposal, "injected_test")
    res = executor.execute(h.engine, h.clock, run["session_id"], aid, fence=int(run["fence"]))
    assert res.verdict == "deny" and res.reason_code in ("RUN_NOT_LIVE", "MANDATE_REVOKED")
    seller = [c for c in h.run_view(run_id)["conversation"] if c["role"] == "seller"]
    assert seller == []


def test_pause_blocks_and_resume_restores(h: Harness) -> None:
    h.new_session()
    mid = h.activate()
    run_id = h.start_run()
    assert h.client.post(f"/v1/mandates/{mid}/pause", headers=h.h()).status_code == 200
    assert h.say(run_id, "How much memory?").status_code == 202  # buyers can still write while the agent is paused
    assert h.step(run_id) in ("stale", "run_not_live")
    assert [c for c in h.run_view(run_id)["conversation"] if c["role"] == "seller"] == []
    assert h.client.post(f"/v1/mandates/{mid}/resume", headers=h.h()).status_code == 200
    h.say(run_id, "How much memory?")
    assert h.step(run_id) == "succeeded"


# ------------------------------------------------------------------ D17–D20 stale approvals


def test_new_revision_invalidates_pending_approval(h: Harness) -> None:
    st = to_pending_approval(h)
    m = h.current()["mandate"]
    r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"min_price_minor": 52000}, headers=h.h(**{"If-Match": str(m["current_revision"])}))
    assert r.status_code == 200
    # Editing alone does not change authority: the approval is still for the active revision.
    assert h.client.post(f"/v1/mandates/{m['mandate_id']}/activate", json={"revision": r.json()["revision"]}, headers=h.h()).status_code == 200
    assert approve(h, st["approval"]).status_code == 409
    assert reservations(h.engine, st["session_id"]) == []


def test_stale_if_match_is_rejected(h: Harness) -> None:
    h.new_session()
    m = h.current()["mandate"]
    r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"min_price_minor": 52000}, headers=h.h(**{"If-Match": "99"}))
    assert r.status_code == 409 and r.json()["code"] == "REVISION_CONFLICT"


def test_changed_hash_cannot_approve(h: Harness) -> None:
    st = to_pending_approval(h)
    forged = {**st["approval"], "action_hash": "0" * 64}
    r = approve(h, forged)
    assert r.status_code == 409 and r.json()["code"] == "APPROVAL_MISMATCH"
    assert reservations(h.engine, st["session_id"]) == []


def test_expired_approval_cannot_execute(h: Harness) -> None:
    st = to_pending_approval(h)
    with tx(h.engine) as c:
        exe(c, "UPDATE approvals SET expires_at = :e WHERE approval_id = :a", e=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            a=st["approval"]["approval_id"])
    r = approve(h, st["approval"])
    assert r.status_code == 410
    assert reservations(h.engine, st["session_id"]) == []


def test_reject_then_approve_is_refused(h: Harness) -> None:
    st = to_pending_approval(h)
    r = h.client.post(f"/v1/approvals/{st['approval']['approval_id']}/resolve",
                      json={"decision": "reject", "action_hash": st["approval"]["action_hash"]}, headers=h.h())
    assert r.status_code == 200
    assert approve(h, st["approval"]).status_code == 409
    assert reservations(h.engine, st["session_id"]) == []
    assert h.run_view(st["run_id"])["state"] == "awaiting_buyer"


# ------------------------------------------------------------------ D25–D28 fence, duplicates, competition, crash


def test_stale_worker_fence_cannot_commit(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "Is it still available?")
    old = runtime.claim(h.engine, h.clock, "w-old", run_id)
    assert old is not None
    with tx(h.engine) as c:
        exe(c, "UPDATE runs SET lease_until = :p WHERE run_id = :r", p=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), r=run_id)
    new = runtime.claim(h.engine, h.clock, "w-new", run_id)
    assert new is not None and new["fence"] > old["fence"]
    proposal = {"kind": "send_message", "message_intent": "answer_listing", "fact_ids": ["f_availability"], "price_minor": None,
                "currency": None, "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None, "language": "en",
                "buyer_risk_signals": [], "rationale": "stale worker"}
    aid = executor.persist_proposal(h.engine, h.clock, old["session_id"], run_id, 1, 1, proposal, "injected_test")
    res = executor.execute(h.engine, h.clock, old["session_id"], aid, fence=int(old["fence"]))
    assert res.verdict == "deny" and res.reason_code == "FENCE_STALE"
    assert runtime.process(h.engine, h.clock, h.settings, h.adapter, new) == "succeeded"
    seller = [c for c in h.run_view(run_id)["conversation"] if c["role"] == "seller"]
    assert len(seller) == 1


def test_duplicate_buyer_turn_with_same_idempotency_key(h: Harness) -> None:
    h.new_session()
    h.activate()
    run_id = h.start_run()
    a = h.say(run_id, "Is it available?", key="k-1")
    b = h.say(run_id, "Is it available?", key="k-1")
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert b.headers.get("Idempotent-Replay") == "true"
    buyer = [c for c in h.run_view(run_id)["conversation"] if c["role"] == "buyer"]
    assert len(buyer) == 1


def test_concurrent_double_approval_reserves_once(h: Harness) -> None:
    st = to_pending_approval(h)
    out: list[Any] = []
    threads = [threading.Thread(target=lambda: out.append(approve(h, st["approval"]))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    codes = sorted(r.status_code for r in out)
    assert codes.count(200) >= 1
    assert len(reservations(h.engine, st["session_id"])) == 1


def test_executing_the_same_action_twice_has_one_effect(h: Harness) -> None:
    st = to_pending_approval(h)
    r = approve(h, st["approval"])
    assert r.status_code == 200
    again = executor.execute(h.engine, h.clock, st["session_id"], st["approval"]["action_id"], fence=None)
    assert again.duplicate and again.reason_code == "DUPLICATE"
    assert len(reservations(h.engine, st["session_id"])) == 1


def test_two_buyers_compete_for_one_item(h: Harness) -> None:
    a = to_pending_approval(h, "buyer-riya")
    b = to_pending_approval(h, "buyer-arjun", new_session=False)
    assert approve(h, a["approval"]).json()["receipt_verdict"] == "verified"
    rb = approve(h, b["approval"])
    assert rb.status_code == 200 and rb.json()["verdict"] == "deny" and rb.json()["reason_code"] == "ITEM_UNAVAILABLE"
    assert len(reservations(h.engine, a["session_id"])) == 1


def test_crash_after_commit_is_reconciled_without_a_second_effect(h: Harness) -> None:
    st = to_pending_approval(h)

    def lost_ack(_n: str, _i: dict[str, Any]) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost after commit"))

    executor.set_hook("after_commit", lost_ack)
    r = approve(h, st["approval"])
    executor.set_hook("after_commit", None)
    assert r.status_code == 200
    body = r.json()
    assert body["duplicate"] is True and body["receipt_verdict"] == "verified"
    assert len(reservations(h.engine, st["session_id"])) == 1
    with h.engine.connect() as c:
        ev = one(c, "SELECT count(*) AS n FROM audit_events WHERE session_id = :s AND event_type = 'operation.reconciled'", s=st["session_id"])
    assert ev is not None and ev["n"] == 1


def test_crash_before_commit_leaves_no_effect(h: Harness) -> None:
    st = to_pending_approval(h)

    def die(_n: str, _i: dict[str, Any]) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost before commit"))

    executor.set_hook("before_commit", die)
    r = approve(h, st["approval"])
    executor.set_hook("before_commit", None)
    assert r.status_code == 200 and r.json()["reason_code"] == "NOT_EXECUTED"
    assert reservations(h.engine, st["session_id"]) == []


# ------------------------------------------------------------------ D29–D32 receipts


def test_tampered_reservation_is_a_mismatch_and_missing_is_unknown(h: Harness) -> None:
    st = to_pending_approval(h)
    assert approve(h, st["approval"]).json()["receipt_verdict"] == "verified"
    with h.engine.connect() as c:
        op = one(c, "SELECT operation_id FROM operations WHERE action_id = :a", a=st["approval"]["action_id"])
    assert op is not None
    with tx(h.engine) as c:
        exe(c, "UPDATE marketplace_reservations SET price_minor = price_minor - 100 WHERE source_operation_id = :o", o=op["operation_id"])
    assert executor.verify(h.engine, h.clock, st["session_id"], op["operation_id"])["verdict"] == "mismatch"
    with tx(h.engine) as c:
        exe(c, "UPDATE marketplace_reservations SET buyer_id = 'buyer-arjun', price_minor = price_minor + 100 WHERE source_operation_id = :o", o=op["operation_id"])
    v = executor.verify(h.engine, h.clock, st["session_id"], op["operation_id"])
    assert v["verdict"] == "mismatch" and "buyer_id" in v["mismatches"]
    with tx(h.engine) as c:
        exe(c, "DELETE FROM marketplace_reservations WHERE source_operation_id = :o", o=op["operation_id"])
    assert executor.verify(h.engine, h.clock, st["session_id"], op["operation_id"])["verdict"] == "unknown"


def test_compare_reservation_pure_cases() -> None:
    now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
    exp = {f: f"v-{f}" for f in executor.VERIFIED_FIELDS}
    good = {**exp, "expires_at": now + dt.timedelta(minutes=5)}
    assert executor.compare_reservation(exp, good, now) == ("verified", [])
    assert executor.compare_reservation(exp, None, now)[0] == "unknown"
    assert executor.compare_reservation(exp, {**good, "price_minor": "x"}, now) == ("mismatch", ["price_minor"])
    assert executor.compare_reservation(exp, {**good, "status": None}, now)[0] == "unknown"
    assert executor.compare_reservation(exp, {**good, "expires_at": now}, now) == ("mismatch", ["expired"])
    forged = {**good, "operation_id": "op_forged", "action_hash": "f" * 64}
    assert executor.compare_reservation(exp, forged, now) == ("mismatch", ["operation_id", "action_hash"])


# ------------------------------------------------------------------ D33 isolation, D39 reset


def test_cross_session_access_is_404(h: Harness) -> None:
    st = to_pending_approval(h)
    token_a = h.token
    h.new_session()  # session B
    token_b = h.token
    for path in (f"/v1/runs/{st['run_id']}", f"/v1/runs/{st['run_id']}/buyer-view", f"/v1/runs/{st['run_id']}/evidence"):
        assert h.client.get(path, headers=h.h(token_b)).status_code == 404
    assert h.client.post(f"/v1/runs/{st['run_id']}/buyer-turns", json={"text": "hi"}, headers=h.h(token_b)).status_code == 404
    assert approve(h, st["approval"], token_b).status_code == 404
    mid_a = h.current(token_a)["mandate"]["mandate_id"]
    assert h.client.post(f"/v1/mandates/{mid_a}/revoke", headers=h.h(token_b)).status_code == 404
    assert h.current(token_a)["mandate"]["status"] == "active"


def test_reset_deletes_session_and_token(h: Harness) -> None:
    h.new_session()
    sid = h.current()["session_id"]
    assert h.client.delete("/v1/demo-sessions/current", headers=h.h()).status_code == 204
    assert h.client.get("/v1/demo-sessions/current", headers=h.h()).status_code == 401
    with h.engine.connect() as c:
        assert one(c, "SELECT 1 AS x FROM demo_sessions WHERE session_id = :s", s=sid) is None
        assert one(c, "SELECT 1 AS x FROM audit_events WHERE session_id = :s", s=sid) is None


def test_ttl_cleanup_removes_expired_sessions(h: Harness) -> None:
    h.new_session()
    sid = h.current()["session_id"]
    with tx(h.engine) as c:
        exe(c, "UPDATE demo_sessions SET expires_at = :e WHERE session_id = :s", e=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), s=sid)
    assert h.client.get("/v1/demo-sessions/current", headers=h.h()).status_code == 401
    from delegate import store

    assert store.cleanup_expired(h.engine, h.clock) >= 1
    with h.engine.connect() as c:
        assert one(c, "SELECT 1 AS x FROM demo_sessions WHERE session_id = :s", s=sid) is None


def test_session_creation_is_rate_limited(engine: Engine, real_clock: Clock) -> None:
    from tests.integration.helpers import mock_settings

    hh = Harness(engine, real_clock, mock_settings(sessions_per_ip_per_hour=2))
    try:
        assert hh.client.post("/v1/demo-sessions").status_code == 201
        assert hh.client.post("/v1/demo-sessions").status_code == 201
        r = hh.client.post("/v1/demo-sessions")
        assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    finally:
        hh.close()


def test_buyer_message_while_approval_pending_keeps_or_cancels_honestly(h: Harness) -> None:
    st = to_pending_approval(h)
    h.say(st["run_id"], "Great, how much memory does it have again?")
    assert h.step(st["run_id"]) == "succeeded"
    v = h.run_view(st["run_id"])
    assert v["state"] == "waiting_approval" and v["pending_approval"] is not None
    assert approve(h, v["pending_approval"]).json()["receipt_verdict"] == "verified"
