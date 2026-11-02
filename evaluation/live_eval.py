"""Paired application-level evaluation on the frozen live probes.

Systems (docs/POLICY.md; spec section 14.2):
  B0  deterministic baseline parser (evaluation/baselines/b0_parser.py) + the same gate, renderers, verifier.
  D   Delegate: the Meta model + gate + exact approval + final fence + verifier.
  B1  the same Meta model, context, schemas, renderers, budgets and read-only preview tool, but with the
      mandate gate's authority checks REMOVED. Exists only inside this process against a private throwaway
      database. It is never reachable from the public app, which has no switch for it.

Each probe runs in a fresh demo session. The simulated owner approves any approval request (so D's gate,
not the owner, is what is being measured) and B1 commits whatever parses. Outcomes are judged by
evaluation/probe_judge.py, which is written from the probe's expected fields, not from the gate.

Usage:
  uv run python evaluation/live_eval.py --systems b0              # free, deterministic
  uv run python evaluation/live_eval.py --systems d,b1 --max-calls 120   # spends provider calls
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT))

import oracle  # noqa: E402
from mandate_gate import Decision, Verdict  # noqa: E402
from sqlalchemy import Engine  # noqa: E402

from baselines import b0_parser  # noqa: E402
from delegate import executor, runtime  # noqa: E402
from delegate.clock import Clock  # noqa: E402
from delegate.db import embedded_url, make_engine, migrate, rows  # noqa: E402
from delegate.meta_adapter import MetaAdapter  # noqa: E402
from delegate.policy import ActionEnvelope, GateContext, MandateSnapshot  # noqa: E402
from delegate.settings import Settings  # noqa: E402

RESULTS_DIR = ROOT / "evaluation" / "results"


# ------------------------------------------------------------------ B1 (private ablation)


def _b1_evaluate(a: ActionEnvelope, m: MandateSnapshot, c: GateContext) -> Decision:
    """B1: keep only schema validity and session scope; drop every mandate check."""
    if a.proposal is None:
        return Decision(Verdict.INVALID, "SCHEMA_INVALID", "1_schema")
    if a.session_id != c.auth_session_id or a.run_id != c.run_id:
        return Decision(Verdict.DENY, "OUT_OF_SCOPE", "2_scope")
    if c.committed_operation:
        return Decision(Verdict.ALLOW, "DUPLICATE", "9_duplicate")
    if a.proposal.kind == "reserve_item" and c.item_reserved:
        return Decision(Verdict.DENY, "ITEM_UNAVAILABLE", "db_unique")  # the database's unique index still holds
    return Decision(Verdict.ALLOW, None, None)


@contextmanager
def b1_bypass() -> Iterator[None]:
    original = executor.evaluate
    executor.evaluate = _b1_evaluate  # type: ignore[assignment]
    try:
        yield
    finally:
        executor.evaluate = original  # type: ignore[assignment]


# ------------------------------------------------------------------ helpers


def _git_sha() -> str:
    return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Driver:
    """Drives one probe through the real API using a TestClient and a synchronous worker."""

    def __init__(self, engine: Engine, settings: Settings, adapter: MetaAdapter, system: str) -> None:
        from fastapi.testclient import TestClient

        from delegate.app import create_app

        self.engine, self.settings, self.adapter, self.system = engine, settings, adapter, system
        self.clock = Clock()
        self.client = TestClient(create_app(settings, self.clock, engine, start_worker=False, adapter=adapter))
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def run_probe(self, probe: dict[str, Any]) -> dict[str, Any]:
        c = self.client
        tok = c.post("/v1/demo-sessions").json()["token"]
        hd = {"Authorization": f"Bearer {tok}"}
        cur = c.get("/v1/demo-sessions/current", headers=hd).json()
        m = cur["mandate"]
        c.post(f"/v1/mandates/{m['mandate_id']}/activate", json={"revision": m["current_revision"]}, headers=hd)
        sid = cur["session_id"]
        run_id = c.post("/v1/runs", json={"buyer_id": "buyer-riya"}, headers=hd).json()["run_id"]
        if self.system == "b0":
            from delegate.db import exe, tx

            with tx(self.engine) as conn:
                exe(conn, "UPDATE runs SET system = 'b0' WHERE run_id = :r", r=run_id)
        started = time.perf_counter()
        model_ms = 0
        approvals_required = 0
        last_kind = None
        for turn in probe["turns"]:
            if turn["type"] == "buyer_message":
                r = c.post(f"/v1/runs/{run_id}/buyer-turns", json={"text": turn["text"]}, headers=hd)
                if r.status_code != 202:
                    break
            elif turn["type"] == "accept_open_offer":
                offer = c.get(f"/v1/runs/{run_id}", headers=hd).json()["open_offer"]
                if offer is None:
                    break
                c.post(f"/v1/runs/{run_id}/accept-offer", json={"offer_id": offer["offer_id"]}, headers=hd)
            run = runtime.claim(self.engine, self.clock, f"eval-{self.system}", run_id)
            if run is None:
                continue
            t0 = time.perf_counter()
            runtime.process(self.engine, self.clock, self.settings, self.adapter, run)
            model_ms += round((time.perf_counter() - t0) * 1000)
            view = c.get(f"/v1/runs/{run_id}", headers=hd).json()
            if view["pending_approval"]:
                approvals_required += 1
                ap = view["pending_approval"]
                c.post(f"/v1/approvals/{ap['approval_id']}/resolve", json={"decision": "approve", "action_hash": ap["action_hash"]}, headers=hd)
        with self.engine.connect() as conn:
            acts = rows(conn, "SELECT action_id, kind, raw_proposal, status, step_no, attempt FROM proposed_actions WHERE run_id = :r ORDER BY step_no, attempt", r=run_id)
            decs = rows(conn, "SELECT d.action_id, d.verdict, d.reason_code FROM decisions d JOIN proposed_actions pa ON pa.action_id = d.action_id WHERE pa.run_id = :r", r=run_id)
            msgs = rows(conn, "SELECT text, offer_id, source_operation_id, created_at FROM marketplace_messages WHERE run_id = :r AND role = 'seller' ORDER BY created_at", r=run_id)
            revoked = rows(conn, "SELECT occurred_at FROM audit_events WHERE session_id = :s AND event_type = 'mandate.revoked'", s=sid)
            approved_res = rows(
                conn,
                "SELECT r.reservation_id, (a.consumed_at IS NOT NULL) AS approved FROM marketplace_reservations r "
                "JOIN operations o ON o.operation_id = r.source_operation_id LEFT JOIN approvals a ON a.action_id = o.action_id "
                "WHERE r.run_id = :r", r=run_id)
            offers = rows(conn, "SELECT offer_id, price_minor, source FROM marketplace_offers WHERE run_id = :r AND source = 'counter_offer'", r=run_id)
            res = rows(conn, "SELECT price_minor, acceptance_event_id, created_at FROM marketplace_reservations WHERE run_id = :r", r=run_id)
            sig = rows(conn, "SELECT risk_signals FROM buyer_events WHERE run_id = :r", r=run_id)
            calls = rows(conn, "SELECT mode, status, latency_ms, usage FROM provider_calls WHERE run_id = :r", r=run_id)
            receipt = rows(conn, "SELECT verdict FROM receipts WHERE run_id = :r", r=run_id)
        if acts:
            last = acts[-1]["raw_proposal"] or {}
            last_kind = last.get("kind")
            if last_kind == "send_message" and last.get("message_intent"):
                last_kind = f"send_message:{last['message_intent']}"
                third = last.get("clarify_topic") or last.get("decline_reason")
                if third:
                    last_kind = f"{last_kind}:{third}"
        high = any(s in ("qr_receive_scam", "fake_payment_proof", "authority_claim") for e in sig for s in (e["risk_signals"] or []))
        for a in acts:
            for s in (a["raw_proposal"] or {}).get("buyer_risk_signals") or []:
                if s in ("qr_receive_scam", "fake_payment_proof", "authority_claim"):
                    high = True
        revoked_at = min((e["occurred_at"] for e in revoked), default=None)

        def after_rev(t: Any) -> bool:
            return revoked_at is not None and t > revoked_at

        timeline: list[tuple[Any, dict[str, Any]]] = []
        for msg in msgs:
            price = next((int(o["price_minor"]) for o in offers if o["offer_id"] == msg["offer_id"]), None)
            if msg["text"].startswith(("Reserved for you", "Aapke liye")):
                continue  # the reservation notice is part of the reservation effect below
            timeline.append((msg["created_at"], {"type": "offer" if price is not None else "message", "price_minor": price,
                                                 "text": msg["text"], "after_revocation": after_rev(msg["created_at"])}))
        for rr in res:
            timeline.append((rr["created_at"], {"type": "reservation", "price_minor": int(rr["price_minor"]), "text": "",
                                                "after_revocation": after_rev(rr["created_at"])}))
        effects = [e for _, e in sorted(timeline, key=lambda x: x[0])]
        usage_total = [cl["usage"].get("total_tokens") for cl in calls if cl.get("usage")]
        return {
            "session_id": sid, "run_id": run_id,
            "last_turn_kind": last_kind,
            "committed_effects": effects,
            "denied_proposals": [d["reason_code"] for d in decs if d["verdict"] in ("deny", "invalid")],
            "reservation_buyer_high_risk": bool(res) and high,
            "reservation_owner_approved": bool(approved_res) and all(bool(x["approved"]) for x in approved_res),
            "approvals_required": approvals_required,
            "receipt_verdicts": [x["verdict"] for x in receipt],
            "provider_calls": len([cl for cl in calls if cl["mode"] == "live"]),
            "provider_errors": len([cl for cl in calls if cl["status"] not in ("ok",)]),
            "tokens_total": sum(t for t in usage_total if isinstance(t, int)) if usage_total else None,
            "wall_ms": round((time.perf_counter() - started) * 1000), "agent_ms": model_ms,
        }


def judge(probe: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    try:
        import probe_judge

        return dict(probe_judge.judge(probe, outcome))
    except ImportError:
        return {"pass": None, "note": "probe_judge.py not available"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="b0")
    ap.add_argument("--probes", default="", help="comma-separated case ids; default all non-integration probes")
    ap.add_argument("--max-calls", type=int, default=120)
    ap.add_argument("--seed", type=int, default=20261103)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    probes = [p for p in oracle.load_live_probes() if not p.get("integration_only")]
    if args.probes:
        wanted = set(args.probes.split(","))
        probes = [p for p in probes if p["case_id"] in wanted]
    systems = args.systems.split(",")
    url = embedded_url(tempfile.mkdtemp(prefix="delegate-eval-pg-"))
    engine = make_engine(url, "")
    migrate(engine)
    runtime.register_baseline("b0", b0_parser.propose)

    order: list[tuple[str, dict[str, Any], int]] = [(s, p, r) for r in range(args.repeat) for p in probes for s in systems]
    random.Random(args.seed).shuffle(order)  # counterbalance system order across probes
    out_path = RESULTS_DIR / f"live_{'_'.join(systems)}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    RESULTS_DIR.mkdir(exist_ok=True)
    meta = {
        "commit": _git_sha(), "scenarios_sha256": _file_sha(ROOT / "evaluation" / "scenarios.json"),
        "systems": systems, "seed": args.seed, "repeat": args.repeat, "max_calls": args.max_calls,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    calls_used = 0
    with out_path.open("w") as fh:
        fh.write(json.dumps({"meta": meta}) + "\n")
        for system, probe, rep in order:
            settings = Settings(provider_mode="mock" if system == "b0" else "live", run_worker=False, web_dist="__none__",
                                sessions_per_ip_per_hour=100_000)
            adapter = MetaAdapter(settings)
            if system != "b0" and adapter.mode != "live":
                rec = {"system": system, "case_id": probe["case_id"], "repeat": rep, "status": "NOT_RUN", "reason": "no live provider key"}
                fh.write(json.dumps(rec) + "\n")
                continue
            if system != "b0" and calls_used >= args.max_calls:
                rec = {"system": system, "case_id": probe["case_id"], "repeat": rep, "status": "NOT_RUN", "reason": "provider call cap reached"}
                fh.write(json.dumps(rec) + "\n")
                continue
            driver = Driver(engine, settings, adapter, system)
            try:
                if system == "b1":
                    with b1_bypass():
                        outcome = driver.run_probe(probe)
                else:
                    outcome = driver.run_probe(probe)
            finally:
                driver.close()
            calls_used += outcome["provider_calls"]
            mode = {"b0": "SYNTHETIC_FIXTURE+CONTROLLED_SANDBOX", "d": "LIVE_PROVIDER+CONTROLLED_SANDBOX",
                    "b1": "LIVE_PROVIDER+PRIVATE_ABLATION_SANDBOX"}[system]
            rec = {"system": system, "case_id": probe["case_id"], "family": probe["family"], "language": probe.get("language"),
                   "feasible": probe["feasible"], "repeat": rep, "status": "RUN", "evidence_mode": mode,
                   "outcome": outcome, "judgement": judge(probe, outcome)}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"{system:3} {probe['case_id']:5} pass={rec['judgement'].get('pass')} calls={outcome['provider_calls']} last={outcome['last_turn_kind']}")
    print(f"wrote {out_path.relative_to(ROOT)}; provider calls used: {calls_used}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
