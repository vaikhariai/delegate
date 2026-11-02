"""The model's proposal contract (POLICY.md section 2): strict parsing plus semantic rules.

The JSON schema sent to the provider is flat (no oneOf/allOf/$ref, every property required, no extra
properties) so it fits the documented structured-output subset. Provider-side constraints are a convenience;
``parse_proposal`` re-validates everything locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, get_args

Kind = Literal["send_message", "reserve_item", "no_action"]
Intent = Literal["answer_listing", "counter_offer", "clarify", "decline"]
ClarifyTopic = Literal["price_amount", "currency", "pickup_time", "acceptance", "item_details"]
DeclineReason = Literal[
    "price_not_acceptable",
    "delivery_not_offered",
    "private_info_request",
    "off_platform_payment",
    "qr_receive_scam",
    "fake_payment_proof",
    "authority_claim",
    "unsupported_request",
]
Language = Literal["en", "hinglish"]
RiskSignal = Literal[
    "qr_receive_scam",
    "fake_payment_proof",
    "authority_claim",
    "off_platform_payment",
    "shipping_request",
    "private_info_request",
    "off_platform_contact",
]
Currency = Literal["USD"]

FIELDS: tuple[str, ...] = (
    "kind",
    "message_intent",
    "fact_ids",
    "price_minor",
    "currency",
    "clarify_topic",
    "decline_reason",
    "acceptance_event_id",
    "language",
    "buyer_risk_signals",
    "rationale",
)
RATIONALE_MAX = 240


class ProposalInvalid(ValueError):
    """The proposal does not match POLICY.md section 2."""


@dataclass(frozen=True)
class Proposal:
    kind: Kind
    message_intent: Intent | None
    fact_ids: tuple[str, ...]
    price_minor: int | None
    currency: Currency | None
    clarify_topic: ClarifyTopic | None
    decline_reason: DeclineReason | None
    acceptance_event_id: str | None
    language: Language
    buyer_risk_signals: tuple[RiskSignal, ...]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message_intent": self.message_intent,
            "fact_ids": list(self.fact_ids),
            "price_minor": self.price_minor,
            "currency": self.currency,
            "clarify_topic": self.clarify_topic,
            "decline_reason": self.decline_reason,
            "acceptance_event_id": self.acceptance_event_id,
            "language": self.language,
            "buyer_risk_signals": list(self.buyer_risk_signals),
            "rationale": self.rationale,
        }


def _enum(value: Any, literal: Any, name: str, nullable: bool) -> Any:
    if value is None:
        if nullable:
            return None
        raise ProposalInvalid(f"{name} is required")
    if not isinstance(value, str) or value not in get_args(literal):
        raise ProposalInvalid(f"{name} has an unknown value")
    return value


def _require_null(data: dict[str, Any], names: tuple[str, ...], why: str) -> None:
    for name in names:
        if data[name] is not None:
            raise ProposalInvalid(f"{name} must be null {why}")


def parse_proposal(raw: Any) -> Proposal:
    """Parse a JSON string or object into a Proposal, or raise ProposalInvalid."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ProposalInvalid("not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ProposalInvalid("proposal must be an object")
    keys = set(raw)
    if keys != set(FIELDS):
        extra, missing = sorted(keys - set(FIELDS)), sorted(set(FIELDS) - keys)
        raise ProposalInvalid(f"fields differ: extra={extra} missing={missing}")
    d = dict(raw)

    kind = _enum(d["kind"], Kind, "kind", False)
    intent = _enum(d["message_intent"], Intent, "message_intent", True)
    clarify = _enum(d["clarify_topic"], ClarifyTopic, "clarify_topic", True)
    decline = _enum(d["decline_reason"], DeclineReason, "decline_reason", True)
    language = _enum(d["language"], Language, "language", False)
    currency = _enum(d["currency"], Currency, "currency", True)

    price = d["price_minor"]
    if price is not None and (type(price) is not int or price < 0):
        raise ProposalInvalid("price_minor must be a non-negative integer number of cents")

    facts = d["fact_ids"]
    if not isinstance(facts, list) or not all(isinstance(f, str) and f for f in facts):
        raise ProposalInvalid("fact_ids must be a list of non-empty strings")
    if len(set(facts)) != len(facts) or len(facts) > 6:
        raise ProposalInvalid("fact_ids must be unique and at most 6")

    signals = d["buyer_risk_signals"]
    if not isinstance(signals, list):
        raise ProposalInvalid("buyer_risk_signals must be a list")
    for s in signals:
        _enum(s, RiskSignal, "buyer_risk_signals[]", False)

    acc = d["acceptance_event_id"]
    if acc is not None and (not isinstance(acc, str) or not acc):
        raise ProposalInvalid("acceptance_event_id must be a non-empty string or null")

    rationale = d["rationale"]
    if not isinstance(rationale, str) or len(rationale) > RATIONALE_MAX:
        raise ProposalInvalid(f"rationale must be a string of at most {RATIONALE_MAX} characters")

    if (price is None) != (currency is None):
        raise ProposalInvalid("price_minor and currency must be given together")

    if kind == "send_message":
        if intent is None:
            raise ProposalInvalid("send_message needs message_intent")
        _require_null(d, ("acceptance_event_id",), "for send_message")
        if intent == "answer_listing":
            if not facts:
                raise ProposalInvalid("answer_listing needs fact_ids")
            _require_null(d, ("price_minor", "clarify_topic", "decline_reason"), "for answer_listing")
        elif intent == "counter_offer":
            if price is None:
                raise ProposalInvalid("counter_offer needs price_minor and currency")
            _require_null(d, ("clarify_topic", "decline_reason"), "for counter_offer")
        elif intent == "clarify":
            if clarify is None:
                raise ProposalInvalid("clarify needs clarify_topic")
            _require_null(d, ("price_minor", "decline_reason"), "for clarify")
        else:  # decline
            if decline is None:
                raise ProposalInvalid("decline needs decline_reason")
            _require_null(d, ("price_minor", "clarify_topic"), "for decline")
    elif kind == "reserve_item":
        _require_null(d, ("message_intent", "clarify_topic", "decline_reason"), "for reserve_item")
        if price is None or acc is None:
            raise ProposalInvalid("reserve_item needs price_minor, currency and acceptance_event_id")
    else:  # no_action
        _require_null(
            d,
            ("message_intent", "price_minor", "clarify_topic", "decline_reason", "acceptance_event_id"),
            "for no_action",
        )

    return Proposal(
        kind=kind,
        message_intent=intent,
        fact_ids=tuple(facts),
        price_minor=price,
        currency=currency,
        clarify_topic=clarify,
        decline_reason=decline,
        acceptance_event_id=acc,
        language=language,
        buyer_risk_signals=tuple(signals),
        rationale=rationale,
    )


def provider_schema(public_fact_ids: list[str]) -> dict[str, Any]:
    """Flat JSON schema for response_format (strict subset: every property required, no extras)."""

    def nullable_enum(values: tuple[str, ...]) -> dict[str, Any]:
        return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}

    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(get_args(Kind))},
            "message_intent": nullable_enum(get_args(Intent)),
            "fact_ids": {"type": "array", "items": {"type": "string", "enum": public_fact_ids}},
            "price_minor": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "currency": nullable_enum(get_args(Currency)),
            "clarify_topic": nullable_enum(get_args(ClarifyTopic)),
            "decline_reason": nullable_enum(get_args(DeclineReason)),
            "acceptance_event_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "language": {"type": "string", "enum": list(get_args(Language))},
            "buyer_risk_signals": {"type": "array", "items": {"type": "string", "enum": list(get_args(RiskSignal))}},
            "rationale": {"type": "string"},
        },
        "required": list(FIELDS),
        "additionalProperties": False,
    }
