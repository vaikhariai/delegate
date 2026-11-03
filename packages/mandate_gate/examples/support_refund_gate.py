"""Example integration: a support agent proposes refunds and replies; a support-policy mandate bounds it.

The same pattern as Delegate's marketplace gate, in another domain:
  - the agent may grant a refund up to ``auto_limit_minor`` on its own;
  - above that, a manager must approve that exact refund (order, amount, reason, policy revision);
  - nothing above ``hard_limit_minor`` or above the order total, ever, approval or not;
  - one refund per order, and only for an order that belongs to the customer in this conversation;
  - replies are fixed templates chosen by key, so internal policy (the limits themselves) never reaches the customer.

Package example only: there is no support product behind it. Run: python packages/mandate_gate/examples/support_refund_gate.py
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mandate_gate import (
    ApprovalRecord,
    ApprovalStatus,
    Check,
    Gate,
    action_hash,
    approval_required,
    check_binding,
    deny,
    invalid,
)

REASONS = frozenset({"damaged", "late", "not_as_described"})
TEMPLATES = {
    "refund_issued": "We've refunded your order. You'll see it on your statement within 5 business days.",
    "refund_pending_review": "A team lead is reviewing your refund. We'll update you here.",
    "cannot_refund": "I can't refund this order. I can connect you with a team lead.",
}


@dataclass(frozen=True)
class Proposal:
    """What the model proposes. Untrusted: every field is checked before anything happens."""

    action_id: str
    kind: object  # "refund" or "reply"
    order_id: object = None
    amount_minor: object = None
    reason: object = None
    template: object = None


@dataclass(frozen=True)
class SupportMandate:
    revision: int
    active: bool
    currency: str
    auto_limit_minor: int
    hard_limit_minor: int


@dataclass(frozen=True)
class Ctx:
    """Server-side facts, never from the model."""

    customer_id: str
    orders: dict[str, tuple[str, int]]  # order_id -> (customer_id, total_minor)
    refunded: frozenset[str]
    manager: str
    now: dt.datetime
    approval: ApprovalRecord | None


def envelope(p: Proposal, m: SupportMandate, c: Ctx) -> dict[str, object]:
    return {"action_id": p.action_id, "kind": p.kind, "order_id": p.order_id, "amount_minor": p.amount_minor,
            "reason": p.reason, "customer_id": c.customer_id, "currency": m.currency, "revision": m.revision}


def _types(p: Proposal, m: SupportMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if p.kind == "reply":
        return None if isinstance(p.template, str) and p.template in TEMPLATES else invalid("UNKNOWN_TEMPLATE")
    if p.kind != "refund":
        return invalid("UNKNOWN_KIND")
    if type(p.amount_minor) is not int or p.amount_minor <= 0:
        return invalid("AMOUNT_NOT_POSITIVE_INTEGER")
    if p.reason not in REASONS:
        return invalid("UNKNOWN_REASON")
    return None if isinstance(p.order_id, str) else invalid("ORDER_ID_MISSING")


def _authority(p: Proposal, m: SupportMandate, c: Ctx):  # type: ignore[no-untyped-def]
    return None if m.active else deny("MANDATE_NOT_ACTIVE")


def _scope(p: Proposal, m: SupportMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if p.kind == "reply":
        return None
    order = c.orders.get(str(p.order_id))
    if order is None or order[0] != c.customer_id:
        return deny("ORDER_NOT_THIS_CUSTOMERS")
    return deny("ALREADY_REFUNDED") if p.order_id in c.refunded else None


def _bounds(p: Proposal, m: SupportMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if p.kind == "reply":
        return None
    assert isinstance(p.amount_minor, int)
    if p.amount_minor > c.orders[str(p.order_id)][1]:
        return deny("OVER_ORDER_TOTAL")
    return deny("OVER_HARD_LIMIT") if p.amount_minor > m.hard_limit_minor else None


def _approval(p: Proposal, m: SupportMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if p.kind == "reply":
        return None
    assert isinstance(p.amount_minor, int)
    if p.amount_minor <= m.auto_limit_minor:
        return None
    code = check_binding(c.approval, action_id=p.action_id, action_hash=action_hash(envelope(p, m, c)),
                         principal=c.manager, authority_revision=m.revision, now=c.now)
    return approval_required(code) if code else None


# Hard checks come before the approval check, so an approval can never override a denial.
GATE: Gate[Proposal, SupportMandate, Ctx] = Gate([
    Check("types", _types), Check("authority", _authority), Check("scope", _scope), Check("bounds", _bounds),
    Check("approval", _approval),
])


def reply_text(p: Proposal) -> str:
    """Customer-facing text is looked up from the template key; the model never writes it."""
    assert isinstance(p.template, str)
    return TEMPLATES[p.template]


if __name__ == "__main__":
    m = SupportMandate(revision=2, active=True, currency="USD", auto_limit_minor=2_500, hard_limit_minor=20_000)
    now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
    ctx = Ctx("cust-1", {"o-1": ("cust-1", 8_000), "o-2": ("cust-9", 9_000)}, frozenset(), "lead-4", now, None)
    small = Proposal("r1", "refund", "o-1", 1_500, "late")
    big = Proposal("r2", "refund", "o-1", 6_000, "damaged")
    print("small refund:", GATE.evaluate(small, m, ctx).verdict)
    print("big refund, no approval:", GATE.evaluate(big, m, ctx).verdict)
    ok = ApprovalRecord("ap1", "r2", action_hash(envelope(big, m, ctx)), "lead-4", 2, ApprovalStatus.APPROVED,
                        now + dt.timedelta(minutes=5))
    approved = Ctx(ctx.customer_id, ctx.orders, ctx.refunded, ctx.manager, now, ok)
    print("big refund, approved:", GATE.evaluate(big, m, approved).verdict)
    print("someone else's order:", GATE.evaluate(Proposal("r3", "refund", "o-2", 500, "late"), m, ctx).reason_code)
    print("free-text reply:", GATE.evaluate(Proposal("r4", "reply", template="Our auto limit is $25"), m, ctx).reason_code)
