"""Property-based tests for the pure gate (POLICY.md section 4).

Random setups and random, often malformed proposals are run through the real gate. The invariants below come from the
policy text, not from the gate's code, so a violation is a real defect.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation"))

import gate_harness  # noqa: E402

from delegate import risk  # noqa: E402
from delegate.policy import evaluate  # noqa: E402
from delegate.proposal import FIELDS, ProposalInvalid, parse_proposal  # noqa: E402
from mandate_gate import action_hash  # noqa: E402

ALLOWED_FACTS = ["f_model", "f_memory", "f_storage", "f_condition", "f_battery", "f_charger", "f_pickup", "f_payment", "f_availability"]
FACTS = ALLOWED_FACTS + ["f_address", "seller_phone", "min_price", ""]
SIGNALS = ["qr_receive_scam", "fake_payment_proof", "authority_claim", "off_platform_payment", "shipping_request",
           "private_info_request", "off_platform_contact"]
PRICES = st.one_of(st.integers(-5, 120_000), st.sampled_from([0, 49_999, 50_000, 50_001, 64_999, 65_000, 65_001, 2**63]),
                   st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6), st.booleans(), st.text(max_size=4),
                   st.none())


def maybe(values: list[Any]) -> st.SearchStrategy[Any]:
    return st.one_of(st.none(), st.sampled_from(values), st.text(max_size=6))


@st.composite
def proposals(draw: st.DrawFn) -> dict[str, Any]:
    p: dict[str, Any] = {
        "kind": draw(st.sampled_from(["send_message", "reserve_item", "no_action", "send_money", ""])),
        "message_intent": draw(maybe(["answer_listing", "counter_offer", "clarify", "decline"])),
        "fact_ids": draw(st.lists(st.sampled_from(FACTS), max_size=4)),
        "price_minor": draw(PRICES),
        "currency": draw(st.one_of(st.none(), st.sampled_from(["USD", "INR", "usd"]))),
        "clarify_topic": draw(maybe(["price_amount", "currency", "pickup_time", "acceptance", "item_details"])),
        "decline_reason": draw(maybe(["price_not_acceptable", "delivery_not_offered", "qr_receive_scam", "authority_claim"])),
        "acceptance_event_id": draw(st.one_of(st.none(), st.sampled_from(["acc-1", "acc-2", "acc-x"]))),
        "language": draw(st.sampled_from(["en", "hinglish", "fr"])),
        "buyer_risk_signals": draw(st.lists(st.sampled_from(SIGNALS), max_size=2, unique=True)),
        "rationale": draw(st.one_of(st.text(max_size=20), st.none())),
    }
    if draw(st.booleans()):  # bias towards well-formed proposals so deep checks are exercised
        kind = draw(st.sampled_from(["counter", "reserve", "answer", "decline"]))
        price = draw(st.integers(40_000, 70_000))
        p.update({"currency": "USD", "language": "en", "rationale": "r", "clarify_topic": None, "decline_reason": None})
        if kind == "counter":
            p.update({"kind": "send_message", "message_intent": "counter_offer", "price_minor": price, "acceptance_event_id": None})
        elif kind == "reserve":
            p.update({"kind": "reserve_item", "message_intent": None, "price_minor": price,
                      "acceptance_event_id": draw(st.sampled_from(["acc-1", "acc-2"])), "fact_ids": []})
        elif kind == "answer":
            p.update({"kind": "send_message", "message_intent": "answer_listing", "price_minor": None, "currency": None,
                      "acceptance_event_id": None, "fact_ids": draw(st.lists(st.sampled_from(FACTS), min_size=1, max_size=3, unique=True))})
        else:
            p.update({"kind": "send_message", "message_intent": "decline", "price_minor": None, "currency": None,
                      "acceptance_event_id": None, "decline_reason": "delivery_not_offered"})
    if draw(st.integers(0, 9)) == 0:
        p["extra"] = 1
    if draw(st.integers(0, 9)) == 0:
        p.pop(draw(st.sampled_from(list(FIELDS))), None)
    return p


@st.composite
def setups(draw: st.DrawFn) -> dict[str, Any]:
    offer_price = draw(st.sampled_from([65_000, 57_500, 50_000, 49_000]))
    offers = [{"offer_id": "list", "price_minor": 65_000, "currency": "USD", "status": draw(st.sampled_from(["open", "superseded"]))},
              {"offer_id": "o2", "price_minor": offer_price, "currency": "USD", "status": draw(st.sampled_from(["open", "superseded", "accepted"]))}]
    acceptances = []
    for acc_id in ("acc-1", "acc-2"):
        if draw(st.booleans()):
            acceptances.append({"acceptance_event_id": acc_id, "offer_id": draw(st.sampled_from(["list", "o2", "o9"])),
                                "buyer_is_run_buyer": draw(st.booleans()), "used": draw(st.booleans())})
    approval = None
    if draw(st.booleans()):
        approval = {"status": draw(st.sampled_from(["approved", "pending", "rejected"])), "consumed": draw(st.booleans()),
                    "expires_in_s": draw(st.sampled_from([-1, 60])), "hash_matches": draw(st.booleans()),
                    "revision_matches": draw(st.booleans()), "owner_matches": draw(st.booleans())}
    return {
        "mandate": {"status": draw(st.sampled_from(["active", "active", "active", "paused", "revoked", "expired", "draft"])),
                    "min_price_minor": draw(st.sampled_from([50_000, 52_000])), "expires_in_s": draw(st.sampled_from([1500, -1])),
                    "max_outbound_messages": 8, "max_reservations": 1},
        "action": {"revision_offset": draw(st.sampled_from([0, 0, 1])), "expires_in_s": draw(st.sampled_from([100, -1])),
                   "session_matches": draw(st.sampled_from([True, True, False])), "run_matches": True, "item_matches": True,
                   "hold_seconds": draw(st.sampled_from([900, 900, 3600])), "delivery": draw(st.sampled_from(["pickup", "pickup", "ship"]))},
        "run": {"state": draw(st.sampled_from(["running", "awaiting_buyer", "completed", "cancelled"])), "fence_current": draw(st.sampled_from([True, True, False]))},
        "counts": {"outbound_messages": draw(st.sampled_from([0, 7, 8])), "reservations": draw(st.sampled_from([0, 0, 1]))},
        "item_reserved": draw(st.sampled_from([False, False, True])),
        "buyer_signals": draw(st.lists(st.sampled_from(SIGNALS), max_size=2, unique=True)),
        "offers": offers, "acceptances": acceptances, "committed_operation": draw(st.sampled_from([False, False, False, True])),
        "approval": approval,
    }


def run_case(setup: dict[str, Any], proposal: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    case = {"case_id": "prop", "family": "P", "title": "property", "setup": setup, "proposal": proposal,
            "expected": {"verdict": "allow", "reason_code": None}}
    env, mandate, ctx = gate_harness.build(case)
    return evaluate(env, mandate, ctx), env, mandate, ctx


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(setups(), proposals())
def test_gate_invariants(setup: dict[str, Any], proposal: dict[str, Any]) -> None:
    label = check_invariants(setup, proposal)
    if label:
        event(label)


def check_invariants(setup: dict[str, Any], proposal: dict[str, Any]) -> str | None:
    """Safety invariants from POLICY.md section 4. Returns a coverage label."""
    d, env, m, ctx = run_case(setup, proposal)
    d2, *_ = run_case(setup, proposal)
    assert (d.verdict, d.reason_code, d.decided_by) == (d2.verdict, d2.reason_code, d2.decided_by)  # deterministic

    try:
        p = parse_proposal(proposal)
        parsed = True
    except ProposalInvalid:
        parsed = False
    assert (d.verdict.value == "invalid") == (not parsed)  # invalid exactly when the proposal breaks section 2
    if not parsed:
        return None
    v = d.verdict.value
    label = f"{v}:{p.kind}:{p.message_intent}"
    if v == "approval_required":
        assert p.kind == "reserve_item"
    if v != "allow" or p.kind == "no_action":
        return label
    duplicate = d.reason_code == "DUPLICATE"
    # Every allow happens under a live, current, active mandate, the current fence and a live run, in scope.
    assert m.status == "active" and env.mandate_revision == m.revision and ctx.fence_current
    assert ctx.run_state in ("created", "running", "awaiting_buyer", "waiting_approval", "blocked")
    assert env.session_id == ctx.auth_session_id and ctx.now < env.expires_at
    assert all(f in m.allowed_public_fact_ids for f in p.fact_ids)  # never cites a non-public fact
    if p.message_intent == "counter_offer" or p.kind == "reserve_item":
        assert p.currency == "USD" and m.min_price_minor <= p.price_minor <= m.asking_price_minor  # never below the floor
    if p.kind == "reserve_item" and not duplicate:
        assert not risk.has_high(set(ctx.buyer_signals) | set(p.buyer_risk_signals))
        assert env.delivery == "pickup" and env.hold_seconds == m.reservation_hold_seconds
        acc = ctx.acceptances[p.acceptance_event_id or ""]
        offer = ctx.offers[acc.offer_id]
        assert acc.buyer_is_run_buyer and not acc.used and offer.status == "open" and offer.price_minor == p.price_minor
        assert not ctx.item_reserved and ctx.reservations < m.max_reservations
        a = ctx.approval  # an allow for a reservation needs a live approval bound to this exact action
        assert a is not None and a.status.value == "approved" and a.consumed_at is None and ctx.now < a.expires_at
        assert a.action_hash == env.hash() and a.authority_revision == m.revision and a.principal == ctx.owner_principal
    return label


@settings(max_examples=500, deadline=None)
@given(proposals(), st.randoms(use_true_random=False))
def test_action_hash_binds_content_not_key_order(proposal: dict[str, Any], rnd: random.Random) -> None:
    try:
        json.dumps(proposal, allow_nan=False)
        h1 = action_hash({"payload": {k: proposal[k] for k in proposal if not isinstance(proposal[k], float)}})
    except (ValueError, TypeError):
        return
    keys = [k for k in proposal if not isinstance(proposal[k], float)]
    rnd.shuffle(keys)
    assert action_hash({"payload": {k: proposal[k] for k in keys}}) == h1
    for k in keys:
        changed = dict({k2: proposal[k2] for k2 in keys})
        changed[k] = ["changed", changed[k]]
        assert action_hash({"payload": changed}) != h1


PERTURBATIONS = [
    ("mandate.status", "paused"), ("mandate.status", "revoked"), ("mandate.expires_in_s", -1), ("action.revision_offset", 1),
    ("action.expires_in_s", -1), ("action.session_matches", False), ("action.delivery", "ship"), ("action.hold_seconds", 3600),
    ("run.state", "cancelled"), ("run.fence_current", False), ("counts.reservations", 1), ("item_reserved", True),
    ("buyer_signals", ["qr_receive_scam"]), ("buyer_signals", ["authority_claim"]), ("offer_status", "superseded"),
    ("offer_price", 60_000), ("acc_used", True), ("acc_other_buyer", True), ("approval", None), ("approval.status", "pending"),
    ("approval.consumed", True), ("approval.expires_in_s", -1), ("approval.hash_matches", False),
    ("approval.revision_matches", False), ("approval.owner_matches", False), ("price", 49_999), ("price", 65_001),
    ("model_signal", "fake_payment_proof"), ("mandate.min_price_minor", 58_000),
]


@st.composite
def near_valid(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any]], bool]:
    kind = draw(st.sampled_from(["reserve", "counter"]))
    price = draw(st.sampled_from([50_000, 57_500, 65_000]))
    setup: dict[str, Any] = {
        "mandate": {"status": "active", "min_price_minor": 50_000, "expires_in_s": 1500},
        "action": {"revision_offset": 0, "expires_in_s": 100, "session_matches": True, "delivery": "pickup", "hold_seconds": 900},
        "run": {"state": "running", "fence_current": True}, "counts": {"outbound_messages": 0, "reservations": 0},
        "item_reserved": False, "buyer_signals": [],
        "offers": [{"offer_id": "list", "price_minor": 65_000, "currency": "USD", "status": "superseded"},
                   {"offer_id": "o2", "price_minor": price, "currency": "USD", "status": "open"}],
        "acceptances": [{"acceptance_event_id": "acc-1", "offer_id": "o2", "buyer_is_run_buyer": True, "used": False}],
        "committed_operation": False,
        "approval": {"status": "approved", "consumed": False, "expires_in_s": 60, "hash_matches": True, "revision_matches": True,
                     "owner_matches": True},
    }
    proposal: dict[str, Any] = {"kind": "reserve_item", "message_intent": None, "fact_ids": [], "price_minor": price, "currency": "USD",
                                "clarify_topic": None, "decline_reason": None, "acceptance_event_id": "acc-1", "language": "en",
                                "buyer_risk_signals": [], "rationale": "r"}
    if kind == "counter":
        proposal.update({"kind": "send_message", "message_intent": "counter_offer", "acceptance_event_id": None})
    n = draw(st.sampled_from([0, 0, 0, 1, 1, 1, 2]))  # weight towards valid worlds so allowed reservations are well covered
    flips = draw(st.lists(st.sampled_from(PERTURBATIONS), min_size=n, max_size=n, unique_by=lambda x: x[0]))
    for path, value in flips:
        if path == "offer_status":
            setup["offers"][1]["status"] = value
        elif path == "offer_price":
            setup["offers"][1]["price_minor"] = value
        elif path == "acc_used":
            setup["acceptances"][0]["used"] = True
        elif path == "acc_other_buyer":
            setup["acceptances"][0]["buyer_is_run_buyer"] = False
        elif path == "approval":
            setup["approval"] = None
        elif path == "price":
            proposal["price_minor"] = value
        elif path == "model_signal":
            proposal["buyer_risk_signals"] = [value]
        elif path.startswith("approval."):
            if setup["approval"] is not None:
                setup["approval"][path.split(".", 1)[1]] = value
        elif "." in path:
            a, b = path.split(".", 1)
            setup[a][b] = value
        else:
            setup[path] = value
    return setup, proposal, flips, kind == "reserve"


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(near_valid())
def test_gate_invariants_near_valid_world(data: tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any]], bool]) -> None:
    setup, proposal, flips, is_reserve = data
    d, env, m, ctx = run_case(setup, proposal)
    event(f"{d.verdict.value}:{'reserve' if is_reserve else 'counter'}:{len(flips)} flips")
    if not flips:
        assert d.verdict.value == "allow", (d, proposal)  # liveness: a fully valid world is allowed
    check_invariants(setup, proposal)  # same safety invariants as the random world


def _apply(price: int, kind: str, flips: list[tuple[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic version of near_valid for exhaustive sweeps."""
    setup = {
        "mandate": {"status": "active", "min_price_minor": 50_000, "expires_in_s": 1500},
        "action": {"revision_offset": 0, "expires_in_s": 100, "session_matches": True, "delivery": "pickup", "hold_seconds": 900},
        "run": {"state": "running", "fence_current": True}, "counts": {"outbound_messages": 0, "reservations": 0},
        "item_reserved": False, "buyer_signals": [],
        "offers": [{"offer_id": "list", "price_minor": 65_000, "currency": "USD", "status": "superseded"},
                   {"offer_id": "o2", "price_minor": price, "currency": "USD", "status": "open"}],
        "acceptances": [{"acceptance_event_id": "acc-1", "offer_id": "o2", "buyer_is_run_buyer": True, "used": False}],
        "committed_operation": False,
        "approval": {"status": "approved", "consumed": False, "expires_in_s": 60, "hash_matches": True, "revision_matches": True,
                     "owner_matches": True},
    }
    proposal = {"kind": "reserve_item", "message_intent": None, "fact_ids": [], "price_minor": price, "currency": "USD",
                "clarify_topic": None, "decline_reason": None, "acceptance_event_id": "acc-1", "language": "en",
                "buyer_risk_signals": [], "rationale": "r"}
    if kind == "counter":
        proposal.update({"kind": "send_message", "message_intent": "counter_offer", "acceptance_event_id": None})
    for path, value in flips:
        if path == "offer_status":
            setup["offers"][1]["status"] = value
        elif path == "offer_price":
            setup["offers"][1]["price_minor"] = value
        elif path == "acc_used":
            setup["acceptances"][0]["used"] = True
        elif path == "acc_other_buyer":
            setup["acceptances"][0]["buyer_is_run_buyer"] = False
        elif path == "approval":
            setup["approval"] = None
        elif path == "price":
            proposal["price_minor"] = value
        elif path == "model_signal":
            proposal["buyer_risk_signals"] = [value]
        elif path.startswith("approval."):
            if setup["approval"] is not None:
                setup["approval"][path.split(".", 1)[1]] = value
        elif "." in path:
            a, b = path.split(".", 1)
            setup[a][b] = value
        else:
            setup[path] = value
    return setup, proposal


def test_exhaustive_single_and_double_perturbations() -> None:
    import itertools

    checked = allowed_reservations = 0
    for price in (50_000, 57_500, 65_000):
        for kind in ("reserve", "counter"):
            setup, proposal = _apply(price, kind, [])
            d, *_ = run_case(setup, proposal)
            assert d.verdict.value == "allow", (price, kind, d)
            for flip in PERTURBATIONS:
                setup, proposal = _apply(price, kind, [flip])
                d, *_ = run_case(setup, proposal)
                check_invariants(setup, proposal)
                checked += 1
                benign = flip == ("mandate.min_price_minor", 58_000) and price >= 58_000
                if kind == "reserve" and not benign:
                    assert d.verdict.value != "allow", ("a single violated condition must block a reservation", price, flip, d)
            for a, b in itertools.combinations(PERTURBATIONS, 2):
                if a[0] == b[0]:
                    continue
                setup, proposal = _apply(price, kind, [a, b])
                d, *_ = run_case(setup, proposal)
                check_invariants(setup, proposal)
                checked += 1
                allowed_reservations += int(kind == "reserve" and d.verdict.value == "allow")
    assert checked > 2000
