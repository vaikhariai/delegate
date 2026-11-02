"""mandate_gate: ordered deterministic checks and exact-action approval binding for agent-proposed actions.

The scope is deliberately narrow: typed actions with structured fields, checked against an explicit
authority snapshot. It is not a general semantic-safety or prompt-injection filter.
"""

from mandate_gate.approval import ApprovalRecord, ApprovalStatus, check_binding
from mandate_gate.canonical import CANONICAL_VERSION, CanonicalizationError, action_hash, canonical_bytes
from mandate_gate.core import (
    Check,
    Decision,
    Gate,
    Outcome,
    Verdict,
    allow_with,
    approval_required,
    deny,
    invalid,
)

__all__ = [
    "CANONICAL_VERSION",
    "ApprovalRecord",
    "ApprovalStatus",
    "CanonicalizationError",
    "Check",
    "Decision",
    "Gate",
    "Outcome",
    "Verdict",
    "action_hash",
    "allow_with",
    "approval_required",
    "canonical_bytes",
    "check_binding",
    "deny",
    "invalid",
]
__version__ = "0.1.0"
