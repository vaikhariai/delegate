"""Gate lab: replay the frozen deterministic gate cases and evaluate edited proposals against a case's setup.

Pure computation over `evaluation/scenarios.json` (hand-authored, frozen) and the real gate in `policy.py`. No database,
no model, no side effects. Expected values come from the catalogue; actual values from the gate.
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from delegate.policy import GATE, evaluate

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "evaluation"
CHECK_LABELS = {
    "1_schema": "Proposal format",
    "2_scope": "Session, run and item scope",
    "3_fence_and_run": "Worker fence and live run",
    "4_authority": "Mandate active, current and unexpired",
    "5_allowed_and_budget": "Action allowed and within budget",
    "6_money": "Currency and price bounds",
    "7_terms_disclosure_risk": "Public facts, pickup terms, scam risk",
    "8_acceptance": "Explicit buyer acceptance",
    "9_duplicate": "Already done (idempotency)",
    "10_approval": "Owner approval of this exact action",
}


@lru_cache(maxsize=1)
def _harness() -> ModuleType:
    if str(EVAL) not in sys.path:
        sys.path.insert(0, str(EVAL))
    spec = importlib.util.spec_from_file_location("delegate_gate_harness", EVAL / "gate_harness.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=1)
def _cases() -> dict[str, dict[str, Any]]:
    h = _harness()
    return {c["case_id"]: c for c in h.oracle.load_gate_cases()}


def _trace(case: dict[str, Any], proposal: Any) -> dict[str, Any]:
    h = _harness()
    env, mandate, ctx = h.build({**case, "proposal": proposal})
    d = evaluate(env, mandate, ctx)
    checks = []
    for name in GATE.check_names:
        status = "passed" if name in d.passed else ("decided" if name == d.decided_by else "not_run")
        checks.append({"name": name, "label": CHECK_LABELS.get(name, name), "status": status})
    return {"verdict": d.verdict.value, "reason_code": d.reason_code, "decided_by": d.decided_by,
            "detail": d.detail[:300], "checks": checks}


def list_cases() -> dict[str, Any]:
    out = []
    for c in _cases().values():
        t = _trace(c, c["proposal"])
        exp = c["expected"]
        out.append({"case_id": c["case_id"], "family": c["family"], "title": c["title"], "expected": exp,
                    "actual": {"verdict": t["verdict"], "reason_code": t["reason_code"]}, "decided_by": t["decided_by"],
                    "pass": t["verdict"] == exp["verdict"] and t["reason_code"] == exp["reason_code"]})
    families = {f["id"]: f["title"] for f in _harness().oracle.load_families()}
    return {"evidence_mode": "SYNTHETIC_FIXTURE", "note": "Deterministic replay of frozen hand-written cases; no model involved.",
            "families": families, "cases": out,
            "summary": {"total": len(out), "passed": sum(1 for c in out if c["pass"])}}


def get_case(case_id: str) -> dict[str, Any] | None:
    c = _cases().get(case_id)
    if c is None:
        return None
    return {"case_id": c["case_id"], "family": c["family"], "title": c["title"], "setup_overrides": c.get("setup", {}),
            "effective_setup": _harness().oracle.effective_setup(c), "proposal": c["proposal"], "expected": c["expected"],
            "trace": _trace(c, c["proposal"])}


def evaluate_edited(case_id: str, proposal: Any) -> dict[str, Any] | None:
    c = _cases().get(case_id)
    if c is None:
        return None
    return {"case_id": case_id, "edited": True, "trace": _trace(c, proposal),
            "note": "Your edited proposal was checked against this case's setup by the real gate. Nothing was stored or sent."}
