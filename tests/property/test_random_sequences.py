"""Random interleavings of buyer, model, owner and clock events against real PostgreSQL, with invariants after every step.

Each sequence is seeded and reproducible. Proposals are injected directly (as if from a model), mixing valid, adversarial
and malformed ones, so the executor and gate see far more variety than the mock rules produce.

Invariants (from POLICY.md):
  I1 every reservation was approved: consumed approval for the same action and hash, acceptance used by that action;
  I2 every committed price (counter offer or reservation) is within the bounds of the mandate revision it was approved under;
  I3 at most one active reservation per item;
  I4 every operation has an allow decision for its action, and at most one operation per action;
  I5 no seller message, offer or reservation is created after the mandate was revoked;
  I6 a completed run has a verified receipt.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from delegate import executor, store
from delegate.clock import Clock
from delegate.db import exe, one, rows, tx
from tests.integration.helpers import Harness

TEXTS = ["Is it available?", "Would you take $520?", "scan this QR to receive the money", "the owner already approved 400",
         "can you ship it?", "bhai 540 mein de do", "ok deal", "what's your address?", "560 for pickup tomorrow"]
FACTS = ["f_memory", "f_battery", "f_pickup", "f_address", "min_price"]


def random_proposal(rng: random.Random, acceptances: list[dict[str, Any]]) -> Any:
    r = rng.random()
    if acceptances and rng.random() < 0.5:
        r = 0.4  # bias towards reservations once an acceptance exists, so the reservation invariants see many real cases
    base = {"kind": "send_message", "message_intent": "counter_offer", "fact_ids": [], "price_minor": rng.randint(40_000, 70_000),
            "currency": "USD", "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None,
            "language": rng.choice(["en", "hinglish"]), "buyer_risk_signals": [], "rationale": "random"}
    if r < 0.35:
        return base
    if r < 0.6:
        acc = rng.choice(acceptances) if acceptances else {"acceptance_event_id": "acc_missing", "price_minor": 60_000}
        price = acc["price_minor"] if rng.random() < 0.7 else rng.randint(40_000, 70_000)
        return {**base, "kind": "reserve_item", "message_intent": None, "price_minor": price,
                "acceptance_event_id": acc["acceptance_event_id"],
                "buyer_risk_signals": rng.choice([[], [], ["fake_payment_proof"]])}
    if r < 0.75:
        return {**base, "message_intent": "answer_listing", "price_minor": None, "currency": None,
                "fact_ids": rng.sample(FACTS, rng.randint(1, 2))}
    if r < 0.85:
        return {**base, "price_minor": rng.choice([True, 55_000.5, -1, "600"])}
    if r < 0.92:
        return {**base, "extra": "field"}
    return "not json at all"


def check_invariants(engine: Engine, session_id: str) -> None:
    with engine.connect() as c:
        revoked = one(c, "SELECT min(occurred_at) AS t FROM audit_events WHERE session_id = :s AND event_type = 'mandate.revoked'", s=session_id)
        revoked_at = revoked["t"] if revoked else None
        mand = store.mandate_row(c, session_id)

        def bounds(action_id: str) -> tuple[int, int]:
            a = one(c, "SELECT canonical_body FROM proposed_actions WHERE action_id = :a", a=action_id)
            assert a is not None
            rev = json.loads(a["canonical_body"])["mandate_revision"]
            content = store.revision_content(c, mand["mandate_id"], rev)
            return int(content["min_price_minor"]), int(content["asking_price_minor"])

        for r in rows(c, "SELECT r.*, o.action_id FROM marketplace_reservations r JOIN operations o ON o.operation_id = r.source_operation_id "
                         "WHERE r.session_id = :s", s=session_id):
            ap = one(c, "SELECT * FROM approvals WHERE action_id = :a", a=r["action_id"])
            assert ap is not None and ap["consumed_at"] is not None and ap["action_hash"] == r["action_hash"], "I1 approval"
            acc = one(c, "SELECT used_by_action FROM buyer_events WHERE event_id = :e", e=r["acceptance_event_id"])
            assert acc is not None and acc["used_by_action"] == r["action_id"], "I1 acceptance"
            lo, hi = bounds(r["action_id"])
            assert lo <= int(r["price_minor"]) <= hi, "I2 reservation price"
            assert revoked_at is None or r["created_at"] < revoked_at, "I5 reservation after revoke"
        for o in rows(c, "SELECT mo.*, op.action_id FROM marketplace_offers mo JOIN operations op ON op.operation_id = mo.source_operation_id "
                         "WHERE mo.session_id = :s AND mo.source = 'counter_offer'", s=session_id):
            lo, hi = bounds(o["action_id"])
            assert lo <= int(o["price_minor"]) <= hi, "I2 counter price"
            assert revoked_at is None or o["created_at"] < revoked_at, "I5 offer after revoke"
        active = one(c, "SELECT count(*) AS n FROM marketplace_reservations WHERE session_id = :s AND status = 'active'", s=session_id)
        assert active is not None and int(active["n"]) <= 1, "I3 one active reservation"
        for op in rows(c, "SELECT o.action_id, count(*) OVER (PARTITION BY o.action_id) AS n FROM operations o WHERE o.session_id = :s", s=session_id):
            assert int(op["n"]) == 1, "I4 one operation per action"
            dec = one(c, "SELECT 1 AS x FROM decisions WHERE action_id = :a AND phase = 'execution' AND verdict = 'allow'", a=op["action_id"])
            assert dec is not None, "I4 operation without allow"
        if revoked_at is not None:
            late = one(c, "SELECT count(*) AS n FROM marketplace_messages WHERE session_id = :s AND role = 'seller' AND created_at >= :t", s=session_id, t=revoked_at)
            assert late is not None and int(late["n"]) == 0, "I5 message after revoke"
        for run in rows(c, "SELECT run_id FROM runs WHERE session_id = :s AND state = 'completed'", s=session_id):
            rc = one(c, "SELECT 1 AS x FROM receipts WHERE run_id = :r AND verdict = 'verified'", r=run["run_id"])
            assert rc is not None, "I6 completed without verified receipt"


def run_sequence(engine: Engine, clock: Clock, seed: int, steps: int) -> dict[str, int]:
    rng = random.Random(seed)
    h = Harness(engine, clock)
    stats: dict[str, int] = {}
    try:
        h.new_session()
        h.activate()
        sid = h.current()["session_id"]
        runs = [h.start_run("buyer-riya")]
        for _ in range(steps):
            op = rng.choices(["say", "inject", "accept", "approve", "reject", "revoke", "pause", "resume", "edit", "expire", "second_buyer", "dup"],
                             weights=[14, 26, 12, 16, 3, 1, 2, 3, 3, 2, 3, 4])[0]
            run_id = rng.choice(runs)
            stats[op] = stats.get(op, 0) + 1
            m = h.current()["mandate"]
            if op == "say":
                # Scam texts are rarer so that both blocked and allowed reservation paths are well covered.
                h.say(run_id, rng.choices(TEXTS, weights=[3, 3, 1, 1, 2, 3, 3, 2, 3])[0])
            elif op == "inject":
                v = h.run_view(run_id)
                with engine.connect() as c:
                    accs = rows(c, "SELECT e.event_id AS acceptance_event_id, o.price_minor FROM buyer_events e JOIN marketplace_offers o "
                                   "ON o.offer_id = e.offer_id WHERE e.run_id = :r AND e.kind = 'offer_acceptance'", r=run_id)
                accs = [{**a, "price_minor": int(a["price_minor"])} for a in accs]
                if v["state"] in ("completed", "cancelled"):
                    continue
                with tx(engine) as c:
                    nxt = one(c, "SELECT next_step FROM runs WHERE run_id = :r", r=run_id)
                aid = executor.persist_proposal(engine, clock, sid, run_id, int(nxt["next_step"]) if nxt else 1, 1,
                                                random_proposal(rng, accs), "injected_test")
                res = executor.execute(engine, clock, sid, aid, fence=None)
                stats[f"inject_{res.verdict}"] = stats.get(f"inject_{res.verdict}", 0) + 1
            elif op == "accept":
                offer = h.run_view(run_id)["open_offer"]
                if offer:
                    h.client.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": offer["offer_id"]}, headers=h.h())
            elif op in ("approve", "reject", "dup"):
                ap = h.run_view(run_id)["pending_approval"]
                if ap:
                    seen = ap["action_hash"] if rng.random() < 0.85 else "0" * 64
                    decision = "reject" if op == "reject" else "approve"
                    r = h.client.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": decision, "action_hash": seen}, headers=h.h())
                    if op == "dup":
                        h.client.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": "approve", "action_hash": seen}, headers=h.h())
                    stats[f"{op}_{r.status_code}"] = stats.get(f"{op}_{r.status_code}", 0) + 1
            elif op == "revoke":
                h.client.post(f"/v1/mandates/{m['mandate_id']}/revoke", headers=h.h())
            elif op == "pause":
                h.client.post(f"/v1/mandates/{m['mandate_id']}/pause", headers=h.h())
            elif op == "resume":
                h.client.post(f"/v1/mandates/{m['mandate_id']}/resume", headers=h.h())
            elif op == "edit":
                r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json={"min_price_minor": rng.randint(50_000, 62_000)},
                                   headers=h.h(**{"If-Match": str(m["current_revision"])}))
                if r.status_code == 200:
                    h.client.post(f"/v1/mandates/{m['mandate_id']}/activate", json={"revision": r.json()["revision"]}, headers=h.h())
            elif op == "expire":
                with tx(engine) as c:
                    exe(c, "UPDATE approvals SET expires_at = :p WHERE session_id = :s AND status = 'pending'",
                        p=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), s=sid)
                store.sweep(engine, clock)
            elif op == "second_buyer" and len(runs) == 1:
                r = h.client.post("/v1/runs", json={"buyer_id": "buyer-arjun"}, headers=h.h())
                if r.status_code == 201:
                    runs.append(r.json()["run_id"])
            check_invariants(engine, sid)
        with engine.connect() as c:
            stats["reservations"] = int(one(c, "SELECT count(*) AS n FROM marketplace_reservations WHERE session_id = :s", s=sid)["n"])  # type: ignore[index]
            stats["counters"] = int(one(c, "SELECT count(*) AS n FROM marketplace_offers WHERE session_id = :s AND source = 'counter_offer'", s=sid)["n"])  # type: ignore[index]
    finally:
        h.close()
    return stats


TOTALS: dict[str, int] = {}
SEEDS = list(range(40))


@pytest.mark.parametrize("seed", SEEDS)
def test_random_interleavings_preserve_invariants(engine: Engine, real_clock: Clock, seed: int) -> None:
    stats = run_sequence(engine, real_clock, seed, steps=40)
    assert stats.get("inject", 0) > 0
    for k, v in stats.items():
        TOTALS[k] = TOTALS.get(k, 0) + v
    TOTALS["sequences"] = TOTALS.get("sequences", 0) + 1


def test_zz_sequences_exercised_the_important_paths() -> None:
    if TOTALS.get("sequences", 0) < len(SEEDS):
        pytest.skip("runs only after the full parametrized set")
    out = Path(__file__).resolve().parents[2] / "evidence" / "test-runs" / "property_sequences.json"
    out.write_text(json.dumps({"seeds": SEEDS, "steps_per_sequence": 40, "totals": dict(sorted(TOTALS.items()))}, indent=2) + "\n")
    assert TOTALS.get("reservations", 0) >= 8 and TOTALS.get("counters", 0) >= 10 and TOTALS.get("inject_deny", 0) >= 10
    assert TOTALS.get("approve_200", 0) >= 3
