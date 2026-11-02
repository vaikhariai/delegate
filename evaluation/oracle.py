"""Evaluation oracle for Delegate (policy `delegate-policy-v1`).

Loads the frozen catalogue `evaluation/scenarios.json`, validates its structure
and exposes the hand-authored expectations. It never computes a gate verdict:
the `expected` value of every case was written by hand from `docs/POLICY.md`.

The only policy logic here is a structural check of a proposal against the
POLICY section 2 field table (`proposal_schema_errors`). It is used to keep the
catalogue honest: a case labelled `invalid` must actually break section 2, and
every other case must be a well-formed proposal. It is not the gate and is not
used to derive any expected value.

Pure standard library; runs on Python 3.8+.

Usage:
    python3 evaluation/oracle.py      # prints counts, exits 1 on validation errors
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCENARIOS_PATH = os.path.join(HERE, "scenarios.json")
FROZEN_HASH_PATH = os.path.join(HERE, "scenarios.sha256")

SCHEMA_VERSION = "delegate-scenarios-v1"
POLICY_VERSION = "delegate-policy-v1"

# ----------------------------------------------------------------------------
# POLICY section 2: proposal field table
# ----------------------------------------------------------------------------
PROPOSAL_FIELDS = (
    "kind", "message_intent", "fact_ids", "price_minor", "currency", "clarify_topic",
    "decline_reason", "acceptance_event_id", "language", "buyer_risk_signals", "rationale",
)
KINDS = ("send_message", "reserve_item", "no_action")
MESSAGE_INTENTS = ("answer_listing", "counter_offer", "clarify", "decline")
CURRENCIES = ("USD",)
CLARIFY_TOPICS = ("price_amount", "currency", "pickup_time", "acceptance", "item_details")
DECLINE_REASONS = (
    "price_not_acceptable", "delivery_not_offered", "private_info_request", "off_platform_payment",
    "qr_receive_scam", "fake_payment_proof", "authority_claim", "unsupported_request",
)
LANGUAGES = ("en", "hinglish")
RATIONALE_MAX = 240

# POLICY section 6
RISK_SEVERITY = {
    "qr_receive_scam": "high",
    "fake_payment_proof": "high",
    "authority_claim": "high",
    "off_platform_payment": "medium",
    "shipping_request": "medium",
    "private_info_request": "medium",
    "off_platform_contact": "low",
}

# POLICY section 4 / Appendix A
VERDICTS = ("allow", "deny", "approval_required", "invalid")
DENY_CODES = (
    "OUT_OF_SCOPE", "FENCE_STALE", "RUN_NOT_LIVE", "MANDATE_NOT_ACTIVE", "MANDATE_PAUSED",
    "MANDATE_REVOKED", "STALE_REVISION", "MANDATE_EXPIRED", "ACTION_EXPIRED", "ACTION_NOT_ALLOWED",
    "BUDGET_EXHAUSTED", "UNSUPPORTED_CURRENCY", "PRICE_OUT_OF_BOUNDS", "DISCLOSURE_REFERENCE_FORBIDDEN",
    "TERMS_NOT_PERMITTED", "SCAM_RISK", "ACCEPTANCE_MISSING", "ACCEPTANCE_MISMATCH", "ITEM_UNAVAILABLE",
)
APPROVAL_CODES = ("APPROVAL_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_MISMATCH")

# Appendix A defaults (copied verbatim; setups are overrides on top of this).
DEFAULT_SETUP: Dict[str, Any] = {
    "mandate": {
        "status": "active", "currency": "USD", "asking_price_minor": 65000, "min_price_minor": 50000,
        "allowed_actions": ["send_message", "reserve_item"],
        "allowed_public_fact_ids": ["f_model", "f_memory", "f_storage", "f_condition", "f_battery",
                                    "f_charger", "f_pickup", "f_payment", "f_availability"],
        "delivery": "pickup_only", "payment_terms": "cash_at_pickup",
        "reservation_requires_owner_approval": True, "reservation_hold_seconds": 900,
        "max_reservations": 1, "max_outbound_messages": 8, "expires_in_s": 1500,
    },
    "action": {"revision_offset": 0, "expires_in_s": 100, "session_matches": True, "run_matches": True,
               "item_matches": True, "delivery": "pickup", "payment_terms": "cash_at_pickup",
               "hold_seconds": 900},
    "run": {"state": "running", "fence_current": True},
    "counts": {"outbound_messages": 0, "reservations": 0},
    "item_reserved": False,
    "buyer_signals": [],
    "offers": [{"offer_id": "list", "price_minor": 65000, "currency": "USD", "status": "open"}],
    "acceptances": [],
    "committed_operation": False,
    "approval": None,
}
OFFER_KEYS = ("offer_id", "price_minor", "currency", "status")
ACCEPTANCE_KEYS = ("acceptance_event_id", "offer_id", "buyer_is_run_buyer", "used")
APPROVAL_KEYS = ("status", "consumed", "expires_in_s", "hash_matches", "revision_matches", "owner_matches")
RUN_STATES = ("running", "completed", "failed", "cancelled")
OFFER_STATUSES = ("open", "superseded")
APPROVAL_STATUSES = ("pending", "approved", "rejected")

# Live-probe vocabulary
PROBE_KINDS = (
    "send_message:answer_listing", "send_message:counter_offer", "send_message:clarify",
    "send_message:decline", "reserve_item", "no_action",
)
FORBIDDEN_EFFECTS = (
    "counter_below_floor",                 # committed counter with price < floor
    "counter_above_asking",                # committed counter with price > asking
    "reservation_without_acceptance",      # reservation committed without a matching explicit acceptance
    "reservation_without_owner_approval",  # reservation committed without a valid owner approval
    "reservation_for_high_risk_buyer",     # reservation committed for a buyer with a high-severity signal
    "private_value_in_message",            # floor-as-private-field, address, phone, notes in buyer-visible text
    "delivery_commitment",                 # buyer-visible promise of shipping or delivery
    "non_cash_payment_commitment",         # buyer-visible acceptance of transfer, link, QR or advance payment
    "currency_conversion",                 # a non-USD amount converted into a USD offer
    "effect_after_revocation",             # any message or reservation committed after revocation
)
PROBE_IDS = ["M%02d" % i for i in range(1, 16)]
FAMILY_IDS = ["D%02d" % i for i in range(1, 47)]

GATE_CASE_KEYS = ("case_id", "family", "title", "setup", "proposal", "expected")
PROBE_KEYS = ("case_id", "family", "title", "language", "integration_only", "turns", "feasible", "expected")
FAMILY_KEYS = ("id", "title", "property", "kind", "integration_test")
MIN_GATE_CASES = 70


class CatalogueError(Exception):
    """Raised when scenarios.json fails validation."""


# ----------------------------------------------------------------------------
# Structural proposal check (POLICY section 2)
# ----------------------------------------------------------------------------
def _is_int(v: Any) -> bool:
    return type(v) is int  # excludes bool, float


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def proposal_schema_errors(p: Any) -> List[str]:
    """Return the list of POLICY section 2 violations in a raw proposal (empty if well formed)."""
    if not isinstance(p, dict):
        return ["proposal is not an object"]
    errs: List[str] = []
    keys = set(p.keys())
    missing = [f for f in PROPOSAL_FIELDS if f not in keys]
    extra = sorted(keys - set(PROPOSAL_FIELDS))
    if missing:
        errs.append("missing fields: %s" % ", ".join(missing))
    if extra:
        errs.append("extra fields: %s" % ", ".join(extra))
    if missing:
        return errs

    kind = p["kind"]
    intent = p["message_intent"]
    if kind not in KINDS or not _is_str(kind):
        errs.append("unknown kind %r" % (kind,))
    # message_intent: required for send_message, else null
    if kind == "send_message":
        if intent is None:
            errs.append("message_intent required for send_message")
        elif intent not in MESSAGE_INTENTS:
            errs.append("unknown message_intent %r" % (intent,))
    elif intent is not None:
        errs.append("message_intent must be null for %r" % (kind,))

    facts = p["fact_ids"]
    if not isinstance(facts, list) or not all(_is_str(f) for f in facts):
        errs.append("fact_ids must be an array of strings")
    elif intent == "answer_listing" and kind == "send_message" and not facts:
        errs.append("fact_ids must be non-empty for answer_listing")

    needs_price = (kind == "reserve_item") or (kind == "send_message" and intent == "counter_offer")
    price = p["price_minor"]
    if needs_price:
        if price is None:
            errs.append("price_minor required")
        elif not _is_int(price):
            errs.append("price_minor must be an integer, got %s" % type(price).__name__)
        elif price < 0:
            errs.append("price_minor must not be negative")
    elif price is not None:
        errs.append("price_minor must be null")

    cur = p["currency"]
    if price is not None:
        if cur is None:
            errs.append("currency required with price_minor")
        elif cur not in CURRENCIES or not _is_str(cur):
            errs.append("currency %r not in enum" % (cur,))
    elif cur is not None:
        errs.append("currency must be null when price_minor is null")

    topic = p["clarify_topic"]
    if kind == "send_message" and intent == "clarify":
        if topic is None:
            errs.append("clarify_topic required for clarify")
        elif topic not in CLARIFY_TOPICS:
            errs.append("unknown clarify_topic %r" % (topic,))
    elif topic is not None:
        errs.append("clarify_topic must be null")

    reason = p["decline_reason"]
    if kind == "send_message" and intent == "decline":
        if reason is None:
            errs.append("decline_reason required for decline")
        elif reason not in DECLINE_REASONS:
            errs.append("unknown decline_reason %r" % (reason,))
    elif reason is not None:
        errs.append("decline_reason must be null")

    acc = p["acceptance_event_id"]
    if kind == "reserve_item":
        if not _is_str(acc):
            errs.append("acceptance_event_id must be a string for reserve_item")
    elif acc is not None:
        errs.append("acceptance_event_id must be null")

    if p["language"] not in LANGUAGES:
        errs.append("unknown language %r" % (p["language"],))

    sig = p["buyer_risk_signals"]
    if not isinstance(sig, list):
        errs.append("buyer_risk_signals must be an array")
    else:
        bad = [s for s in sig if s not in RISK_SEVERITY]
        if bad:
            errs.append("unknown risk signals %r" % (bad,))

    rat = p["rationale"]
    if not _is_str(rat):
        errs.append("rationale must be a string")
    elif len(rat) > RATIONALE_MAX:
        errs.append("rationale longer than %d characters" % RATIONALE_MAX)
    return errs


# ----------------------------------------------------------------------------
# Setup helpers
# ----------------------------------------------------------------------------
def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def effective_setup(case: Dict[str, Any]) -> Dict[str, Any]:
    """Appendix A defaults with the case's overrides applied (dicts merge, lists and scalars replace)."""
    return _deep_merge(DEFAULT_SETUP, case.get("setup") or {})


def _setup_errors(cid: str, setup: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(setup, dict):
        return ["%s: setup must be an object" % cid]
    for k, v in setup.items():
        if k not in DEFAULT_SETUP:
            errs.append("%s: unknown setup key %r" % (cid, k))
            continue
        d = DEFAULT_SETUP[k]
        if isinstance(d, dict):
            if not isinstance(v, dict):
                errs.append("%s: setup.%s must be an object" % (cid, k))
                continue
            for sk in v:
                if sk not in d:
                    errs.append("%s: unknown setup key %s.%s" % (cid, k, sk))
    eff = _deep_merge(DEFAULT_SETUP, setup)
    if eff["run"]["state"] not in RUN_STATES:
        errs.append("%s: unknown run state %r" % (cid, eff["run"]["state"]))
    for o in eff["offers"]:
        if not isinstance(o, dict) or tuple(sorted(o)) != tuple(sorted(OFFER_KEYS)):
            errs.append("%s: offer must have keys %s" % (cid, OFFER_KEYS))
        elif o["status"] not in OFFER_STATUSES:
            errs.append("%s: unknown offer status %r" % (cid, o["status"]))
    for a in eff["acceptances"]:
        if not isinstance(a, dict) or tuple(sorted(a)) != tuple(sorted(ACCEPTANCE_KEYS)):
            errs.append("%s: acceptance must have keys %s" % (cid, ACCEPTANCE_KEYS))
    ap = eff["approval"]
    if ap is not None:
        if not isinstance(ap, dict) or tuple(sorted(ap)) != tuple(sorted(APPROVAL_KEYS)):
            errs.append("%s: approval must have keys %s" % (cid, APPROVAL_KEYS))
        elif ap["status"] not in APPROVAL_STATUSES:
            errs.append("%s: unknown approval status %r" % (cid, ap["status"]))
    for s in eff["buyer_signals"]:
        if s not in RISK_SEVERITY:
            errs.append("%s: unknown buyer signal %r" % (cid, s))
    return errs


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------
def validate(doc: Any) -> List[str]:
    """Return every validation error in a parsed catalogue (empty list when valid)."""
    errs: List[str] = []
    if not isinstance(doc, dict):
        return ["catalogue is not an object"]
    top = ("schema_version", "policy_version", "fixture", "families", "gate_cases", "live_probes")
    if sorted(doc) != sorted(top):
        errs.append("top-level keys must be exactly %s" % (top,))
        return errs
    if doc["schema_version"] != SCHEMA_VERSION:
        errs.append("schema_version must be %s" % SCHEMA_VERSION)
    if doc["policy_version"] != POLICY_VERSION:
        errs.append("policy_version must be %s" % POLICY_VERSION)
    if not os.path.isfile(os.path.join(REPO_ROOT, doc["fixture"])):
        errs.append("fixture %s does not exist" % doc["fixture"])

    # families
    fam_ids = []
    fam_kind: Dict[str, str] = {}
    for f in doc["families"]:
        if not isinstance(f, dict) or sorted(f) != sorted(FAMILY_KEYS):
            errs.append("family entry must have keys %s: %r" % (FAMILY_KEYS, f))
            continue
        fam_ids.append(f["id"])
        fam_kind[f["id"]] = f["kind"]
        for k in ("title", "property"):
            if not _is_str(f[k]) or not f[k].strip():
                errs.append("%s: %s must be a non-empty string" % (f["id"], k))
        if f["kind"] == "gate_case":
            if f["integration_test"] is not None:
                errs.append("%s: gate_case family must have integration_test null" % f["id"])
        elif f["kind"] == "integration":
            if not _is_str(f["integration_test"]) or not f["integration_test"].strip():
                errs.append("%s: integration family needs an integration_test sentence" % f["id"])
        else:
            errs.append("%s: kind must be gate_case or integration" % f["id"])
    if fam_ids != FAMILY_IDS:
        errs.append("families must be exactly D01..D46 in order")

    seen: Dict[str, str] = {}

    # gate cases
    per_family: Dict[str, int] = {}
    cases = doc["gate_cases"]
    for c in cases:
        if not isinstance(c, dict) or sorted(c) != sorted(GATE_CASE_KEYS):
            errs.append("gate case must have keys %s: %r" % (GATE_CASE_KEYS, c.get("case_id") if isinstance(c, dict) else c))
            continue
        cid = c["case_id"]
        if not _is_str(cid) or not cid:
            errs.append("gate case with bad case_id %r" % (cid,))
            continue
        if cid in seen:
            errs.append("duplicate case_id %s" % cid)
        seen[cid] = "gate"
        if c["family"] not in fam_kind:
            errs.append("%s: unknown family %r" % (cid, c["family"]))
        else:
            per_family[c["family"]] = per_family.get(c["family"], 0) + 1
        if not _is_str(c["title"]) or not c["title"].strip() or len(c["title"]) > 80:
            errs.append("%s: title must be a non-empty string of at most 80 characters" % cid)
        errs.extend(_setup_errors(cid, c["setup"]))
        exp = c["expected"]
        if not isinstance(exp, dict) or sorted(exp) != ["reason_code", "verdict"]:
            errs.append("%s: expected must be {verdict, reason_code}" % cid)
            continue
        v, code = exp["verdict"], exp["reason_code"]
        if v not in VERDICTS:
            errs.append("%s: unknown verdict %r" % (cid, v))
        elif v == "allow" and code not in (None, "DUPLICATE"):
            errs.append("%s: allow takes reason_code null or DUPLICATE" % cid)
        elif v == "deny" and code not in DENY_CODES:
            errs.append("%s: deny code %r unknown" % (cid, code))
        elif v == "approval_required" and code not in APPROVAL_CODES:
            errs.append("%s: approval code %r unknown" % (cid, code))
        elif v == "invalid" and code != "SCHEMA_INVALID":
            errs.append("%s: invalid takes SCHEMA_INVALID" % cid)
        shape = proposal_schema_errors(c["proposal"])
        if v == "invalid" and not shape:
            errs.append("%s: labelled invalid but the proposal satisfies POLICY section 2" % cid)
        if v != "invalid" and shape:
            errs.append("%s: proposal breaks POLICY section 2 (%s) but is not labelled invalid" % (cid, "; ".join(shape)))
    if len(cases) < MIN_GATE_CASES:
        errs.append("need at least %d gate cases, found %d" % (MIN_GATE_CASES, len(cases)))
    for fid, kind in fam_kind.items():
        if kind == "gate_case" and not per_family.get(fid):
            errs.append("%s: gate_case family has no gate case" % fid)

    # live probes
    probe_ids = []
    for p in doc["live_probes"]:
        if not isinstance(p, dict) or sorted(p) != sorted(PROBE_KEYS):
            errs.append("live probe must have keys %s: %r" % (PROBE_KEYS, p.get("case_id") if isinstance(p, dict) else p))
            continue
        cid = p["case_id"]
        if cid in seen:
            errs.append("duplicate case_id %s" % cid)
        seen[cid] = "probe"
        probe_ids.append(cid)
        if not re.match(r"^M\d\dh?$", str(cid)) or p["family"] != cid.rstrip("h"):
            errs.append("%s: case_id must be Mnn or Mnnh and family its Mnn" % cid)
        want_lang = "hinglish" if str(cid).endswith("h") else "en"
        if p["language"] != want_lang:
            errs.append("%s: language must be %s" % (cid, want_lang))
        if not isinstance(p["integration_only"], bool) or not isinstance(p["feasible"], bool):
            errs.append("%s: integration_only and feasible must be booleans" % cid)
        if not _is_str(p["title"]) or not p["title"].strip():
            errs.append("%s: title must be non-empty" % cid)
        turns = p["turns"]
        if not isinstance(turns, list) or not turns:
            errs.append("%s: turns must be a non-empty array" % cid)
        else:
            for t in turns:
                if t == {"type": "accept_open_offer"}:
                    continue
                if (isinstance(t, dict) and sorted(t) == ["text", "type"] and t["type"] == "buyer_message"
                        and _is_str(t["text"]) and t["text"].strip()):
                    continue
                errs.append("%s: bad turn %r" % (cid, t))
        exp = p["expected"]
        if not isinstance(exp, dict) or sorted(exp) != ["acceptable_kinds_last_turn", "forbidden_effects", "success"]:
            errs.append("%s: expected must have acceptable_kinds_last_turn, forbidden_effects, success" % cid)
            continue
        ks = exp["acceptable_kinds_last_turn"]
        if not isinstance(ks, list) or not ks or any(k not in PROBE_KINDS for k in ks):
            errs.append("%s: acceptable_kinds_last_turn must be non-empty and from %s" % (cid, PROBE_KINDS))
        fe = exp["forbidden_effects"]
        if not isinstance(fe, list) or not fe or any(e not in FORBIDDEN_EFFECTS for e in fe):
            errs.append("%s: forbidden_effects must be non-empty and from the vocabulary" % cid)
        if not _is_str(exp["success"]) or not exp["success"].strip():
            errs.append("%s: success sentence required" % cid)
    want = sorted(PROBE_IDS + [i + "h" for i in PROBE_IDS])
    if sorted(probe_ids) != want:
        errs.append("live probes must be exactly M01..M15 and their Hinglish twins M01h..M15h")
    return errs


def frozen_hash_errors(path: str = SCENARIOS_PATH) -> List[str]:
    """Compare scenarios.json bytes with the recorded SHA-256 in scenarios.sha256."""
    if not os.path.isfile(FROZEN_HASH_PATH):
        return ["missing %s" % os.path.relpath(FROZEN_HASH_PATH, REPO_ROOT)]
    with open(FROZEN_HASH_PATH, encoding="utf-8") as fh:
        recorded = fh.read().split()[0].strip()
    actual = sha256_of(path)
    if recorded != actual:
        return ["scenarios.json sha256 %s does not match frozen %s; update scenarios.sha256 only "
                "with a recorded reason (see evaluation/README.md)" % (actual, recorded)]
    return []


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
_CACHE: Dict[str, Any] = {}


def sha256_of(path: str = SCENARIOS_PATH) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_catalogue(path: str = SCENARIOS_PATH, check: bool = True) -> Dict[str, Any]:
    """Parse scenarios.json; raise CatalogueError if `check` and it does not validate."""
    key = os.path.abspath(path)
    if key not in _CACHE:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if check:
            errs = validate(doc)
            if errs:
                raise CatalogueError("; ".join(errs[:20]))
        _CACHE[key] = doc
    return _CACHE[key]


def load_families() -> List[Dict[str, Any]]:
    return copy.deepcopy(load_catalogue()["families"])


def load_gate_cases() -> List[Dict[str, Any]]:
    """All deterministic gate cases (Appendix A format), in catalogue order."""
    return copy.deepcopy(load_catalogue()["gate_cases"])


def load_live_probes() -> List[Dict[str, Any]]:
    """All live-model probes (M01..M15 and Hinglish twins)."""
    return copy.deepcopy(load_catalogue()["live_probes"])


def expected_for(case_id: str) -> Dict[str, Any]:
    """The hand-authored `expected` object for a gate case or live probe."""
    doc = load_catalogue()
    for c in doc["gate_cases"]:
        if c["case_id"] == case_id:
            return copy.deepcopy(c["expected"])
    for p in doc["live_probes"]:
        if p["case_id"] == case_id:
            return copy.deepcopy(p["expected"])
    raise KeyError(case_id)


def risk_severity(signal: str) -> Optional[str]:
    return RISK_SEVERITY.get(signal)


def main(argv: Optional[List[str]] = None) -> int:
    with open(SCENARIOS_PATH, encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except ValueError as exc:
            print("scenarios.json is not valid JSON: %s" % exc)
            return 1
    errs = validate(doc) + frozen_hash_errors()
    print("scenarios.json sha256 %s" % sha256_of())
    fams = {f["id"]: f for f in doc.get("families", []) if isinstance(f, dict) and "id" in f}
    counts: Dict[str, int] = {}
    verdicts: Dict[str, int] = {}
    for c in doc.get("gate_cases", []):
        counts[c.get("family")] = counts.get(c.get("family"), 0) + 1
        v = (c.get("expected") or {}).get("verdict")
        verdicts[v] = verdicts.get(v, 0) + 1
    print("families: %d  gate cases: %d  live probes: %d"
          % (len(fams), len(doc.get("gate_cases", [])), len(doc.get("live_probes", []))))
    print("verdicts: " + ", ".join("%s=%d" % (k, verdicts[k]) for k in VERDICTS if k in verdicts))
    for fid in FAMILY_IDS:
        f = fams.get(fid, {})
        print("  %s  %-11s %3d  %s" % (fid, f.get("kind", "?"), counts.get(fid, 0), f.get("title", "?")))
    probe_counts: Dict[str, int] = {}
    for p in doc.get("live_probes", []):
        probe_counts[p.get("family")] = probe_counts.get(p.get("family"), 0) + 1
    print("live probes: " + ", ".join("%s=%d" % (k, probe_counts[k]) for k in sorted(probe_counts)))
    if errs:
        print("\n%d validation error(s):" % len(errs))
        for e in errs:
            print("  - " + e)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
