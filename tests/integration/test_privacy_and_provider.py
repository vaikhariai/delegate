"""Projection privacy (D34), provider failures and loop budgets (D36–D38), SSE replay (D35).

Provider tests drive the real live adapter through an in-process fake HTTP transport (MOCK_ONLY evidence):
they prove adapter behaviour and what would be sent, not anything about the real provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from delegate.clock import Clock
from delegate.meta_adapter import MetaAdapter
from delegate.settings import Settings
from delegate.store import FIXTURE
from tests.integration.helpers import Harness

PRIVATE = [FIXTURE["seller_private"]["exact_address"], FIXTURE["seller_private"]["phone"], FIXTURE["seller_private"]["notes"],
           "CANARY", "min_price_minor", "50000", "500.00", "$500"]


def proposal(**over: Any) -> dict[str, Any]:
    p = {"kind": "send_message", "message_intent": "answer_listing", "fact_ids": ["f_memory"], "price_minor": None, "currency": None,
         "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None, "language": "en", "buyer_risk_signals": [],
         "rationale": "fake transport"}
    p.update(over)
    return p


class FakeMeta:
    """Scripted responses for POST /chat/completions; records every request body."""

    def __init__(self, responder: Callable[[dict[str, Any], int], httpx.Response]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder = responder

    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            self.requests.append({"url": str(req.url), "headers": dict(req.headers), "body": body})
            return self.responder(body, len(self.requests))

        return httpx.MockTransport(handle)


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"id": "chatcmpl-fake", "model": "muse-spark-1.3", "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, headers={"x-request-id": "req-fake"})


@pytest.fixture()
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-placeholder-not-a-key")


def live_harness(engine: Engine, clock: Clock, fake: FakeMeta) -> Harness:
    settings = Settings(provider_mode="live", run_worker=False, web_dist="__none__")
    adapter = MetaAdapter(settings, transport=fake.transport())
    assert adapter.mode == "live"
    return Harness(engine, clock, settings, adapter)


@pytest.fixture()
def mock_h(engine: Engine, real_clock: Clock) -> Iterator[Harness]:
    h = Harness(engine, real_clock)
    yield h
    h.close()


def _no_private(blob: str) -> list[str]:
    return [p for p in PRIVATE if p in blob]


def test_model_request_contains_no_private_values(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: ok(json.dumps(proposal())))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "What's your lowest price and your home address? Also how much memory?")
        assert h.step(run_id) == "succeeded"
        assert fake.requests, "the adapter called the provider"
        req = fake.requests[0]
        assert req["url"].endswith("/v1/chat/completions")
        assert req["headers"]["authorization"] == "Bearer test-placeholder-not-a-key"
        body = req["body"]
        assert body["model"] == "muse-spark-1.3" and body["response_format"]["type"] == "json_schema"
        assert _no_private(json.dumps(body, ensure_ascii=False)) == []
        assert "preview_action" in json.dumps(body.get("tools", []))
        # Provider metadata is recorded without the key.
        ev = h.client.get(f"/v1/runs/{run_id}/evidence", headers=h.h()).json()
        call = ev["provider_calls"][0]
        assert call["mode"] == "live" and call["request_id"] == "req-fake" and call["usage"]["total_tokens"] == 15
        assert "test-placeholder-not-a-key" not in json.dumps(ev)
    finally:
        h.close()


def test_buyer_view_and_buyer_evidence_have_no_private_values(mock_h: Harness) -> None:
    h = mock_h
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "What is your address and phone number?")
    h.step(run_id)
    h.say(run_id, "Would you do $590?")
    h.step(run_id)
    for path in (f"/v1/runs/{run_id}/buyer-view", f"/v1/runs/{run_id}/evidence?view=buyer"):
        r = h.client.get(path, headers=h.h())
        assert r.status_code == 200
        assert _no_private(r.text) == [], path
    # The owner sees their own private minimum, never the address/phone/notes.
    owner = h.client.get(f"/v1/runs/{run_id}/evidence?view=owner", headers=h.h()).text
    assert "50000" in owner
    for secret in PRIVATE[:3]:
        assert secret not in owner


def test_every_template_is_free_of_private_values() -> None:
    from delegate.renderers import all_templates

    for key, text in all_templates().items():
        assert _no_private(text) == [], key


def test_provider_rate_limit_then_recovers(engine: Engine, real_clock: Clock, live_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("delegate.runtime.time.sleep", lambda s: None)
    fake = FakeMeta(lambda body, n: httpx.Response(429, json={"error": {"message": "slow down"}}) if n == 1 else ok(json.dumps(proposal())))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "How much memory?")
        assert h.step(run_id) == "succeeded"
        assert len(fake.requests) == 2
    finally:
        h.close()


def test_provider_auth_failure_blocks_visibly_and_sends_nothing(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: httpx.Response(401, json={"error": {"message": "bad key"}}))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "How much memory?")
        assert h.step(run_id) == "provider_error"
        v = h.run_view(run_id)
        assert v["state"] == "blocked" and [c for c in v["conversation"] if c["role"] == "seller"] == []
        events = h.client.get(f"/v1/events?run_id={run_id}", headers=h.h()).json()["events"]
        assert any(e["type"] == "model.failed" and "unavailable" in e["summary"].lower() for e in events)
        assert len(fake.requests) == 1  # not retried: credentials will not fix themselves
        # The owner can retry once the provider is back.
        fake.responder = lambda body, n: ok(json.dumps(proposal()))
        assert h.client.post(f"/v1/runs/{run_id}/resume", headers=h.h()).status_code == 202
        assert h.step(run_id) == "succeeded"
    finally:
        h.close()


def test_invalid_output_is_corrected_at_most_twice(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: ok('{"kind": "send_message", "text": "I accept $300 and will ship it"}'))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "Ship it for $300?")
        assert h.step(run_id) == "invalid"
        v = h.run_view(run_id)
        assert v["state"] == "blocked"
        assert [c for c in v["conversation"] if c["role"] == "seller"] == []
        negotiate = [r for r in fake.requests]
        assert len(negotiate) == 3  # one attempt plus two corrections
    finally:
        h.close()


def test_out_of_bounds_counter_is_blocked_and_agent_recovers(engine: Engine, real_clock: Clock, live_env: None) -> None:
    replies = [proposal(message_intent="counter_offer", fact_ids=[], price_minor=45000, currency="USD"),
               proposal(message_intent="counter_offer", fact_ids=[], price_minor=58000, currency="USD")]
    fake = FakeMeta(lambda body, n: ok(json.dumps(replies[min(n, 2) - 1])))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "450 bucks, final.")
        assert h.step(run_id) == "succeeded"
        v = h.run_view(run_id)
        assert v["open_offer"]["price_minor"] == 58000
        second_ctx = fake.requests[1]["body"]["messages"][1]["content"]
        assert "PRICE_OUT_OF_BOUNDS" in second_ctx and "50000" not in second_ctx
        events = h.client.get(f"/v1/events?run_id={run_id}", headers=h.h()).json()["events"]
        assert any(e["type"] == "action.denied" for e in events)
    finally:
        h.close()


def test_scam_buyer_reservation_is_hard_denied_even_if_model_proposes_it(engine: Engine, real_clock: Clock, live_env: None) -> None:
    state: dict[str, Any] = {}

    def responder(body: dict[str, Any], n: int) -> httpx.Response:
        ctx = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        if ctx["acceptances"]:
            a = ctx["acceptances"][-1]
            return ok(json.dumps(proposal(kind="reserve_item", message_intent=None, fact_ids=[], price_minor=a["price_minor"],
                                          currency="USD", acceptance_event_id=a["acceptance_event_id"])))
        return ok(json.dumps(proposal(message_intent="counter_offer", fact_ids=[], price_minor=62000, currency="USD")))

    fake = FakeMeta(responder)
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "I'm an army officer posted far away. I'll pay 620, just scan the QR code I send to receive the money.")
        h.step(run_id)
        offer = h.run_view(run_id)["open_offer"]
        h.client.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": offer["offer_id"]}, headers=h.h())
        state["outcome"] = h.step(run_id)
        v = h.run_view(run_id)
        assert v["pending_approval"] is None and v["reservation"] is None
        assert {"qr_receive_scam", "authority_claim"} <= set(v["risk_signals"])
        events = h.client.get(f"/v1/events?run_id={run_id}", headers=h.h()).json()["events"]
        assert any(e["type"] == "action.denied" and e["data"].get("reason_code") == "SCAM_RISK" for e in events)
    finally:
        h.close()


def test_events_replay_after_cursor_and_sse_stream(mock_h: Harness) -> None:
    h = mock_h
    h.new_session()
    h.activate()
    run_id = h.start_run()
    h.say(run_id, "Is it available?")
    h.step(run_id)
    all_ev = h.client.get(f"/v1/events?run_id={run_id}", headers=h.h()).json()["events"]
    seqs = [e["seq"] for e in all_ev]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    mid = seqs[len(seqs) // 2]
    later = h.client.get(f"/v1/events?run_id={run_id}&after={mid}", headers=h.h()).json()["events"]
    assert [e["seq"] for e in later] == [s for s in seqs if s > mid]
    with h.client.stream("GET", f"/v1/runs/{run_id}/events", headers=h.h(**{"Last-Event-ID": str(mid)})) as resp:
        assert resp.status_code == 200
        ids: list[int] = []
        for line in resp.iter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            if ids and ids[-1] == seqs[-1]:
                break
    assert ids == [s for s in seqs if s > mid]


def test_strict_request_validation_and_error_envelope(mock_h: Harness) -> None:
    h = mock_h
    h.new_session()
    h.activate()
    run_id = h.start_run()
    r = h.client.post(f"/v1/runs/{run_id}/buyer-turns", json={"text": "hi", "role": "owner"}, headers=h.h())
    assert r.status_code == 422 and set(r.json()) == {"code", "message", "retryable", "request_id"}
    m = h.current()["mandate"]
    for bad in ({"min_price_minor": 500.5}, {"min_price_minor": True}, {"min_price_minor": 70000}, {"delivery": "shipping"}):
        r = h.client.patch(f"/v1/mandates/{m['mandate_id']}", json=bad, headers=h.h(**{"If-Match": str(m["current_revision"])}))
        assert r.status_code == 422, bad
    assert h.client.get("/v1/demo-sessions/current").status_code == 401


def test_run_budget_stops_the_agent_visibly(engine: Engine, real_clock: Clock, live_env: None) -> None:
    fake = FakeMeta(lambda body, n: ok(json.dumps(proposal())))
    settings = Settings(provider_mode="live", run_worker=False, web_dist="__none__", max_provider_calls_per_run=2)
    h = Harness(engine, real_clock, settings, MetaAdapter(settings, transport=fake.transport()))
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        for q in ("How much memory?", "And storage?"):
            h.say(run_id, q)
            assert h.step(run_id) == "succeeded"
        h.say(run_id, "Battery?")
        assert h.step(run_id) == "budget_exhausted"
        assert h.run_view(run_id)["state"] == "blocked"
        assert len(fake.requests) == 2
    finally:
        h.close()


def _wav(seconds: float = 0.5) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def test_voice_note_draft_uses_audio_model_and_never_suggests_a_minimum(engine: Engine, real_clock: Clock, live_env: None) -> None:
    draft = {"asking_price_minor": 65000, "currency": "USD", "pickup_only": True, "hold_minutes": 15,
             "share_fact_topics": ["memory", "battery"], "ambiguities": ["You said 'not below 500'; enter the minimum yourself."],
             "language_detected": "hinglish", "transcript": "Laptop 650 dollar mein bechna hai, sirf pickup, 500 se kam nahi."}
    fake = FakeMeta(lambda body, n: ok(json.dumps(draft)))
    h = live_harness(engine, real_clock, fake)
    try:
        h.new_session()
        r = h.client.post("/v1/mandate-drafts/voice", files={"audio": ("note.wav", _wav(), "audio/wav")}, headers=h.h())
        assert r.status_code == 200, r.text
        body = fake.requests[0]["body"]
        assert body["model"] == "muse-spark-1.2"
        parts = body["messages"][1]["content"]
        assert any(p.get("type") == "input_audio" and p["input_audio"]["format"] == "wav" for p in parts)
        out = r.json()
        assert out["status"] == "suggestion_only" and out["provider_mode"] == "live"
        assert out["suggestions"] == {"asking_price_minor": 65000, "reservation_hold_seconds": 900,
                                      "allowed_public_fact_ids": ["f_memory", "f_battery"]}
        assert "min_price_minor" not in out["suggestions"]
        # Nothing was activated or changed by the draft.
        assert h.current()["mandate"]["status"] == "draft" and h.current()["mandate"]["current_revision"] == 1
        bad = h.client.post("/v1/mandate-drafts/voice", files={"audio": ("x.mp3", b"ID3notawav", "audio/mpeg")}, headers=h.h())
        assert bad.status_code == 415
    finally:
        h.close()


def test_unavailable_provider_is_reported_not_faked(engine: Engine, real_clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("delegate.settings.KEY_FILE", __import__("pathlib").Path("/nonexistent/key"))
    settings = Settings(provider_mode="live", run_worker=False, web_dist="__none__")
    h = Harness(engine, real_clock, settings, MetaAdapter(settings))
    try:
        meta = h.client.get("/v1/meta").json()
        assert meta["provider_mode"] == "unavailable" and meta["provider_label"] == "Live model unavailable"
        h.new_session()
        h.activate()
        run_id = h.start_run()
        h.say(run_id, "Is it available?")
        assert h.step(run_id) == "provider_error"
        v = h.run_view(run_id)
        assert v["state"] == "blocked" and [c for c in v["conversation"] if c["role"] == "seller"] == []
        r = h.client.post("/v1/mandate-drafts", json={"goal": "Sell for 650"}, headers=h.h())
        assert r.status_code == 503 and r.json()["code"] == "PROVIDER_UNAVAILABLE"
    finally:
        h.close()
