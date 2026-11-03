"""Gate lab: deterministic replay of frozen cases and evaluation of edited proposals."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from delegate.clock import Clock
from tests.integration.helpers import Harness


@pytest.fixture()
def h(engine: Engine, real_clock: Clock) -> Iterator[Harness]:
    harness = Harness(engine, real_clock)
    yield harness
    harness.close()


def test_lab_lists_every_frozen_case_with_its_verdict(h: Harness) -> None:
    d = h.client.get("/v1/lab/cases").json()
    assert d["summary"]["total"] >= 151 and d["summary"]["passed"] == d["summary"]["total"]
    assert d["evidence_mode"] == "SYNTHETIC_FIXTURE" and "D01" in d["families"]


def test_lab_case_detail_has_a_check_trace(h: Harness) -> None:
    c = h.client.get("/v1/lab/cases/D01-01").json()
    names = [x["name"] for x in c["trace"]["checks"]]
    assert names[0] == "1_schema" and len(names) == 10
    assert all(x["status"] == "passed" for x in c["trace"]["checks"]) and c["trace"]["verdict"] == "allow"
    assert h.client.get("/v1/lab/cases/NOPE").status_code == 404


def test_lab_evaluates_an_edited_proposal_with_the_real_gate(h: Harness) -> None:
    base = h.client.get("/v1/lab/cases/D01-01").json()["proposal"]
    below = {**base, "message_intent": "counter_offer", "fact_ids": [], "price_minor": 40000, "currency": "USD"}
    r = h.client.post("/v1/lab/evaluate", json={"case_id": "D01-01", "proposal": below}).json()
    t = r["trace"]
    assert t["verdict"] == "deny" and t["reason_code"] == "PRICE_OUT_OF_BOUNDS" and t["decided_by"] == "6_money"
    statuses = {x["name"]: x["status"] for x in t["checks"]}
    assert statuses["5_allowed_and_budget"] == "passed" and statuses["6_money"] == "decided" and statuses["7_terms_disclosure_risk"] == "not_run"
    resp = h.client.post("/v1/lab/evaluate", json={"case_id": "D01-01", "proposal": {"kind": "send_money"}})
    assert resp.status_code == 200, resp.text
    bad = resp.json()["trace"]
    assert bad["verdict"] == "invalid" and bad["decided_by"] == "1_schema"
    assert h.client.post("/v1/lab/evaluate", json={"case_id": "D01-01", "proposal": base, "extra": 1}).status_code == 422


def test_lab_evaluate_is_rate_limited_per_network(h: Harness) -> None:
    from delegate.app import WindowLimiter

    lim = WindowLimiter(per_minute=3)
    assert [lim.allow("ip-a", t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert lim.allow("ip-b", 3)  # other networks are unaffected
    assert lim.allow("ip-a", 61)  # the window slides
    case = h.client.get("/v1/lab/cases").json()["cases"][0]["case_id"]
    detail = h.client.get(f"/v1/lab/cases/{case}").json()
    codes = [h.client.post("/v1/lab/evaluate", json={"case_id": case, "proposal": detail["proposal"]},
                           headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_responses_carry_hsts(h: Harness) -> None:
    r = h.client.get("/v1/health")
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
