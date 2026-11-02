"""Regression tests for the second (adversarial) review, 3 Nov 2026. One or more tests per finding."""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from delegate import app as app_module
from delegate import runtime
from delegate.clock import Clock
from delegate.db import one, tx
from delegate.meta_adapter import MetaAdapter
from delegate.projections import public_context
from delegate.settings import Settings
from tests.integration.helpers import Harness
from tests.integration.test_privacy_and_provider import FakeMeta, _wav, ok, proposal


@pytest.fixture()
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-placeholder-not-a-key")


def live(engine: Engine, clock: Clock, fake_transport: httpx.BaseTransport, **over: Any) -> Harness:
    settings = Settings(provider_mode="live", run_worker=False, web_dist="__none__", **over)
    return Harness(engine, clock, settings, MetaAdapter(settings, transport=fake_transport))


def daily(engine: Engine) -> int:
    with engine.connect() as c:
        r = one(c, "SELECT COALESCE(sum(live_calls), 0) AS n FROM daily_usage")
    return int(r["n"]) if r else 0


# Finding 1: deleting a session must not reset the global daily cap.
def test_deleting_a_session_keeps_the_daily_cap(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: ok(json.dumps(proposal())))
    h = live(engine, real_clock, fake.transport(), max_live_calls_per_day=2)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        for q in ("How much memory?", "Battery?"):
            h.say(run_id, q)
            h.step(run_id)
        assert daily(engine) == 2
        assert h.client.delete("/v1/demo-sessions/current", headers=h.h()).status_code == 204
        assert daily(engine) == 2
        with engine.connect() as c:
            assert one(c, "SELECT count(*) AS n FROM provider_calls WHERE mode = 'live'")["n"] == 2  # type: ignore[index]
        h.new_session()
        h.activate()
        run2 = h.start_run()
        h.say(run2, "Still available?")
        assert h.step(run2) == "global_budget_exhausted"
        assert len(fake.requests) == 2  # no third provider request after the delete
    finally:
        h.close()


# Finding 2: failed draft calls are recorded and counted.
def test_failed_voice_draft_calls_are_counted(engine: Engine, real_clock: Clock, live_env: None) -> None:
    calls = {"n": 0}

    def handle(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "could not decode audio: internal decoder /srv/asr"}})

    h = live(engine, real_clock, httpx.MockTransport(handle))
    try:
        h.new_session()
        r = h.client.post("/v1/mandate-drafts/voice", files={"audio": ("n.wav", _wav(), "audio/wav")}, headers=h.h())
        assert r.status_code == 503
        assert "/srv/asr" not in r.text and "decoder" not in r.text  # Finding 10: provider text stays server-side
        with engine.connect() as c:
            sess = one(c, "SELECT provider_calls FROM demo_sessions")
            rows_ = one(c, "SELECT count(*) AS n FROM provider_calls")
        assert calls["n"] == 2 and sess is not None and sess["provider_calls"] == 2 and rows_ is not None and rows_["n"] >= 2
    finally:
        h.close()


# Finding 3: a burst of concurrent drafts cannot exceed the per-session cap.
def test_concurrent_drafts_respect_the_session_cap(engine: Engine, real_clock: Clock, live_env: None) -> None:
    sent = {"n": 0}
    lock = threading.Lock()

    def handle(req: httpx.Request) -> httpx.Response:
        with lock:
            sent["n"] += 1
        return ok(json.dumps({"asking_price_minor": 65000, "currency": "USD", "pickup_only": True, "hold_minutes": None,
                              "share_fact_topics": [], "ambiguities": [], "language_detected": "en", "transcript": "x"}))

    h = live(engine, real_clock, httpx.MockTransport(handle), max_provider_calls_per_session=3)
    try:
        h.new_session()
        codes: list[int] = []
        threads = [threading.Thread(target=lambda: codes.append(
            h.client.post("/v1/mandate-drafts", json={"goal": "sell for 650"}, headers=h.h()).status_code)) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sent["n"] == 3 and codes.count(200) == 3 and codes.count(429) == 9
    finally:
        h.close()


# Finding 4: the preview tool cannot be used to probe the private minimum.
def test_preview_ignores_the_private_minimum(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "hi")
        run = runtime.claim(engine, real_clock, "w", run_id)
        assert run is not None
        prev = runtime.preview_fn(engine, real_clock, run, int(run["fence"]))
        counter = lambda price: proposal(message_intent="counter_offer", fact_ids=[], price_minor=price, currency="USD")  # noqa: E731
        below, above_floor = prev(counter(40000)), prev(counter(55000))
        assert below == above_floor and below["verdict"] == "allow"
        assert prev(counter(70000))["reason_code"] == "PRICE_OUT_OF_BOUNDS"  # the public asking price still bounds it
    finally:
        h.close()


# Finding 6/13: control characters are rejected; invisible formatting characters are stripped; proposals with them are invalid.
def test_control_and_format_characters(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        r = h.say(run_id, "hi\u0000there")
        assert r.status_code == 422 and r.json()["code"] == "INVALID_TEXT"
        assert h.say(run_id, "price‮05$ ok​?").status_code == 202
        text = [m for m in h.run_view(run_id)["conversation"] if m["role"] == "buyer"][-1]["text"]
        assert "‮" not in text and "​" not in text
    finally:
        h.close()
    from delegate.proposal import ProposalInvalid, parse_proposal

    with pytest.raises(ProposalInvalid):
        parse_proposal(proposal(rationale="bad\u0000"))
    with pytest.raises(ProposalInvalid):
        parse_proposal(proposal(rationale="lone \ud800 surrogate"))


# Finding 8: acceptances of superseded offers are not shown to the model.
def test_superseded_acceptances_are_hidden_from_the_model(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        list_offer = h.run_view(run_id)["open_offer"]
        h.client.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": list_offer["offer_id"]}, headers=h.h())
        with tx(engine) as c:  # a newer counter supersedes the accepted listing offer
            c.exec_driver_sql("UPDATE marketplace_offers SET status = 'superseded'")
        with engine.connect() as c:
            ctx = public_context(c, h.current()["session_id"], run_id)
        assert ctx["acceptances"] == []
    finally:
        h.close()


# Finding 9: an unrelated 400 does not switch tools off for everyone.
def test_unrelated_bad_request_does_not_disable_tools(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: httpx.Response(400, json={"error": {"message": "content rejected"}}) if n == 1 else ok(json.dumps(proposal())))
    h = live(engine, real_clock, fake.transport())
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "hello")
        h.step(run_id)
        h.client.post(f"/v1/runs/{run_id}/resume", headers=h.h())
        h.step(run_id)
        assert "tools" in fake.requests[-1]["body"]
    finally:
        h.close()


# Finding 11: event streams are capped per session.
def test_event_streams_are_capped_per_session(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        sid = h.current()["session_id"]
        app_module._streams[sid] = app_module.MAX_STREAMS_PER_SESSION
        r = h.client.get(f"/v1/runs/{run_id}/events", headers=h.h())
        assert r.status_code == 429 and r.json()["code"] == "TOO_MANY_STREAMS"
    finally:
        app_module._streams.clear()
        h.close()
