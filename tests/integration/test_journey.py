from __future__ import annotations

from sqlalchemy import Engine

from delegate.clock import Clock
from tests.integration.helpers import Harness


def test_full_journey_mock_mode(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        assert h.say(run_id, "Is it still available? How much RAM?").status_code == 202
        assert h.step(run_id) == "succeeded"
        v = h.run_view(run_id)
        seller = [m for m in v["conversation"] if m["role"] == "seller"]
        assert "16 GB memory" in seller[-1]["text"]

        h.say(run_id, "Would you take $560 if I pick it up tonight?")
        h.step(run_id)
        v = h.run_view(run_id)
        offer = v["open_offer"]
        assert offer["source"] == "counter_offer" and 50000 <= offer["price_minor"] <= 65000

        r = h.client.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": offer["offer_id"]}, headers=h.h())
        assert r.status_code == 202, r.text
        assert h.step(run_id) == "waiting_approval"
        v = h.run_view(run_id)
        ap = v["pending_approval"]
        assert ap and ap["price_minor"] == offer["price_minor"] and v["state"] == "waiting_approval"

        r = h.client.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": "approve", "action_hash": ap["action_hash"]}, headers=h.h())
        assert r.status_code == 200, r.text
        assert r.json()["receipt_verdict"] == "verified"
        v = h.run_view(run_id)
        assert v["state"] == "completed" and v["receipt"]["verdict"] == "verified"
        assert v["reservation"]["status"] == "active"

        # Replaying the approval is single-use.
        r = h.client.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": "approve", "action_hash": ap["action_hash"]}, headers=h.h())
        assert r.status_code in (200, 409)
        if r.status_code == 200:
            assert r.json()["duplicate"] is True
    finally:
        h.close()
