"""Wiring between an external channel and the existing negotiation runs.

Inbound: each message ID is recorded once (``channel_inbound``), so redelivered or replayed webhooks are processed at
most once; messages older than ``whatsapp_max_message_age_s`` are refused; text from a bound buyer goes through the same
``store.buyer_turn`` as the in-app simulator, so it is untrusted input to the same gate.

Outbound: only seller messages the server rendered from a fixed template (``template_key`` set) are delivered, only to
the bound buyer, and only inside the 24-hour customer-service window; anything else is recorded, not sent.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import Engine

from delegate import store
from delegate.channels import whatsapp
from delegate.channels.base import InboundMessage, OutboundMessage
from delegate.clock import Clock
from delegate.db import exe, one, rows, tx
from delegate.settings import Settings

log = logging.getLogger("delegate.channels")
INBOUND_RETENTION = dt.timedelta(days=7)


def bind(engine: Engine, clock: Clock, p: store.Principal, run_id: str, channel: str, external_user: str) -> dict[str, Any]:
    external_user = (external_user or "").strip()
    if channel != whatsapp.CHANNEL or not external_user.isdigit() or not 6 <= len(external_user) <= 20:
        raise store.ApiError(422, "INVALID_BINDING", "Give a WhatsApp ID (digits only) for this buyer.")
    with tx(engine) as conn:
        store.get_run(conn, p.session_id, run_id, for_update=True)  # 404 for another session's run
        taken = one(conn, "SELECT run_id FROM channel_bindings WHERE (channel = :c AND external_user = :u) OR run_id = :r",
                    c=channel, u=external_user, r=run_id)
        if taken is not None:
            raise store.ApiError(409, "ALREADY_BOUND", "This buyer or this negotiation is already bound to a channel.")
        exe(conn, "INSERT INTO channel_bindings (channel, external_user, session_id, run_id, created_at) VALUES (:c, :u, :s, :r, :n)",
            c=channel, u=external_user, s=p.session_id, r=run_id, n=clock.now())
    return {"channel": channel, "run_id": run_id}


def handle_inbound(engine: Engine, clock: Clock, settings: Settings, msgs: list[InboundMessage]) -> dict[str, int]:
    outcomes: dict[str, int] = {}
    now = clock.now()
    for m in msgs:
        outcome = _one_inbound(engine, clock, settings, m, now)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


def _one_inbound(engine: Engine, clock: Clock, settings: Settings, m: InboundMessage, now: dt.datetime) -> str:
    with tx(engine) as conn:
        fresh = one(conn, "INSERT INTO channel_inbound (channel, external_message_id, received_at, outcome) VALUES (:c, :i, :n, 'received') "
                          "ON CONFLICT DO NOTHING RETURNING 1 AS x", c=m.channel, i=m.external_message_id, n=now)
    if fresh is None:
        return "duplicate"

    def done(outcome: str) -> str:
        with tx(engine) as conn:
            exe(conn, "UPDATE channel_inbound SET outcome = :o WHERE channel = :c AND external_message_id = :i",
                o=outcome, c=m.channel, i=m.external_message_id)
        return outcome

    if now - m.sent_at > dt.timedelta(seconds=settings.whatsapp_max_message_age_s) or m.sent_at - now > dt.timedelta(minutes=5):
        return done("stale")
    if m.kind != "text" or not m.text:
        return done("unsupported_type")
    with engine.connect() as conn:
        b = one(conn, "SELECT session_id, run_id FROM channel_bindings WHERE channel = :c AND external_user = :u", c=m.channel, u=m.external_user)
    if b is None:
        return done("unbound")
    try:
        store.buyer_turn(engine, clock, store.Principal(b["session_id"]), b["run_id"], m.text, settings.max_buyer_turns_per_run)
    except store.ApiError as exc:
        return done(f"refused:{exc.code}")
    with tx(engine) as conn:
        exe(conn, "UPDATE channel_bindings SET last_inbound_at = :t WHERE channel = :c AND external_user = :u",
            t=m.sent_at, c=m.channel, u=m.external_user)
    return done("accepted")


def deliver_pending(engine: Engine, clock: Clock, settings: Settings,
                    transport: httpx.BaseTransport | None = None) -> dict[str, int]:
    """Send fixed-template seller messages of bound runs that have no delivery record yet."""
    now = clock.now()
    with engine.connect() as conn:
        pending = rows(conn, "SELECT m.message_id, m.text, b.channel, b.external_user, b.last_inbound_at "
                             "FROM marketplace_messages m JOIN channel_bindings b ON b.run_id = m.run_id "
                             "LEFT JOIN channel_deliveries d ON d.message_id = m.message_id "
                             "WHERE m.role = 'seller' AND m.template_key IS NOT NULL AND d.message_id IS NULL "
                             "AND m.created_at >= b.created_at ORDER BY m.created_at")
    counts: dict[str, int] = {}
    for r in pending:
        status, provider_id, detail = _send(settings, r, now, transport)
        with tx(engine) as conn:
            exe(conn, "INSERT INTO channel_deliveries (message_id, channel, external_user, status, provider_message_id, detail, created_at) "
                      "VALUES (:m, :c, :u, :s, :p, :d, :n) ON CONFLICT DO NOTHING",
                m=r["message_id"], c=r["channel"], u=r["external_user"], s=status, p=provider_id, d=detail, n=now)
        counts[status] = counts.get(status, 0) + 1
    with tx(engine) as conn:
        exe(conn, "DELETE FROM channel_inbound WHERE received_at < :t", t=now - INBOUND_RETENTION)
    return counts


def _send(settings: Settings, r: dict[str, Any], now: dt.datetime,
          transport: httpx.BaseTransport | None) -> tuple[str, str | None, str | None]:
    if not whatsapp.within_service_window(r["last_inbound_at"], now):
        return "outside_window", None, "more than 24 hours since the buyer's last message; templates only"
    url, body = whatsapp.send_request(settings.whatsapp_graph_base, settings.whatsapp_phone_number_id,
                                      OutboundMessage(r["channel"], r["external_user"], r["text"]))
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"})
    except httpx.HTTPError as exc:
        return "error", None, type(exc).__name__
    if resp.status_code != 200:
        return "error", None, f"HTTP {resp.status_code}"
    try:
        provider_id = str(resp.json()["messages"][0]["id"])
    except (ValueError, KeyError, IndexError, TypeError):
        provider_id = None
    return "sent", provider_id, None


def delivery_loop(engine: Engine, clock: Clock, settings: Settings, stop: Callable[[], bool], interval_s: float = 2.0) -> None:
    import time

    while not stop():
        try:
            deliver_pending(engine, clock, settings)
        except Exception:  # noqa: BLE001 - a delivery failure must never stop the loop; it is logged
            log.exception("channel delivery failed")
        time.sleep(interval_s)

