from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
from mandate_gate import (
    ApprovalRecord,
    ApprovalStatus,
    CanonicalizationError,
    Check,
    Gate,
    Verdict,
    action_hash,
    canonical_bytes,
    check_binding,
    deny,
    invalid,
)

NOW = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)


def test_canonical_sorts_keys_and_is_compact() -> None:
    assert canonical_bytes({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_canonical_preserves_unicode_exactly() -> None:
    assert canonical_bytes({"t": "bhai 520 मेंं"}) == '{"t":"bhai 520 मेंं"}'.encode()


@pytest.mark.parametrize("bad", [1.0, float("nan"), {"x": 0.5}, [1, 2.5], {1: "a"}, {"s": {1, 2}}])
def test_canonical_rejects_non_integer_numbers_and_odd_types(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_bytes(bad)


def test_hash_is_stable_and_order_independent() -> None:
    a = action_hash({"price_minor": 57500, "currency": "USD", "kind": "reserve_item"})
    b = action_hash({"kind": "reserve_item", "currency": "USD", "price_minor": 57500})
    assert a == b
    assert action_hash({"price_minor": 57501, "currency": "USD", "kind": "reserve_item"}) != a


def test_hash_known_vector() -> None:
    # sha256 of b'{"a":1}'
    assert action_hash({"a": 1}) == "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


def _gate(log: list[str]) -> Gate[int, int, int]:
    def first(a: int, m: int, c: int):  # type: ignore[no-untyped-def]
        log.append("first")
        return invalid("NEG") if a < 0 else None

    def second(a: int, m: int, c: int):  # type: ignore[no-untyped-def]
        log.append("second")
        return deny("TOO_BIG") if a > m else None

    return Gate([Check("first", first), Check("second", second)])


def test_first_decisive_check_wins_and_later_checks_do_not_run() -> None:
    log: list[str] = []
    d = _gate(log).evaluate(-5, 0, 0)
    assert (d.verdict, d.reason_code, d.decided_by, d.passed) == (Verdict.INVALID, "NEG", "first", ())
    assert log == ["first"]


def test_all_pass_is_allow_with_trace() -> None:
    log: list[str] = []
    d = _gate(log).evaluate(3, 10, 0)
    assert d.allowed and d.passed == ("first", "second") and d.reason_code is None


def test_duplicate_check_names_rejected() -> None:
    with pytest.raises(ValueError):
        Gate([Check("x", lambda a, m, c: None), Check("x", lambda a, m, c: None)])


def _approval(**over: object) -> ApprovalRecord:
    base: dict[str, object] = dict(approval_id="ap", action_id="a1", action_hash="h1", principal="owner",
                                   authority_revision=2, status=ApprovalStatus.APPROVED,
                                   expires_at=NOW + dt.timedelta(seconds=60), consumed_at=None)
    base.update(over)
    return ApprovalRecord(**base)  # type: ignore[arg-type]


def _bind(ap: ApprovalRecord | None, **over: object) -> str | None:
    kw: dict[str, object] = dict(action_id="a1", action_hash="h1", principal="owner", authority_revision=2, now=NOW)
    kw.update(over)
    return check_binding(ap, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("approval", "over", "expected"),
    [
        (None, {}, "APPROVAL_REQUIRED"),
        (_approval(), {}, None),
        (_approval(status=ApprovalStatus.PENDING), {}, "APPROVAL_REQUIRED"),
        (_approval(status=ApprovalStatus.REJECTED), {}, "APPROVAL_REQUIRED"),
        (_approval(consumed_at=NOW), {}, "APPROVAL_REQUIRED"),
        (_approval(expires_at=NOW), {}, "APPROVAL_EXPIRED"),
        (_approval(), {"action_hash": "h2"}, "APPROVAL_MISMATCH"),
        (_approval(), {"principal": "someone-else"}, "APPROVAL_MISMATCH"),
        (_approval(), {"authority_revision": 3}, "APPROVAL_MISMATCH"),
        (_approval(), {"action_id": "a2"}, "APPROVAL_MISMATCH"),
    ],
)
def test_approval_binding(approval: ApprovalRecord | None, over: dict[str, object], expected: str | None) -> None:
    assert _bind(approval, **over) == expected


def test_expense_example_runs() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "expense_gate.py"
    spec = importlib.util.spec_from_file_location("expense_gate", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["expense_gate"] = mod
    spec.loader.exec_module(mod)
    m = mod.FinanceMandate(3, True, "EUR", 5_000, 50_000, frozenset({"travel"}))
    big = mod.Reimburse("a2", "e", 18_000, "EUR", "travel")
    assert mod.GATE.evaluate(big, m, mod.Ctx("cfo", NOW, None)).verdict is Verdict.APPROVAL_REQUIRED
    assert mod.GATE.evaluate(mod.Reimburse("a3", "e", True, "EUR", "travel"), m,
                             mod.Ctx("cfo", NOW, None)).verdict is Verdict.INVALID
    assert mod.GATE.evaluate(mod.Reimburse("a4", "e", 60_000, "EUR", "travel"), m,
                             mod.Ctx("cfo", NOW, None)).reason_code == "OVER_HARD_LIMIT"
