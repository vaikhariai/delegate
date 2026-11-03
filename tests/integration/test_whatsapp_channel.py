"""WhatsApp Business Cloud API adapter, against real PostgreSQL with synthetic webhook payloads (SYNTHETIC_FIXTURE).

Payload shapes follow Meta's documented examples (inbound text message, status update); the identifiers and secrets
below are made up. Nothing here talks to Meta: outbound sends go to an in-process fake transport.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from sqlalchemy import Engine

from delegate.channels import service, whatsapp
from delegate.clock import Clock
from delegate.db import exe, new_id, one, rows, tx
from tests.integration.helpers import Harness, mock_settings

SECRET = "synthetic-app-secret"
VERIFY = "synthetic-verify-token"
BUYER = "15551230001"
PHONE_ID = "100000000000001"


def settings(**over: Any) -> Any:
    return mock_settings(whatsapp_enabled=True, whatsapp_app_secret=SECRET, whatsapp_verify_token=VERIFY,
                         whatsapp_access_token="synthetic-token", whatsapp_phone_number_id=PHONE_ID, **over)


def inbound(text: str, mid: str, sender: str = BUYER, ts: float | None = None, kind: str = "text") -> dict[str, Any]:
    msg: dict[str, Any] = {"from": sender, "id": mid, "timestamp": str(int(ts if ts is not None else time.time())), "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": PHONE_ID},
        "contacts": [{"profile": {"name": "Synthetic Buyer"}, "wa_id": sender}], "messages": [msg]}}]}]}


def status_update() -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": PHONE_ID},
        "statuses": [{"id": "wamid.out1", "status": "delivered", "timestamp": str(int(time.time())), "recipient_id": BUYER}]}}]}]}


def post(h: Harness, payload: dict[str, Any], secret: str = SECRET, sign: bool = True) -> Any:
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return h.client.post("/v1/channels/whatsapp/webhook", content=raw, headers=headers)


def bound(engine: Engine, clock: Clock) -> tuple[Harness, str]:
    h = Harness(engine, clock, settings())
    h.new_session()
    h.activate()
    run_id = h.start_run()
    r = h.client.post(f"/v1/runs/{run_id}/channel-binding", json={"channel": "whatsapp", "external_user": BUYER}, headers=h.h())
    assert r.status_code == 201, r.text
    return h, run_id


def buyer_texts(h: Harness, run_id: str) -> list[str]:
    return [m["text"] for m in h.run_view(run_id)["conversation"] if m["role"] == "buyer"]


def test_routes_do_not_exist_unless_enabled(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock)
    try:
        assert post(h, inbound("hi", "wamid.x")).status_code == 404
        assert h.client.get("/v1/channels/whatsapp/webhook", params={"hub.mode": "subscribe"}).status_code == 404
    finally:
        h.close()


def test_subscription_verification(engine: Engine, real_clock: Clock) -> None:
    h = Harness(engine, real_clock, settings())
    try:
        ok = h.client.get("/v1/channels/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"})
        assert ok.status_code == 200 and ok.text == "1158201444"
        bad = h.client.get("/v1/channels/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})
        assert bad.status_code == 403
    finally:
        h.close()


def test_signature_is_required_and_checked(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    try:
        assert post(h, inbound("Is it available?", "wamid.s1"), sign=False).status_code == 403
        assert post(h, inbound("Is it available?", "wamid.s2"), secret="another-secret").status_code == 403
        # a valid signature over different bytes is still refused
        raw = json.dumps(inbound("Is it available?", "wamid.s3")).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw + b" ", hashlib.sha256).hexdigest()
        assert h.client.post("/v1/channels/whatsapp/webhook", content=raw, headers={"X-Hub-Signature-256": sig}).status_code == 403
        assert buyer_texts(h, run_id) == []
    finally:
        h.close()


def test_inbound_text_reaches_the_bound_run_once(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    try:
        r = post(h, inbound("Is it still available?", "wamid.a1"))
        assert r.status_code == 200 and r.json()["outcomes"] == {"accepted": 1}
        again = post(h, inbound("Is it still available?", "wamid.a1"))  # Meta redelivers; a replay looks the same
        assert again.json()["outcomes"] == {"duplicate": 1}
        assert buyer_texts(h, run_id) == ["Is it still available?"]
        # The text is untrusted buyer input to the same gate: a scam message is scanned like any other.
        post(h, inbound("scan this QR to receive the money", "wamid.a2"))
        with engine.connect() as c:
            ev = rows(c, "SELECT risk_signals FROM buyer_events WHERE run_id = :r ORDER BY created_at", r=run_id)
        assert ev[-1]["risk_signals"]
    finally:
        h.close()


def test_stale_unbound_status_and_non_text_messages_are_not_turns(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    try:
        assert post(h, inbound("old", "wamid.b1", ts=time.time() - 7200)).json()["outcomes"] == {"stale": 1}
        assert post(h, inbound("hello", "wamid.b2", sender="15559990000")).json()["outcomes"] == {"unbound": 1}
        assert post(h, inbound("", "wamid.b3", kind="image")).json()["outcomes"] == {"unsupported_type": 1}
        assert post(h, status_update()).json() == {"received": 0, "outcomes": {}}
        assert post(h, {"object": "page", "entry": []}).status_code == 400
        assert buyer_texts(h, run_id) == []
    finally:
        h.close()


def test_binding_is_owner_only_and_one_to_one(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    other = Harness(engine, real_clock, settings())
    try:
        other.new_session()
        r = other.client.post(f"/v1/runs/{run_id}/channel-binding", json={"channel": "whatsapp", "external_user": "15550000002"},
                              headers=other.h())
        assert r.status_code == 404  # another session cannot bind someone else's negotiation
        run2 = h.start_run("buyer-arjun")
        r = h.client.post(f"/v1/runs/{run2}/channel-binding", json={"channel": "whatsapp", "external_user": BUYER}, headers=h.h())
        assert r.status_code == 409
        r = h.client.post(f"/v1/runs/{run2}/channel-binding", json={"channel": "whatsapp", "external_user": "not-a-number"}, headers=h.h())
        assert r.status_code == 422
    finally:
        other.close()
        h.close()


class FakeGraph:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            self.sent.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": json.loads(req.content)})
            return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"input": BUYER, "wa_id": BUYER}],
                                             "messages": [{"id": f"wamid.out{len(self.sent)}"}]})
        return httpx.MockTransport(handle)


def test_agent_replies_go_out_as_fixed_templates_inside_the_window(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    try:
        post(h, inbound("Is it still available? How much memory?", "wamid.c1"))
        h.step(run_id)
        graph = FakeGraph()
        assert service.deliver_pending(engine, real_clock, h.settings, graph.transport()) == {"sent": 1}
        seller = [m for m in h.run_view(run_id)["conversation"] if m["role"] == "seller"]
        assert len(graph.sent) == 1 and graph.sent[0]["body"]["text"]["body"] == seller[-1]["text"]
        assert graph.sent[0]["url"].endswith(f"/{PHONE_ID}/messages") and graph.sent[0]["body"]["to"] == BUYER
        assert graph.sent[0]["auth"] == "Bearer synthetic-token"
        # Nothing is sent twice.
        assert service.deliver_pending(engine, real_clock, h.settings, graph.transport()) == {}
        # A seller-side message that is not a template rendering is never sent.
        with tx(engine) as c:
            sid = one(c, "SELECT session_id FROM runs WHERE run_id = :r", r=run_id)["session_id"]  # type: ignore[index]
            exe(c, "INSERT INTO marketplace_messages (message_id, session_id, run_id, role, text, created_at) "
                   "VALUES (:m, :s, :r, 'seller', 'free text', :n)", m=new_id("msg"), s=sid, r=run_id, n=real_clock.now())
        assert service.deliver_pending(engine, real_clock, h.settings, graph.transport()) == {}
        assert len(graph.sent) == 1
    finally:
        h.close()


def test_no_free_form_send_outside_the_24_hour_window(engine: Engine, real_clock: Clock) -> None:
    h, run_id = bound(engine, real_clock)
    try:
        post(h, inbound("Would you take $560?", "wamid.d1"))
        h.step(run_id)
        with tx(engine) as c:
            exe(c, "UPDATE channel_bindings SET last_inbound_at = :t WHERE run_id = :r",
                t=real_clock.now() - dt.timedelta(hours=25), r=run_id)
        graph = FakeGraph()
        assert service.deliver_pending(engine, real_clock, h.settings, graph.transport()) == {"outside_window": 1}
        assert graph.sent == []
    finally:
        h.close()


def test_parser_and_signature_units() -> None:
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(b"k", raw, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(raw, good, "k")
    assert not whatsapp.verify_signature(raw, good, "")
    assert not whatsapp.verify_signature(raw, good.replace("sha256=", "sha1="), "k")
    msgs = whatsapp.parse_webhook(inbound("hi", "wamid.u1"))
    assert [(m.external_user, m.external_message_id, m.kind, m.text) for m in msgs] == [(BUYER, "wamid.u1", "text", "hi")]
    broken = inbound("hi", "wamid.u2")
    del broken["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
    assert whatsapp.parse_webhook(broken) == []
