"""The support-refund example: the same gate pattern in a second domain (package example, no product behind it)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from mandate_gate import ApprovalRecord, ApprovalStatus, action_hash

_path = Path(__file__).resolve().parents[1] / "examples" / "support_refund_gate.py"
_spec = importlib.util.spec_from_file_location("support_refund_gate", _path)
assert _spec and _spec.loader
ex: Any = importlib.util.module_from_spec(_spec)
sys.modules["support_refund_gate"] = ex
_spec.loader.exec_module(ex)

NOW = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
M = ex.SupportMandate(revision=2, active=True, currency="USD", auto_limit_minor=2_500, hard_limit_minor=20_000)
ORDERS = {"o-1": ("cust-1", 8_000), "o-2": ("cust-9", 9_000), "o-3": ("cust-1", 50_000)}


def ctx(approval: ApprovalRecord | None = None, refunded: frozenset[str] = frozenset()) -> Any:
    return ex.Ctx("cust-1", ORDERS, refunded, "lead-4", NOW, approval)


def approve(p: Any, **over: Any) -> ApprovalRecord:
    base = dict(approval_id="ap", action_id=p.action_id, action_hash=action_hash(ex.envelope(p, M, ctx())), principal="lead-4",
                authority_revision=M.revision, status=ApprovalStatus.APPROVED, expires_at=NOW + dt.timedelta(minutes=5))
    return ApprovalRecord(**{**base, **over})


@pytest.mark.parametrize("proposal, verdict, code, decided_by", [
    (ex.Proposal("a", "refund", "o-1", 2_500, "late"), "allow", None, None),
    (ex.Proposal("a", "refund", "o-1", 2_501, "late"), "approval_required", "APPROVAL_REQUIRED", "approval"),
    (ex.Proposal("a", "refund", "o-1", 8_001, "late"), "deny", "OVER_ORDER_TOTAL", "bounds"),
    (ex.Proposal("a", "refund", "o-3", 20_001, "damaged"), "deny", "OVER_HARD_LIMIT", "bounds"),
    (ex.Proposal("a", "refund", "o-2", 100, "late"), "deny", "ORDER_NOT_THIS_CUSTOMERS", "scope"),
    (ex.Proposal("a", "refund", "o-404", 100, "late"), "deny", "ORDER_NOT_THIS_CUSTOMERS", "scope"),
    (ex.Proposal("a", "refund", "o-1", 25.0, "late"), "invalid", "AMOUNT_NOT_POSITIVE_INTEGER", "types"),
    (ex.Proposal("a", "refund", "o-1", True, "late"), "invalid", "AMOUNT_NOT_POSITIVE_INTEGER", "types"),
    (ex.Proposal("a", "refund", "o-1", 100, "goodwill"), "invalid", "UNKNOWN_REASON", "types"),
    (ex.Proposal("a", "close_account"), "invalid", "UNKNOWN_KIND", "types"),
    (ex.Proposal("a", "reply", template="refund_issued"), "allow", None, None),
    (ex.Proposal("a", "reply", template="Our auto-approve limit is $25, ask for $24"), "invalid", "UNKNOWN_TEMPLATE", "types"),
])
def test_decisions(proposal: Any, verdict: str, code: str | None, decided_by: str | None) -> None:
    d = ex.GATE.evaluate(proposal, M, ctx())
    assert (d.verdict.value, d.reason_code, d.decided_by) == (verdict, code, decided_by)


def test_one_refund_per_order() -> None:
    d = ex.GATE.evaluate(ex.Proposal("a", "refund", "o-1", 100, "late"), M, ctx(refunded=frozenset({"o-1"})))
    assert d.reason_code == "ALREADY_REFUNDED"


def test_manager_approval_covers_exactly_one_refund() -> None:
    big = ex.Proposal("r2", "refund", "o-1", 6_000, "damaged")
    ok = approve(big)
    assert ex.GATE.evaluate(big, M, ctx(ok)).verdict.value == "allow"
    # any change after approval needs a new one
    for changed in (dataclasses.replace(big, amount_minor=6_500), dataclasses.replace(big, reason="late"),
                    dataclasses.replace(big, action_id="r3")):
        assert ex.GATE.evaluate(changed, M, ctx(ok)).verdict.value == "approval_required"
    assert ex.GATE.evaluate(big, dataclasses.replace(M, revision=3), ctx(ok)).verdict.value == "approval_required"
    assert ex.GATE.evaluate(big, M, ctx(approve(big, principal="agent"))).verdict.value == "approval_required"
    expired = approve(big, expires_at=NOW - dt.timedelta(seconds=1))
    assert ex.GATE.evaluate(big, M, ctx(expired)).reason_code == "APPROVAL_EXPIRED"


def test_approval_never_overrides_a_hard_denial() -> None:
    huge = ex.Proposal("r9", "refund", "o-3", 30_000, "damaged")
    assert ex.GATE.evaluate(huge, M, ctx(approve(huge))).reason_code == "OVER_HARD_LIMIT"
    assert ex.GATE.evaluate(huge, dataclasses.replace(M, active=False), ctx(approve(huge))).reason_code == "MANDATE_NOT_ACTIVE"


def test_customer_text_comes_only_from_templates() -> None:
    text = ex.reply_text(ex.Proposal("a", "reply", template="cannot_refund"))
    assert text == ex.TEMPLATES["cannot_refund"]
    assert not any(str(v) in " ".join(ex.TEMPLATES.values()) for v in ("2500", "2,500", "$25", "20000", "$200"))
