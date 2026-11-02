"""Deterministic buyer-risk scanner (POLICY.md section 6).

These rules run on every buyer message regardless of what the model says. They are intentionally simple
and explainable; the model can add signals but never remove them. False positives only block a
reservation for that buyer (the conversation continues), which is the safe direction for a seller.
"""

from __future__ import annotations

import re
from typing import Final

HIGH: Final = frozenset({"qr_receive_scam", "fake_payment_proof", "authority_claim"})
MEDIUM: Final = frozenset({"off_platform_payment", "shipping_request", "private_info_request"})
LOW: Final = frozenset({"off_platform_contact"})


def severity(signal: str) -> str:
    if signal in HIGH:
        return "high"
    if signal in MEDIUM:
        return "medium"
    return "low"


_I = re.IGNORECASE

_RULES: Final[list[tuple[str, re.Pattern[str]]]] = [
    (
        "qr_receive_scam",
        re.compile(
            r"(scan\b.{0,40}\b(qr|code)|qr\b.{0,40}\b(receive|get|credit|accept)|"
            r"(collect|payment)\s*request|\b(approve|accept)\b.{0,30}\brequest\b.{0,30}\b(receive|money|payment)|"
            r"link\b.{0,40}\b(receive|get)\b.{0,20}\b(money|payment)|enter\s+(your\s+)?(upi\s+)?pin)",
            _I,
        ),
    ),
    (
        "fake_payment_proof",
        re.compile(
            r"(already\s+(paid|sent|transferred)|(paid|sent|transferred)\s+(you\s+)?(the\s+)?(money|amount|payment)|"
            r"payment\s+(screenshot|proof|receipt)|screenshot\s+of\s+(the\s+)?(payment|transfer)|"
            r"check\s+your\s+(account|bank|balance)|paise\s+(bhej|daal)\s+diye|payment\s+kar\s+di)",
            _I,
        ),
    ),
    (
        "authority_claim",
        re.compile(
            r"(owner\s+(has\s+)?(approved|agreed|said|told)|seller\s+(already\s+)?(approved|agreed|told\s+me)|"
            r"i\s+am\s+the\s+(owner|seller)|ignore\s+(all\s+|your\s+|the\s+)?(previous|prior|earlier)?\s*"
            r"(instructions|rules|limits)|system\s*(prompt|message)\s*:|developer\s*:|"
            r"\b(army|military|police|cbi|customs|navy|air\s*force)\b.{0,30}\b(officer|man|posted|jawan)\b|"
            r"\bi\s+am\s+(an?\s+)?(army|police|cbi|customs)\b|fauji)",
            _I,
        ),
    ),
    (
        "off_platform_payment",
        re.compile(
            r"(\b[\w.\-]{2,}@(ok\w+|ybl|paytm|upi|ibl|axl|apl|icici|sbi|hdfc)\b|\bupi\b|paypal|venmo|zelle|"
            r"wire\s+transfer|bank\s+transfer|\badvance\b|payment\s+link|gift\s*card|crypto|bitcoin|token\s+amount)",
            _I,
        ),
    ),
    (
        "shipping_request",
        re.compile(
            r"(\bship\b|\bshipping\b|courier|deliver|delivery|\bpost\s+it\b|\bsend\s+it\b|dunzo|porter|"
            r"bhej\s+(do|dena|denge)|ghar\s+pe\s+(bhej|de))",
            _I,
        ),
    ),
    (
        "private_info_request",
        re.compile(
            r"(\baddress\b|exact\s+location|where\s+do\s+you\s+live|your\s+(phone|number|mobile|email|house|flat)|"
            r"(phone|mobile|contact)\s+number|lowest\s+(price|you\s+can)|minimum\s+price|"
            r"\bfloor\b|ghar\s+ka\s+pata|kahan\s+rehte)",
            _I,
        ),
    ),
    (
        "off_platform_contact",
        re.compile(r"(whats\s*app|telegram|\bsignal\b|call\s+me|text\s+me|my\s+number\s+is|\b\d{10}\b|\S+@\S+\.\w+)", _I),
    ),
]


def scan(text: str) -> list[str]:
    """Return the risk signals found in a buyer message, in rule order, without duplicates."""
    found: list[str] = []
    for name, pattern in _RULES:
        if pattern.search(text) and name not in found:
            found.append(name)
    return found


def has_high(signals: set[str] | frozenset[str] | list[str]) -> bool:
    return any(s in HIGH for s in signals)
