"""All frozen deterministic gate cases from evaluation/scenarios.json must pass against the gate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation"))

import gate_harness  # noqa: E402


def test_every_frozen_gate_case_matches_the_hand_authored_oracle() -> None:
    results = gate_harness.run_all()
    fails = [f"{r['case_id']}: expected {r['expected']} got {r['actual']}" for r in results if not r["pass"]]
    assert len(results) >= 150
    assert not fails, "\n".join(fails)
