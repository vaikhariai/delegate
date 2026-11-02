"""Marketplace gate (POLICY.md section 4) built on the reusable ``mandate_gate`` package.

``evaluate`` is a pure function of three snapshots taken at decision time:
  - ``ActionEnvelope``: the server-built action around the model's proposal,
  - ``MandateSnapshot``: the current mandate revision and status,
  - ``GateContext``: world state (run, offers, acceptances, counts, risk signals, approval, clock).
The executor takes these snapshots inside the transaction that holds the mandate and item locks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from mandate_gate import (
    ApprovalRecord,
    Check,
    Decision,
    Gate,
    Outcome,
    action_hash,
    allow_with,
    approval_required,
    check_binding,
    deny,
    invalid,
)

from delegate import risk
from delegate.proposal import Proposal

POLICY_VERSION = "delegate-policy-v1"
LIVE_RUN_STATES = frozenset({"created", "running", "awaiting_buyer", "waiting_approval", "blocked"})

SAFE_MESSAGES: dict[str, str] = {
    "SCHEMA_INVALID": "The proposal did not follow the action format.",
    "OUT_OF_SCOPE": "That action is outside this negotiation.",
    "FENCE_STALE": "This step was superseded by a newer worker.",
    "RUN_NOT_LIVE": "This negotiation has ended.",
    "MANDATE_NOT_ACTIVE": "The seller's permission changed or expired.",
    "MANDATE_PAUSED": "The seller's permission changed or expired.",
    "MANDATE_REVOKED": "The seller's permission changed or expired.",
    "STALE_REVISION": "The seller's permission changed or expired.",
    "MANDATE_EXPIRED": "The seller's permission changed or expired.",
    "ACTION_EXPIRED": "The seller's permission changed or expired.",
    "ACTION_NOT_ALLOWED": "That kind of action is not allowed.",
    "BUDGET_EXHAUSTED": "The message or reservation limit for this negotiation is used up.",
    "UNSUPPORTED_CURRENCY": "This listing is priced in US dollars.",
    "PRICE_OUT_OF_BOUNDS": "That price is outside what the seller allows.",
    "DISCLOSURE_REFERENCE_FORBIDDEN": "That fact is not public.",
    "TERMS_NOT_PERMITTED": "Only pickup with cash at pickup is allowed.",
    "SCAM_RISK": "The seller does not reserve for buyers who raised a payment or authority risk.",
    "ACCEPTANCE_MISSING": "A reservation needs the buyer's explicit acceptance of the current offer.",
    "ACCEPTANCE_MISMATCH": "A reservation needs the buyer's explicit acceptance of the current offer.",
    "ITEM_UNAVAILABLE": "The item is already reserved.",
    "DUPLICATE": "Already done.",
    "APPROVAL_REQUIRED": "The seller must approve this exact reservation.",
    "APPROVAL_EXPIRED": "The seller's approval expired.",
    "APPROVAL_MISMATCH": "The approval does not match this exact reservation.",
}

OWNER_EXPLANATIONS: dict[str, str] = {
    "PRICE_OUT_OF_BOUNDS": "The price was outside your allowed range.",
    "SCAM_RISK": "This buyer raised a scam signal, so reservations are blocked for them.",
    "DISCLOSURE_REFERENCE_FORBIDDEN": "The agent tried to cite something that is not a public fact.",
    "TERMS_NOT_PERMITTED": "Only pickup with cash at pickup is permitted.",
    "STALE_REVISION": "Your permission changed; a new request is needed.",
    "MANDATE_REVOKED": "You revoked the agent's authority.",
    "MANDATE_PAUSED": "You paused the agent.",
    "ACCEPTANCE_MISSING": "The buyer has not explicitly accepted an offer.",
    "ACCEPTANCE_MISMATCH": "The reservation did not match what the buyer accepted.",
}


def safe_message(code: str | None) -> str:
    return SAFE_MESSAGES.get(code or "", "") if code else ""


@dataclass(frozen=True)
class MandateSnapshot:
    mandate_id: str
    status: str  # draft | active | paused | revoked | expired
    revision: int
    currency: str
    asking_price_minor: int
    min_price_minor: int
    allowed_actions: frozenset[str]
    allowed_public_fact_ids: frozenset[str]
    delivery: str
    payment_terms: str
    reservation_requires_owner_approval: bool
    reservation_hold_seconds: int
    max_reservations: int
    max_outbound_messages: int
    expires_at: dt.datetime | None


@dataclass(frozen=True)
class ActionEnvelope:
    action_id: str
    session_id: str
    run_id: str
    item_id: str
    buyer_id: str
    mandate_id: str
    mandate_revision: int
    created_at: dt.datetime
    expires_at: dt.datetime
    proposal: Proposal | None
    raw_proposal: Any = None
    validation_error: str | None = None
    delivery: str = "pickup"
    payment_terms: str = "cash_at_pickup"
    hold_seconds: int = 0

    @property
    def kind(self) -> str | None:
        return self.proposal.kind if self.proposal else None

    def hash_body(self) -> dict[str, Any]:
        """The fields bound by the action hash (no transport metadata)."""
        body: dict[str, Any] = {
            "schema_version": "delegate-action-v1",
            "action_id": self.action_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "item_id": self.item_id,
            "buyer_id": self.buyer_id,
            "mandate_id": self.mandate_id,
            "mandate_revision": self.mandate_revision,
            "created_at": self.created_at.astimezone(dt.timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(dt.timezone.utc).isoformat(),
            "payload": self.proposal.as_dict() if self.proposal else {"invalid": True},
        }
        if self.proposal and self.proposal.kind == "reserve_item":
            body["terms"] = {
                "delivery": self.delivery,
                "payment_terms": self.payment_terms,
                "hold_seconds": self.hold_seconds,
            }
        return body

    def hash(self) -> str:
        return action_hash(self.hash_body())


@dataclass(frozen=True)
class OfferSnap:
    offer_id: str
    price_minor: int
    currency: str
    status: str  # open | superseded | accepted | withdrawn


@dataclass(frozen=True)
class AcceptanceSnap:
    acceptance_event_id: str
    offer_id: str
    buyer_is_run_buyer: bool
    used: bool


@dataclass(frozen=True)
class GateContext:
    now: dt.datetime
    auth_session_id: str
    run_id: str
    item_id: str
    owner_principal: str
    run_state: str
    fence_current: bool
    outbound_messages: int
    reservations: int
    item_reserved: bool
    buyer_signals: frozenset[str]
    offers: dict[str, OfferSnap] = field(default_factory=dict)
    acceptances: dict[str, AcceptanceSnap] = field(default_factory=dict)
    committed_operation: bool = False
    approval: ApprovalRecord | None = None




def _schema(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    if a.proposal is None:
        return invalid("SCHEMA_INVALID", a.validation_error or "")
    return None


def _scope(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    if a.session_id != c.auth_session_id or a.run_id != c.run_id or a.item_id != c.item_id:
        return deny("OUT_OF_SCOPE")
    return None


def _fence(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    if not c.fence_current:
        return deny("FENCE_STALE")
    if c.run_state not in LIVE_RUN_STATES:
        return deny("RUN_NOT_LIVE")
    return None


def _authority(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    if m.status == "revoked":
        return deny("MANDATE_REVOKED")
    if m.status == "paused":
        return deny("MANDATE_PAUSED")
    if m.status == "expired" or (m.expires_at is not None and c.now >= m.expires_at):
        return deny("MANDATE_EXPIRED")
    if m.status != "active":
        return deny("MANDATE_NOT_ACTIVE")
    if a.mandate_revision != m.revision:
        return deny("STALE_REVISION")
    if c.now >= a.expires_at:
        return deny("ACTION_EXPIRED")
    return None


def _allowed(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    p = a.proposal
    assert p is not None
    if p.kind == "no_action":
        return None
    if p.kind not in m.allowed_actions:
        return deny("ACTION_NOT_ALLOWED")
    if p.kind == "send_message" and c.outbound_messages >= m.max_outbound_messages:
        return deny("BUDGET_EXHAUSTED")
    if p.kind == "reserve_item" and c.reservations >= m.max_reservations:
        return deny("BUDGET_EXHAUSTED")
    return None


def _money(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    p = a.proposal
    assert p is not None
    if p.price_minor is None:
        return None
    if p.currency != m.currency:
        return deny("UNSUPPORTED_CURRENCY")
    if p.message_intent == "counter_offer" or p.kind == "reserve_item":
        if not (m.min_price_minor <= p.price_minor <= m.asking_price_minor):
            return deny("PRICE_OUT_OF_BOUNDS")
    return None


def _terms(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    p = a.proposal
    assert p is not None
    if any(f not in m.allowed_public_fact_ids for f in p.fact_ids):
        return deny("DISCLOSURE_REFERENCE_FORBIDDEN")
    if p.kind == "reserve_item":
        if (
            m.delivery != "pickup_only"
            or a.delivery != "pickup"
            or a.payment_terms != m.payment_terms
            or a.payment_terms != "cash_at_pickup"
            or a.hold_seconds != m.reservation_hold_seconds
        ):
            return deny("TERMS_NOT_PERMITTED")
        if risk.has_high(c.buyer_signals | set(p.buyer_risk_signals)):
            return deny("SCAM_RISK")
    return None


def _acceptance(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    p = a.proposal
    assert p is not None
    if p.kind != "reserve_item":
        return None
    acc = c.acceptances.get(p.acceptance_event_id or "")
    if acc is None or not acc.buyer_is_run_buyer:
        return deny("ACCEPTANCE_MISSING")
    offer = c.offers.get(acc.offer_id)
    if acc.used or offer is None or offer.status != "open":
        return deny("ACCEPTANCE_MISMATCH")
    if offer.price_minor != p.price_minor or offer.currency != p.currency:
        return deny("ACCEPTANCE_MISMATCH")
    if c.item_reserved:
        return deny("ITEM_UNAVAILABLE")
    return None


def _duplicate(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    return allow_with("DUPLICATE") if c.committed_operation else None


def _approval(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Outcome | None:
    p = a.proposal
    assert p is not None
    if p.kind != "reserve_item" or not m.reservation_requires_owner_approval:
        return None
    code = check_binding(
        c.approval,
        action_id=a.action_id,
        action_hash=a.hash(),
        principal=c.owner_principal,
        authority_revision=m.revision,
        now=c.now,
    )
    return approval_required(code) if code else None


GATE: Gate[ActionEnvelope, MandateSnapshot, GateContext] = Gate(
    [
        Check("1_schema", _schema),
        Check("2_scope", _scope),
        Check("3_fence_and_run", _fence),
        Check("4_authority", _authority),
        Check("5_allowed_and_budget", _allowed),
        Check("6_money", _money),
        Check("7_terms_disclosure_risk", _terms),
        Check("8_acceptance", _acceptance),
        Check("9_duplicate", _duplicate),
        Check("10_approval", _approval),
    ]
)


def evaluate(action: ActionEnvelope, mandate: MandateSnapshot, ctx: GateContext) -> Decision:
    return GATE.evaluate(action, mandate, ctx)
