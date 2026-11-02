"""Example integration: an agent proposes reimbursing an expense; a finance mandate bounds it.

Run: python packages/mandate_gate/examples/expense_gate.py
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


@dataclass(frozen=True)
class Reimburse:
    action_id: str
    employee: str
    amount_minor: object  # untrusted until checked
    currency: object
    category: str


@dataclass(frozen=True)
class FinanceMandate:
    revision: int
    active: bool
    currency: str
    auto_limit_minor: int
    hard_limit_minor: int
    categories: frozenset[str]


@dataclass(frozen=True)
class Ctx:
    principal: str
    now: dt.datetime
    approval: ApprovalRecord | None


def envelope(a: Reimburse, m: FinanceMandate) -> dict[str, object]:
    return {"action_id": a.action_id, "employee": a.employee, "amount_minor": a.amount_minor,
            "currency": a.currency, "category": a.category, "revision": m.revision}


def _types(a: Reimburse, m: FinanceMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if type(a.amount_minor) is not int or a.amount_minor <= 0:
        return invalid("AMOUNT_NOT_POSITIVE_INTEGER")
    return None


def _authority(a: Reimburse, m: FinanceMandate, c: Ctx):  # type: ignore[no-untyped-def]
    return None if m.active else deny("MANDATE_NOT_ACTIVE")


def _bounds(a: Reimburse, m: FinanceMandate, c: Ctx):  # type: ignore[no-untyped-def]
    if a.currency != m.currency:
        return deny("UNSUPPORTED_CURRENCY")
    if a.category not in m.categories:
        return deny("CATEGORY_NOT_ALLOWED")
    assert isinstance(a.amount_minor, int)
    return deny("OVER_HARD_LIMIT") if a.amount_minor > m.hard_limit_minor else None


def _approval(a: Reimburse, m: FinanceMandate, c: Ctx):  # type: ignore[no-untyped-def]
    assert isinstance(a.amount_minor, int)
    if a.amount_minor <= m.auto_limit_minor:
        return None
    code = check_binding(c.approval, action_id=a.action_id, action_hash=action_hash(envelope(a, m)),
                         principal=c.principal, authority_revision=m.revision, now=c.now)
    return approval_required(code) if code else None


GATE: Gate[Reimburse, FinanceMandate, Ctx] = Gate([
    Check("types", _types), Check("authority", _authority), Check("bounds", _bounds), Check("approval", _approval),
])


if __name__ == "__main__":
    m = FinanceMandate(revision=3, active=True, currency="EUR", auto_limit_minor=5_000,
                       hard_limit_minor=50_000, categories=frozenset({"travel", "meals"}))
    now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
    small = Reimburse("a1", "emp-7", 4_200, "EUR", "meals")
    big = Reimburse("a2", "emp-7", 18_000, "EUR", "travel")
    print("small:", GATE.evaluate(small, m, Ctx("cfo", now, None)).verdict)
    print("big, no approval:", GATE.evaluate(big, m, Ctx("cfo", now, None)).verdict)
    ok = ApprovalRecord("ap1", "a2", action_hash(envelope(big, m)), "cfo", 3, ApprovalStatus.APPROVED,
                        now + dt.timedelta(minutes=2))
    print("big, approved:", GATE.evaluate(big, m, Ctx("cfo", now, ok)).verdict)
    edited = Reimburse("a2", "emp-7", 19_000, "EUR", "travel")
    print("edited after approval:", GATE.evaluate(edited, m, Ctx("cfo", now, ok)).reason_code)
