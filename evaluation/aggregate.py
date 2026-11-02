"""Aggregate case-level results into evaluation/RESULTS.json and evaluation/REPORT.md.

Inputs: evaluation/results/gate_cases.jsonl, the newest live_*.jsonl per system, and an optional pytest JUnit XML
(evidence/test-runs/pytest.xml). Every metric carries its numerator and denominator. Missing data is reported as
null with a reason, never as zero.
Usage: uv run python evaluation/aggregate.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evaluation" / "results"
PRICES = {"muse-spark-1.3": {"input": 1.25, "output": 4.25}}  # USD per 1M tokens, dev.meta.ai pricing page read 2026-11-02


def _ratio(n: int, d: int) -> dict[str, Any]:
    return {"numerator": n, "denominator": d, "rate": round(n / d, 4) if d else None}


def _latest_live() -> dict[str, list[dict[str, Any]]]:
    by_system: dict[str, list[dict[str, Any]]] = {}
    files = sorted(RES.glob("live_*.jsonl"), key=lambda p: p.stat().st_mtime)
    for f in files:
        lines = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        meta = lines[0].get("meta", {})
        for rec in lines[1:]:
            rec["_file"] = f.name
            rec["_commit"] = meta.get("commit")
        systems = set(meta.get("systems", []))
        for s in systems:
            recs = [r for r in lines[1:] if r.get("system") == s and r.get("status") == "RUN"]
            if recs:
                by_system[s] = recs  # newest file with real runs wins
    return by_system


def _system_metrics(recs: list[dict[str, Any]]) -> dict[str, Any]:
    feasible = [r for r in recs if r["feasible"]]
    adversarial = [r for r in recs if not r["feasible"]]
    passes = [r for r in recs if r["judgement"].get("pass")]
    forbidden_eps = [r for r in recs if r["judgement"].get("forbidden_effects_observed")]
    blocked = sum(len(r["outcome"]["denied_proposals"]) for r in recs)
    reservations = [r for r in recs if any(e["type"] == "reservation" for e in r["outcome"]["committed_effects"])]
    verified = [r for r in reservations if "verified" in r["outcome"].get("receipt_verdicts", [])]
    false_success = [r for r in recs if r["outcome"].get("receipt_verdicts") and r["outcome"]["receipt_verdicts"][-1] == "verified"
                     and not any(e["type"] == "reservation" for e in r["outcome"]["committed_effects"])]
    agent_ms = sorted(r["outcome"]["agent_ms"] for r in recs)
    tokens = [r["outcome"]["tokens_total"] for r in recs if r["outcome"].get("tokens_total") is not None]
    calls = sum(r["outcome"]["provider_calls"] for r in recs)
    hard = [r for r in recs if int(r["case_id"].rstrip("h")[1:]) >= 16]
    base = [r for r in recs if int(r["case_id"].rstrip("h")[1:]) < 16]
    hinglish = [r for r in recs if r["case_id"].endswith("h")]
    return {
        "episodes": len(recs),
        "probe_pass": _ratio(len(passes), len(recs)),
        "probe_pass_original_M01_M15": _ratio(sum(1 for r in base if r["judgement"].get("pass")), len(base)),
        "probe_pass_harder_M16_M27": _ratio(sum(1 for r in hard if r["judgement"].get("pass")), len(hard)),
        "probe_pass_hinglish": _ratio(sum(1 for r in hinglish if r["judgement"].get("pass")), len(hinglish)),
        "valid_task_completion_feasible": _ratio(sum(1 for r in feasible if r["judgement"].get("pass")), len(feasible)),
        "episodes_with_forbidden_effect": _ratio(len(forbidden_eps), len(recs)),
        "adversarial_episodes_with_forbidden_effect": _ratio(sum(1 for r in adversarial if r["judgement"].get("forbidden_effects_observed")), len(adversarial)),
        "forbidden_effects_by_type": _count([e for r in recs for e in r["judgement"].get("forbidden_effects_observed") or []]),
        "proposals_blocked_by_gate": blocked,
        "reservations_committed": len(reservations),
        "reservations_verified": _ratio(len(verified), len(reservations)),
        "false_success": _ratio(len(false_success), len(verified) + len(false_success)),
        "owner_approvals_requested": sum(r["outcome"]["approvals_required"] for r in recs),
        "agent_time_ms": {"n": len(agent_ms), "median": agent_ms[len(agent_ms) // 2] if agent_ms else None,
                          "max": agent_ms[-1] if agent_ms else None},
        "provider_calls": calls,
        "tokens_total": sum(tokens) if tokens else None,
        "tokens_note": None if tokens else "provider usage not reported or no live calls",
        "commit": recs[0].get("_commit") if recs else None,
        "source_file": recs[0].get("_file") if recs else None,
        "failures": [{"case_id": r["case_id"], "forbidden": r["judgement"].get("forbidden_effects_observed"),
                      "last_turn_kind": r["outcome"]["last_turn_kind"], "acceptable_kind": r["judgement"].get("acceptable_kind")}
                     for r in recs if not r["judgement"].get("pass")],
    }


def _count(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def _junit() -> dict[str, Any] | None:
    path = ROOT / "evidence" / "test-runs" / "pytest.xml"
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    a = suite.attrib
    return {"tests": int(a.get("tests", 0)), "failures": int(a.get("failures", 0)), "errors": int(a.get("errors", 0)),
            "skipped": int(a.get("skipped", 0)), "timestamp": a.get("timestamp"), "file": str(path.relative_to(ROOT))}


def main() -> int:
    gate = [json.loads(x) for x in (RES / "gate_cases.jsonl").read_text().splitlines() if x.strip()]
    live = _latest_live()
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    results: dict[str, Any] = {
        "schema_version": "delegate-results-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "commit": sha,
        "scenarios_sha256": hashlib.sha256((ROOT / "evaluation" / "scenarios.json").read_bytes()).hexdigest(),
        "gate_oracle_agreement": {**_ratio(sum(1 for g in gate if g["pass"]), len(gate)), "evidence_mode": "SYNTHETIC_FIXTURE",
                                  "scope": "Narrow structured action domain in docs/POLICY.md; not general semantic safety."},
        "test_suite": _junit(),
        "systems": {s: _system_metrics(r) for s, r in sorted(live.items())},
        "systems_not_run": [s for s in ("b0", "d", "b1") if s not in live],
        "pricing_used": PRICES,
        "limitations": [
            "Small paired pilot on fictional probes written for this project; not a statistical estimate.",
            "Probe outcomes are judged by evaluation/probe_judge.py from each probe's expected fields; keyword-based detectors in the judge can misread phrasing.",
            "B1 removes mandate enforcement only inside a private evaluation process; it is never available in the public app.",
            "The simulated owner approves every approval request, so D's results measure the gate, not owner judgement.",
            "Sandbox effects only; no real marketplace, payment or identity.",
        ],
    }
    for s, m in results["systems"].items():
        if m["tokens_total"] and s in ("d", "b1"):
            m["cost_note"] = "Token totals include input and output; per-direction split is in the case files. Cost not computed without the split."
    (ROOT / "evaluation" / "RESULTS.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    lines = ["# Evaluation report", "", f"Generated {results['generated_at']} at commit `{sha}`. Scenarios hash `{results['scenarios_sha256'][:16]}…`.", ""]
    g = results["gate_oracle_agreement"]
    lines += ["## Deterministic gate", "", f"{g['numerator']}/{g['denominator']} frozen hand-authored gate cases match the oracle (SYNTHETIC_FIXTURE).", ""]
    if results["test_suite"]:
        t = results["test_suite"]
        lines += ["## Test suite", "", f"{t['tests']} tests, {t['failures']} failures, {t['errors']} errors, {t['skipped']} skipped (`{t['file']}`).", ""]
    lines += ["## Live probes", ""]
    if not results["systems"]:
        lines.append("No live-probe runs yet.")
    else:
        lines += ["| Metric | " + " | ".join(results["systems"]) + " |", "| --- |" + " --- |" * len(results["systems"])]
        keys = [("All probes passed", "probe_pass"), ("Original M01–M15", "probe_pass_original_M01_M15"),
                ("Harder M16–M27", "probe_pass_harder_M16_M27"), ("Hinglish twins", "probe_pass_hinglish"),
                ("Valid completion (feasible)", "valid_task_completion_feasible"),
                ("Episodes with a forbidden effect", "episodes_with_forbidden_effect"),
                ("Adversarial episodes with a forbidden effect", "adversarial_episodes_with_forbidden_effect"),
                ("Reservations verified", "reservations_verified"), ("False success", "false_success")]
        for label, k in keys:
            cells = []
            for m in results["systems"].values():
                v = m[k]
                cells.append(f"{v['numerator']}/{v['denominator']}")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("| Proposals blocked by gate | " + " | ".join(str(m["proposals_blocked_by_gate"]) for m in results["systems"].values()) + " |")
        lines.append("| Provider calls | " + " | ".join(str(m["provider_calls"]) for m in results["systems"].values()) + " |")
        lines.append("| Median agent time per probe (ms) | " + " | ".join(str(m["agent_time_ms"]["median"]) for m in results["systems"].values()) + " |")
    if results["systems_not_run"]:
        lines += ["", "Not run: " + ", ".join(results["systems_not_run"]) + "."]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in results["limitations"]]
    for s, m in results["systems"].items():
        if m["failures"]:
            lines += ["", f"## {s} failures ({len(m['failures'])})", ""]
            lines += [f"- {f['case_id']}: last action `{f['last_turn_kind']}`, forbidden {f['forbidden'] or 'none'}" for f in m["failures"]]
    (ROOT / "evaluation" / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: results[k] for k in ("gate_oracle_agreement", "systems_not_run")}, indent=1))
    for s, m in results["systems"].items():
        print(s, "probe_pass", m["probe_pass"], "forbidden", m["episodes_with_forbidden_effect"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
