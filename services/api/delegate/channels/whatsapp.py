"""WhatsApp Business Cloud API adapter: webhook verification, payload parsing and the outbound request.

Written against Meta's developer documentation read on 3 Nov 2026:
  - webhook verification: GET with ``hub.mode=subscribe``, ``hub.verify_token`` and ``hub.challenge``; reply with the
    challenge only if the token matches;
  - event signatures: ``X-Hub-Signature-256: sha256=<hex>``, a SHA-256 HMAC of the raw request body keyed with the
    app secret;
  - inbound payload: ``object = whatsapp_business_account``, ``entry[].changes[]`` with ``field = messages`` and
    ``value.messages[]`` (``from``, ``id``, ``timestamp``, ``type``, ``text.body``); ``value.statuses[]`` reports
    delivery of our own messages;
  - sending: ``POST {graph}/{phone_number_id}/messages`` with ``messaging_product = whatsapp``, ``recipient_type =
    individual``, ``to``, ``type = text``, ``text.body``; free-form messages only within 24 hours of the customer's
    last message, pre-approved templates otherwise.

Fixture-tested only (SYNTHETIC_FIXTURE); not connected to a live business number.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from typing import Any

from delegate.channels.base import ChannelError, InboundMessage, OutboundMessage

CHANNEL = "whatsapp"
SERVICE_WINDOW = dt.timedelta(hours=24)


def verify_subscription(mode: str | None, token: str | None, challenge: str | None, expected_token: str) -> str:
    if not expected_token or mode != "subscribe" or not token or not hmac.compare_digest(token, expected_token):
        raise ChannelError("VERIFY_TOKEN_MISMATCH", "Webhook verification failed.")
    if not challenge:
        raise ChannelError("NO_CHALLENGE", "Webhook verification had no challenge.")
    return challenge


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def parse_webhook(payload: Any) -> list[InboundMessage]:
    """Inbound buyer messages in the payload. Status updates and unknown objects yield nothing."""
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise ChannelError("NOT_WHATSAPP", "Not a WhatsApp Business Account webhook.")
    out: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if (change or {}).get("field") != "messages":
                continue
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                try:
                    sender, mid, ts, kind = str(m["from"]), str(m["id"]), int(m["timestamp"]), str(m["type"])
                except (KeyError, TypeError, ValueError):
                    continue  # malformed message objects are dropped, never guessed at
                body = ((m.get("text") or {}).get("body")) if kind == "text" else None
                out.append(InboundMessage(CHANNEL, sender, mid, dt.datetime.fromtimestamp(ts, dt.timezone.utc), kind,
                                          body if isinstance(body, str) else None))
    return out


def within_service_window(last_inbound_at: dt.datetime | None, now: dt.datetime) -> bool:
    return last_inbound_at is not None and now - last_inbound_at <= SERVICE_WINDOW


def send_request(graph_base: str, phone_number_id: str, msg: OutboundMessage) -> tuple[str, dict[str, Any]]:
    """URL and JSON body for a free-form text reply (only ever a fixed-template rendering, inside the service window)."""
    if msg.channel != CHANNEL:
        raise ChannelError("WRONG_CHANNEL", "Not a WhatsApp message.")
    return f"{graph_base.rstrip('/')}/{phone_number_id}/messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": msg.external_user,
        "type": "text",
        "text": {"body": msg.text, "preview_url": False},
    }
