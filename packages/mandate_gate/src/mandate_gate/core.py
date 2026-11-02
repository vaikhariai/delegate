"""Ordered, deterministic gate over typed actions.

A gate is an ordered list of named checks. Each check returns ``None`` (pass) or an ``Outcome`` (decisive).
The first decisive outcome is the decision; later checks do not run. If every check passes, the verdict is
``ALLOW``. The gate never calls a model and holds no state: callers pass a fresh snapshot of authority
(the mandate) and of the world (the context) at decision time, and must re-run the gate inside the same
transaction that performs the effect.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar


class Verdict(enum.StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"
    INVALID = "invalid"


@dataclass(frozen=True)
class Outcome:
    """A decisive result from one check."""

    verdict: Verdict
    reason_code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("a decisive outcome needs a reason code")


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason_code: str | None
    decided_by: str | None
    passed: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


A = TypeVar("A")  # action
M = TypeVar("M")  # mandate / authority snapshot
C = TypeVar("C")  # world context snapshot

CheckFn = Callable[[A, M, C], Outcome | None]


@dataclass(frozen=True)
class Check(Generic[A, M, C]):
    name: str
    fn: Callable[[A, M, C], Outcome | None]


class Gate(Generic[A, M, C]):
    """Evaluate checks in order; the first decisive outcome wins."""

    def __init__(self, checks: Sequence[Check[A, M, C]]) -> None:
        names = [c.name for c in checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must be unique")
        self._checks = tuple(checks)

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._checks)

    def evaluate(self, action: A, mandate: M, context: C) -> Decision:
        passed: list[str] = []
        for check in self._checks:
            outcome = check.fn(action, mandate, context)
            if outcome is not None:
                return Decision(outcome.verdict, outcome.reason_code, check.name, tuple(passed), outcome.detail)
            passed.append(check.name)
        return Decision(Verdict.ALLOW, None, None, tuple(passed))


def deny(code: str, detail: str = "") -> Outcome:
    return Outcome(Verdict.DENY, code, detail)


def invalid(code: str, detail: str = "") -> Outcome:
    return Outcome(Verdict.INVALID, code, detail)


def approval_required(code: str = "APPROVAL_REQUIRED", detail: str = "") -> Outcome:
    return Outcome(Verdict.APPROVAL_REQUIRED, code, detail)


def allow_with(code: str, detail: str = "") -> Outcome:
    """An early, decisive allow (for example, returning an already committed result)."""
    return Outcome(Verdict.ALLOW, code, detail)
