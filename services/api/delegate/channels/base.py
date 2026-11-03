"""The channel seam: where buyer messages come from and where the seller's fixed-template replies go.

The gate, the model and the seller console do not depend on the channel. A channel only
  - normalises inbound buyer messages into ``InboundMessage`` (untrusted text, an external user ID and a message ID), and
  - delivers seller messages that the server already rendered from fixed templates.
It never carries authority: approving a reservation happens in the seller console, never in the buyer's chat.

The in-app buyer simulator (``POST /v1/runs/{id}/buyer-turns``) is the default channel, ``sandbox``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    external_user: str
    external_message_id: str
    sent_at: dt.datetime
    kind: str  # "text", or the provider's type for messages we do not accept (image, audio, ...)
    text: str | None


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    external_user: str
    text: str  # always a seller message rendered by the server from a fixed template


class ChannelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
