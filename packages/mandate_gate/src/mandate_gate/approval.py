"""Exact-action approval binding.

An approval authorizes one specific action, identified by its canonical hash, under one authority revision,
for one principal, until an expiry, once. ``check_binding`` returns ``None`` when the approval authorizes the
action now, otherwise a reason code. It never upgrades a hard denial: callers run their hard checks first.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    action_id: str
    action_hash: str
    principal: str
    authority_revision: int
    status: ApprovalStatus
    expires_at: dt.datetime
    consumed_at: dt.datetime | None = None


def check_binding(
    approval: ApprovalRecord | None,
    *,
    action_id: str,
    action_hash: str,
    principal: str,
    authority_revision: int,
    now: dt.datetime,
) -> str | None:
    """Return None if ``approval`` authorizes exactly this action now; otherwise a reason code."""
    if approval is None:
        return "APPROVAL_REQUIRED"
    if approval.consumed_at is not None:
        return "APPROVAL_REQUIRED"
    if approval.status is ApprovalStatus.PENDING:
        return "APPROVAL_REQUIRED"
    if approval.status is not ApprovalStatus.APPROVED:
        return "APPROVAL_REQUIRED"
    if now >= approval.expires_at:
        return "APPROVAL_EXPIRED"
    if (
        approval.action_id != action_id
        or approval.action_hash != action_hash
        or approval.principal != principal
        or approval.authority_revision != authority_revision
    ):
        return "APPROVAL_MISMATCH"
    return None
