"""Run the frozen deterministic gate cases against the application's gate.

The expected results come from evaluation/scenarios.json (hand-authored from docs/POLICY.md). This harness only
translates each case's Appendix A setup into the gate's snapshot types, evaluates, and compares.
Usage: uv run python evaluation/gate_harness.py [--write]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "evaluation"))

import oracle  # noqa: E402
from delegate.policy import AcceptanceSnap, ActionEnvelope, GateContext, MandateSnapshot, OfferSnap, evaluate  # noqa: E402
from delegate.proposal import ProposalInvalid, parse_proposal  # noqa: E402
from mandate_gate import ApprovalRecord, ApprovalStatus  # noqa: E402

NOW = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)
SESSION, RUN, ITEM, OWNER = "ses_case", "run_case", "laptop-1", "owner:ses_case"
REVISION = 3


def build(case: dict[str, Any]) -> tuple[ActionEnvelope, MandateSnapshot, GateContext]:
    s = oracle.effective_setup(case)
    m, a, r = s["mandate"], s["action"], s["run"]
    mandate = MandateSnapshot(
        mandate_id="man_case", status=m["status"], revision=REVISION, currency=m["currency"],
        asking_price_minor=m["asking_price_minor"], min_price_minor=m["min_price_minor"],
        allowed_actions=frozenset(m["allowed_actions"]), allowed_public_fact_ids=frozenset(m["allowed_public_fact_ids"]),
        delivery=m["delivery"], payment_terms=m["payment_terms"],
        reservation_requires_owner_approval=m["reservation_requires_owner_approval"],
        reservation_hold_seconds=m["reservation_hold_seconds"], max_reservations=m["max_reservations"],
        max_outbound_messages=m["max_outbound_messages"], expires_at=NOW + dt.timedelta(seconds=m["expires_in_s"]),
    )
    raw = case["proposal"]
    try:
        proposal, err = parse_proposal(raw), None
    except ProposalInvalid as exc:
        proposal, err = None, str(exc)
    env = ActionEnvelope(
        action_id="act_case", session_id=SESSION if a["session_matches"] else "ses_other",
        run_id=RUN if a["run_matches"] else "run_other", item_id=ITEM if a["item_matches"] else "item_other",
        buyer_id="buyer-riya", mandate_id="man_case", mandate_revision=REVISION - a["revision_offset"],
        created_at=NOW - dt.timedelta(seconds=10), expires_at=NOW + dt.timedelta(seconds=a["expires_in_s"]),
        proposal=proposal, raw_proposal=raw, validation_error=err,
        delivery=a["delivery"], payment_terms=a["payment_terms"], hold_seconds=a["hold_seconds"],
    )
    ap = s.get("approval")
    approval = None
    if ap is not None:
        approval = ApprovalRecord(
            approval_id="apr_case", action_id="act_case",
            action_hash=env.hash() if ap["hash_matches"] and proposal is not None else "0" * 64,
            principal=OWNER if ap["owner_matches"] else "owner:someone-else",
            authority_revision=REVISION if ap["revision_matches"] else REVISION - 1,
            status=ApprovalStatus(ap["status"]), expires_at=NOW + dt.timedelta(seconds=ap["expires_in_s"]),
            consumed_at=NOW - dt.timedelta(seconds=5) if ap["consumed"] else None,
        )
    ctx = GateContext(
        now=NOW, auth_session_id=SESSION, run_id=RUN, item_id=ITEM, owner_principal=OWNER,
        run_state=r["state"], fence_current=r["fence_current"],
        outbound_messages=s["counts"]["outbound_messages"], reservations=s["counts"]["reservations"],
        item_reserved=s["item_reserved"], buyer_signals=frozenset(s["buyer_signals"]),
        offers={o["offer_id"]: OfferSnap(o["offer_id"], o["price_minor"], o["currency"], o["status"]) for o in s["offers"]},
        acceptances={x["acceptance_event_id"]: AcceptanceSnap(x["acceptance_event_id"], x["offer_id"], x["buyer_is_run_buyer"], x["used"])
                     for x in s["acceptances"]},
        committed_operation=s["committed_operation"], approval=approval,
    )
    return env, mandate, ctx


def run_all() -> list[dict[str, Any]]:
    out = []
    for case in oracle.load_gate_cases():
        env, mandate, ctx = build(case)
        d = evaluate(env, mandate, ctx)
        exp = case["expected"]
        actual = {"verdict": d.verdict.value, "reason_code": d.reason_code}
        out.append({
            "case_id": case["case_id"], "family": case["family"], "title": case["title"],
            "evidence_mode": "SYNTHETIC_FIXTURE", "expected": exp, "actual": actual, "decided_by": d.decided_by,
            "pass": actual["verdict"] == exp["verdict"] and actual["reason_code"] == exp["reason_code"],
        })
    return out


if __name__ == "__main__":
    results = run_all()
    fails = [r for r in results if not r["pass"]]
    for r in fails:
        print(f"FAIL {r['case_id']} {r['title']}: expected {r['expected']} got {r['actual']} ({r['decided_by']})")
    print(f"gate cases: {len(results) - len(fails)}/{len(results)} pass")
    if "--write" in sys.argv:
        path = ROOT / "evaluation" / "results" / "gate_cases.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    sys.exit(1 if fails else 0)
