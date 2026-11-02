"""Execution coordinator: persist proposals, run the gate under lock, commit effects once, verify receipts.

Every mutation goes through ``execute``. It opens one transaction, locks the mandate row and then the
listing row, reloads everything, runs the full gate against the current revision and server clock, and only
then writes the sandbox effect, the operation record and the audit events. Provider calls never happen
inside this transaction.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mandate_gate import ApprovalRecord, ApprovalStatus, Decision, Verdict, canonical_bytes
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import OperationalError

from delegate import store
from delegate.clock import Clock
from delegate.db import exe, new_id, one, rows, tx
from delegate.policy import (
    OWNER_EXPLANATIONS,
    AcceptanceSnap,
    ActionEnvelope,
    GateContext,
    MandateSnapshot,
    OfferSnap,
    evaluate,
    safe_message,
)
from delegate.proposal import Proposal, ProposalInvalid, parse_proposal
from delegate.renderers import format_money, render_message, render_reservation_notice

Hook = Callable[[str, dict[str, Any]], None]
_hooks: dict[str, Hook] = {}


def set_hook(name: str, fn: Hook | None) -> None:
    """Test-only barriers: 'after_locks', 'before_commit', 'after_commit'."""
    if fn is None:
        _hooks.pop(name, None)
    else:
        _hooks[name] = fn


def _hook(name: str, info: dict[str, Any]) -> None:
    fn = _hooks.get(name)
    if fn is not None:
        fn(name, info)


ACTION_TTL_S = 120


@dataclass(frozen=True)
class ExecResult:
    action_id: str
    verdict: str
    reason_code: str | None
    status: str
    operation_id: str | None = None
    approval_id: str | None = None
    receipt_verdict: str | None = None
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ---------------------------------------------------------------- proposal persistence


def persist_proposal(
    engine: Engine,
    clock: Clock,
    session_id: str,
    run_id: str,
    step_no: int,
    attempt: int,
    raw: Any,
    origin: str,
    provider_call_id: str | None = None,
) -> str:
    """Validate and persist a model proposal as an immutable action. Returns action_id."""
    proposal: Proposal | None
    error: str | None = None
    try:
        proposal = parse_proposal(raw)
    except ProposalInvalid as exc:
        proposal, error = None, str(exc)
    with tx(engine) as conn:
        run = store.get_run(conn, session_id, run_id, for_update=True)
        prev = one(conn, "SELECT COALESCE(max(attempt), 0) AS a FROM proposed_actions WHERE run_id = :r AND step_no = :st",
                   r=run_id, st=step_no)
        attempt = int(prev["a"]) + 1 if prev else attempt
        m = store.mandate_row(conn, session_id)
        snap = store.snapshot(conn, m)
        now = clock.now()
        expires = now + dt.timedelta(seconds=ACTION_TTL_S)
        if m["expires_at"] is not None:
            expires = min(expires, m["expires_at"])
        action_id = new_id("act")
        env = ActionEnvelope(
            action_id=action_id,
            session_id=session_id,
            run_id=run_id,
            item_id=run["item_id"],
            buyer_id=run["buyer_id"],
            mandate_id=m["mandate_id"],
            mandate_revision=snap.revision,
            created_at=now,
            expires_at=expires,
            proposal=proposal,
            raw_proposal=raw,
            validation_error=error,
            hold_seconds=snap.reservation_hold_seconds,
        )
        body = env.hash_body()
        raw_json: Any
        if isinstance(raw, (dict, list)):
            raw_json = raw
        else:
            try:
                raw_json = json.loads(raw)
                if not isinstance(raw_json, (dict, list)):
                    raw_json = {"unparsed": str(raw)[:2000]}
            except (TypeError, ValueError):
                raw_json = {"unparsed": str(raw)[:2000]}
        exe(
            conn,
            "INSERT INTO proposed_actions (action_id, session_id, run_id, step_no, attempt, origin, kind, raw_proposal, "
            "validation_error, canonical_body, action_hash, mandate_id, mandate_revision, status, created_at, expires_at, "
            "provider_call_id) VALUES (:a, :s, :r, :st, :at, :o, :k, CAST(:raw AS jsonb), :ve, :cb, :h, :m, :mr, :status, :c, :e, :pc)",
            a=action_id, s=session_id, r=run_id, st=step_no, at=attempt, o=origin,
            k=proposal.kind if proposal else None, raw=raw_json, ve=error,
            cb=canonical_bytes(body).decode(), h=env.hash(), m=m["mandate_id"], mr=snap.revision,
            status="proposed" if proposal else "invalid", c=now, e=expires, pc=provider_call_id,
        )
        summary = _describe(proposal) if proposal else "Invalid proposal (did not follow the action format)."
        store.append_event(
            conn, clock, session_id, "action.proposed", "agent", summary, run_id=run_id, object_id=action_id,
            data={"origin": origin, "step": step_no, "attempt": attempt, "proposal": proposal.as_dict() if proposal else None,
                  "validation_error": error, "action_hash": env.hash()},
            owner_only=True,
        )
    return action_id


def _describe(p: Proposal) -> str:
    if p.kind == "send_message":
        if p.message_intent == "counter_offer" and p.price_minor is not None and p.currency:
            return f"Agent proposes a counter-offer of {format_money(p.price_minor, p.currency)}."
        if p.message_intent == "decline":
            return f"Agent proposes declining ({p.decline_reason})."
        if p.message_intent == "clarify":
            return f"Agent proposes asking for clarification ({p.clarify_topic})."
        return "Agent proposes answering with public listing facts."
    if p.kind == "reserve_item" and p.price_minor is not None and p.currency:
        return f"Agent proposes reserving the laptop at {format_money(p.price_minor, p.currency)}."
    return "Agent proposes to wait for the buyer."


# ---------------------------------------------------------------- snapshot loading


def _load_action(conn: Connection, session_id: str, action_id: str) -> tuple[dict[str, Any], ActionEnvelope]:
    a = one(conn, "SELECT * FROM proposed_actions WHERE action_id = :a AND session_id = :s", a=action_id, s=session_id)
    if a is None:
        raise store.ApiError(404, "NOT_FOUND", "Action not found.")
    body = json.loads(a["canonical_body"])
    proposal: Proposal | None = None
    if a["validation_error"] is None:
        proposal = parse_proposal(body["payload"])
    terms = body.get("terms") or {}
    env = ActionEnvelope(
        action_id=body["action_id"],
        session_id=body["session_id"],
        run_id=body["run_id"],
        item_id=body["item_id"],
        buyer_id=body["buyer_id"],
        mandate_id=body["mandate_id"],
        mandate_revision=body["mandate_revision"],
        created_at=dt.datetime.fromisoformat(body["created_at"]),
        expires_at=dt.datetime.fromisoformat(body["expires_at"]),
        proposal=proposal,
        validation_error=a["validation_error"],
        delivery=terms.get("delivery", "pickup"),
        payment_terms=terms.get("payment_terms", "cash_at_pickup"),
        hold_seconds=terms.get("hold_seconds", 0),
    )
    if env.hash() != a["action_hash"]:
        raise RuntimeError("stored action hash does not match its canonical body")
    return a, env


def _context(
    conn: Connection,
    clock: Clock,
    session_id: str,
    run: dict[str, Any],
    fence: int | None,
    action_id: str,
    principal: str,
) -> GateContext:
    now = clock.now()
    fence_ok = True
    if fence is not None:
        fence_ok = run["fence"] == fence and run["lease_until"] is not None and run["lease_until"] > now
    counts = one(
        conn,
        "SELECT (SELECT count(*) FROM operations o JOIN proposed_actions pa ON pa.action_id = o.action_id "
        "        WHERE pa.run_id = :r AND o.kind = 'send_message') AS msgs, "
        "       (SELECT count(*) FROM marketplace_reservations WHERE run_id = :r) AS res",
        r=run["run_id"],
    )
    assert counts is not None
    reserved = one(
        conn,
        "SELECT 1 AS x FROM marketplace_reservations WHERE session_id = :s AND item_id = :i AND status = 'active' AND expires_at > :n",
        s=session_id, i=run["item_id"], n=now,
    )
    signals: set[str] = set()
    for ev in rows(conn, "SELECT risk_signals FROM buyer_events WHERE run_id = :r", r=run["run_id"]):
        signals.update(ev["risk_signals"] or [])
    for pa in rows(conn, "SELECT raw_proposal FROM proposed_actions WHERE run_id = :r AND validation_error IS NULL", r=run["run_id"]):
        raw = pa["raw_proposal"] or {}
        signals.update(raw.get("buyer_risk_signals") or [])
    offers = {
        o["offer_id"]: OfferSnap(o["offer_id"], int(o["price_minor"]), o["currency"], o["status"])
        for o in rows(conn, "SELECT * FROM marketplace_offers WHERE run_id = :r", r=run["run_id"])
    }
    accs = {
        e["event_id"]: AcceptanceSnap(e["event_id"], e["offer_id"], True, e["used_by_action"] is not None)
        for e in rows(conn, "SELECT * FROM buyer_events WHERE run_id = :r AND kind = 'offer_acceptance'", r=run["run_id"])
    }
    ap = one(conn, "SELECT * FROM approvals WHERE action_id = :a", a=action_id)
    approval = None
    if ap is not None:
        approval = ApprovalRecord(
            approval_id=ap["approval_id"], action_id=ap["action_id"], action_hash=ap["action_hash"],
            principal=ap["owner_principal"], authority_revision=ap["mandate_revision"],
            status=ApprovalStatus(ap["status"]), expires_at=ap["expires_at"], consumed_at=ap["consumed_at"],
        )
    committed = one(conn, "SELECT 1 AS x FROM operations WHERE action_id = :a AND state = 'committed'", a=action_id) is not None
    return GateContext(
        now=now, auth_session_id=session_id, run_id=run["run_id"], item_id=run["item_id"], owner_principal=principal,
        run_state=run["state"], fence_current=fence_ok, outbound_messages=int(counts["msgs"]),
        reservations=int(counts["res"]), item_reserved=reserved is not None, buyer_signals=frozenset(signals),
        offers=offers, acceptances=accs, committed_operation=committed, approval=approval,
    )


def _record_decision(conn: Connection, clock: Clock, session_id: str, env: ActionEnvelope, rev: int, d: Decision, phase: str) -> None:
    exe(
        conn,
        "INSERT INTO decisions (decision_id, session_id, action_id, action_hash, mandate_revision, phase, verdict, reason_code, "
        "decided_by, passed, checked_at) VALUES (:d, :s, :a, :h, :r, :ph, :v, :rc, :db, CAST(:p AS jsonb), :n)",
        d=new_id("dec"), s=session_id, a=env.action_id, h=env.hash(), r=rev, ph=phase, v=d.verdict.value,
        rc=d.reason_code, db=d.decided_by, p=list(d.passed), n=clock.now(),
    )


# ---------------------------------------------------------------- execution


def execute(
    engine: Engine,
    clock: Clock,
    session_id: str,
    action_id: str,
    *,
    fence: int | None,
    owner_approval: tuple[str, str] | None = None,
) -> ExecResult:
    """Run the gate for ``action_id`` under lock and commit its effect at most once.

    ``fence``: the worker's lease fence (None for owner-initiated execution after approval).
    ``owner_approval``: (approval_id, action_hash the owner saw) to approve and execute in one transaction.
    """
    principal = store.Principal(session_id).owner
    try:
        result, verify_op = _execute_tx(engine, clock, session_id, action_id, fence, owner_approval, principal)
    except OperationalError:
        # The commit outcome is unknown (connection lost). Reconcile by operation identity, never by retrying blindly.
        return reconcile(engine, clock, session_id, action_id)
    if verify_op is not None:
        receipt = verify(engine, clock, session_id, verify_op)
        result = ExecResult(**{**result.as_dict(), "receipt_verdict": receipt["verdict"]})
    return result


def _execute_tx(
    engine: Engine,
    clock: Clock,
    session_id: str,
    action_id: str,
    fence: int | None,
    owner_approval: tuple[str, str] | None,
    principal: str,
) -> tuple[ExecResult, str | None]:
    verify_op: str | None = None
    _hook("before_locks", {"action_id": action_id, "session_id": session_id})
    with tx(engine) as conn:
        m = store.mandate_row(conn, session_id, for_update=True)
        listing = one(conn, "SELECT * FROM listings WHERE session_id = :s AND item_id = :i FOR UPDATE", s=session_id, i=m["item_id"])
        assert listing is not None
        a_row, env = _load_action(conn, session_id, action_id)
        run = store.get_run(conn, session_id, env.run_id, for_update=True)
        _hook("after_locks", {"action_id": action_id, "session_id": session_id})

        existing = one(conn, "SELECT * FROM operations WHERE action_id = :a", a=action_id)
        if existing is not None:
            return ExecResult(action_id, "allow", "DUPLICATE", a_row["status"], existing["operation_id"], duplicate=True), None

        m = store.expire_if_due(conn, clock, m)
        snap = store.snapshot(conn, m)

        if owner_approval is not None:
            _apply_owner_approval(conn, clock, session_id, action_id, owner_approval, principal, snap)

        ctx = _context(conn, clock, session_id, run, fence, action_id, principal)
        decision = evaluate(env, snap, ctx)
        _record_decision(conn, clock, session_id, env, snap.revision, decision, "execution")
        p = env.proposal

        if decision.verdict in (Verdict.INVALID, Verdict.DENY):
            status = "invalid" if decision.verdict is Verdict.INVALID else "denied"
            exe(conn, "UPDATE proposed_actions SET status = :st WHERE action_id = :a", st=status, a=action_id)
            feedback = {"code": decision.reason_code, "safe_message": safe_message(decision.reason_code)}
            exe(conn, "UPDATE runs SET last_feedback = CAST(:f AS jsonb), updated_at = :n WHERE run_id = :r", f=feedback, n=clock.now(), r=run["run_id"])
            if owner_approval is not None:
                exe(conn, "UPDATE approvals SET status = 'cancelled', resolved_at = :n WHERE approval_id = :ap AND consumed_at IS NULL", n=clock.now(), ap=owner_approval[0])
                exe(conn, "UPDATE runs SET state = CASE WHEN state = 'waiting_approval' THEN 'awaiting_buyer' ELSE state END WHERE run_id = :r", r=run["run_id"])
            explanation = OWNER_EXPLANATIONS.get(decision.reason_code or "", safe_message(decision.reason_code))
            store.append_event(
                conn, clock, session_id, "action.denied" if status == "denied" else "action.invalid", "gate",
                f"Blocked: {explanation}", run_id=run["run_id"], object_id=action_id,
                data={"verdict": decision.verdict.value, "reason_code": decision.reason_code, "decided_by": decision.decided_by},
                owner_only=True,
            )
            return ExecResult(action_id, decision.verdict.value, decision.reason_code, status), None

        if decision.verdict is Verdict.APPROVAL_REQUIRED:
            assert p is not None and p.price_minor is not None and p.currency is not None
            ap = one(conn, "SELECT * FROM approvals WHERE action_id = :a", a=action_id)
            if ap is not None and ap["status"] != "pending":
                exe(conn, "UPDATE proposed_actions SET status = 'cancelled' WHERE action_id = :a", a=action_id)
                return ExecResult(action_id, decision.verdict.value, decision.reason_code, "cancelled"), None
            if ap is None:
                expires = min(clock.now() + dt.timedelta(seconds=120), env.expires_at)
                if snap.expires_at is not None:
                    expires = min(expires, snap.expires_at)
                approval_id = new_id("apr")
                exe(
                    conn,
                    "INSERT INTO approvals (approval_id, session_id, run_id, action_id, action_hash, owner_principal, "
                    "mandate_revision, status, created_at, expires_at) VALUES (:ap, :s, :r, :a, :h, :o, :rev, 'pending', :n, :e)",
                    ap=approval_id, s=session_id, r=run["run_id"], a=action_id, h=env.hash(), o=principal,
                    rev=snap.revision, n=clock.now(), e=expires,
                )
                exe(conn, "UPDATE proposed_actions SET status = 'waiting_approval' WHERE action_id = :a", a=action_id)
                exe(conn, "UPDATE runs SET state = 'waiting_approval', updated_at = :n WHERE run_id = :r", n=clock.now(), r=run["run_id"])
                store.append_event(
                    conn, clock, session_id, "approval.requested", "gate",
                    f"Waiting for your approval: reserve the laptop for {run['buyer_display_name']} at "
                    f"{format_money(p.price_minor, p.currency)}, pickup only, for {snap.reservation_hold_seconds // 60} minutes.",
                    run_id=run["run_id"], object_id=approval_id,
                    data={"approval_id": approval_id, "action_id": action_id, "action_hash": env.hash(),
                          "expires_at": expires.isoformat(), "price_minor": p.price_minor, "currency": p.currency},
                    owner_only=True,
                )
                return ExecResult(action_id, decision.verdict.value, decision.reason_code, "waiting_approval", approval_id=approval_id), None
            return ExecResult(action_id, decision.verdict.value, decision.reason_code, "waiting_approval", approval_id=ap["approval_id"]), None

        # ALLOW
        assert p is not None
        if p.kind == "no_action":
            exe(conn, "UPDATE proposed_actions SET status = 'no_action' WHERE action_id = :a", a=action_id)
            return ExecResult(action_id, "allow", None, "no_action"), None

        operation_id = new_id("op")
        if p.kind == "send_message":
            facts = {f["id"]: f["text"] for f in listing["public_facts"]}
            text = render_message(p, facts)
            offer_id = None
            if p.message_intent == "counter_offer":
                assert p.price_minor is not None and p.currency is not None
                exe(conn, "UPDATE marketplace_offers SET status = 'superseded' WHERE run_id = :r AND status = 'open'", r=run["run_id"])
                offer_id = new_id("offer")
                exe(
                    conn,
                    "INSERT INTO marketplace_offers (offer_id, session_id, run_id, item_id, price_minor, currency, status, source, "
                    "source_operation_id, created_at) VALUES (:o, :s, :r, :i, :p, :c, 'open', 'counter_offer', :op, :n)",
                    o=offer_id, s=session_id, r=run["run_id"], i=run["item_id"], p=p.price_minor, c=p.currency, op=operation_id, n=clock.now(),
                )
            message_id = new_id("msg")
            exe(
                conn,
                "INSERT INTO marketplace_messages (message_id, session_id, run_id, role, text, source_operation_id, template_key, "
                "language, offer_id, created_at) VALUES (:m, :s, :r, 'seller', :t, :op, :tk, :lang, :o, :n)",
                m=message_id, s=session_id, r=run["run_id"], t=text, op=operation_id,
                tk=f"{p.message_intent}.{p.clarify_topic or p.decline_reason or ''}".rstrip("."), lang=p.language, o=offer_id, n=clock.now(),
            )
            result = {"kind": "send_message", "message_id": message_id, "offer_id": offer_id, "text": text}
            new_state = "awaiting_buyer"
        else:  # reserve_item
            assert p.price_minor is not None and p.currency is not None and p.acceptance_event_id is not None
            acc = one(conn, "SELECT * FROM buyer_events WHERE event_id = :e", e=p.acceptance_event_id)
            assert acc is not None
            reservation_id = new_id("res")
            now = clock.now()
            expires = now + dt.timedelta(seconds=env.hold_seconds)
            exe(
                conn,
                "INSERT INTO marketplace_reservations (reservation_id, session_id, item_id, run_id, buyer_id, price_minor, currency, "
                "delivery, payment_terms, status, source_operation_id, action_hash, acceptance_event_id, created_at, expires_at) "
                "VALUES (:res, :s, :i, :r, :b, :p, :c, :d, :pt, 'active', :op, :h, :acc, :n, :e)",
                res=reservation_id, s=session_id, i=run["item_id"], r=run["run_id"], b=run["buyer_id"], p=p.price_minor, c=p.currency,
                d=env.delivery, pt=env.payment_terms, op=operation_id, h=env.hash(), acc=p.acceptance_event_id, n=now, e=expires,
            )
            exe(conn, "UPDATE buyer_events SET used_by_action = :a WHERE event_id = :e", a=action_id, e=p.acceptance_event_id)
            exe(conn, "UPDATE marketplace_offers SET status = 'accepted' WHERE offer_id = :o", o=acc["offer_id"])
            exe(conn, "UPDATE listings SET state = 'reserved' WHERE session_id = :s AND item_id = :i", s=session_id, i=run["item_id"])
            exe(conn, "UPDATE approvals SET consumed_at = :n WHERE action_id = :a", n=now, a=action_id)
            notice = render_reservation_notice(p.price_minor, p.currency, env.hold_seconds, p.language)
            exe(
                conn,
                "INSERT INTO marketplace_messages (message_id, session_id, run_id, role, text, source_operation_id, template_key, "
                "language, created_at) VALUES (:m, :s, :r, 'seller', :t, :op, 'reservation', :lang, :n)",
                m=new_id("msg"), s=session_id, r=run["run_id"], t=notice, op=operation_id, lang=p.language, n=now,
            )
            result = {"kind": "reserve_item", "reservation_id": reservation_id, "expires_at": expires.isoformat()}
            new_state = "running"  # completed only after verification
            verify_op = operation_id

        exe(
            conn,
            "INSERT INTO operations (operation_id, session_id, action_id, idempotency_key, kind, state, result, committed_at) "
            "VALUES (:op, :s, :a, :k, :kind, 'committed', CAST(:res AS jsonb), :n)",
            op=operation_id, s=session_id, a=action_id, k=f"action:{action_id}", kind=p.kind, res=result, n=clock.now(),
        )
        exe(conn, "UPDATE proposed_actions SET status = 'succeeded' WHERE action_id = :a", a=action_id)
        exe(conn, "UPDATE runs SET state = :st, last_feedback = NULL, updated_at = :n WHERE run_id = :r", st=new_state, n=clock.now(), r=run["run_id"])
        if p.kind == "send_message":
            store.append_event(
                conn, clock, session_id, "operation.committed", "executor", f"Sent to buyer: {text}",
                run_id=run["run_id"], object_id=operation_id, data={**result, "action_id": action_id},
            )
        else:
            store.append_event(
                conn, clock, session_id, "operation.committed", "executor",
                "Reservation written to the marketplace sandbox. Verifying it now.",
                run_id=run["run_id"], object_id=operation_id, data={**result, "action_id": action_id},
            )
        _hook("before_commit", {"action_id": action_id, "operation_id": operation_id})
    _hook("after_commit", {"action_id": action_id, "operation_id": operation_id})
    return ExecResult(action_id, "allow", None, "succeeded", operation_id), verify_op


def _apply_owner_approval(
    conn: Connection,
    clock: Clock,
    session_id: str,
    action_id: str,
    owner_approval: tuple[str, str],
    principal: str,
    snap: MandateSnapshot,
) -> None:
    approval_id, seen_hash = owner_approval
    ap = one(conn, "SELECT * FROM approvals WHERE approval_id = :ap AND session_id = :s FOR UPDATE", ap=approval_id, s=session_id)
    if ap is None or ap["action_id"] != action_id:
        raise store.ApiError(404, "NOT_FOUND", "Approval not found.")
    if ap["status"] != "pending":
        raise store.ApiError(409, "APPROVAL_NOT_PENDING", f"This approval is already {ap['status']}.")
    if clock.now() >= ap["expires_at"]:
        exe(conn, "UPDATE approvals SET status = 'expired', resolved_at = :n WHERE approval_id = :ap", n=clock.now(), ap=approval_id)
        raise store.ApiError(410, "APPROVAL_EXPIRED", "This approval expired. The agent will need to ask again.")
    if seen_hash != ap["action_hash"] or ap["owner_principal"] != principal:
        raise store.ApiError(409, "APPROVAL_MISMATCH", "The action changed since you reviewed it. Review it again.")
    exe(conn, "UPDATE approvals SET status = 'approved', resolved_at = :n WHERE approval_id = :ap", n=clock.now(), ap=approval_id)
    store.append_event(
        conn, clock, session_id, "approval.resolved", "owner", "You approved this exact reservation.",
        run_id=ap["run_id"], object_id=approval_id, data={"decision": "approve", "action_hash": seen_hash, "revision": snap.revision},
    )


def resolve_approval(
    engine: Engine, clock: Clock, session_id: str, approval_id: str, decision: str, seen_hash: str
) -> ExecResult:
    with engine.connect() as conn:
        ap = one(conn, "SELECT * FROM approvals WHERE approval_id = :ap AND session_id = :s", ap=approval_id, s=session_id)
    if ap is None:
        raise store.ApiError(404, "NOT_FOUND", "Approval not found.")
    if decision == "approve":
        return execute(engine, clock, session_id, ap["action_id"], fence=None, owner_approval=(approval_id, seen_hash))
    if decision != "reject":
        raise store.ApiError(422, "BAD_DECISION", "Decision must be approve or reject.")
    with tx(engine) as conn:
        store.mandate_row(conn, session_id, for_update=True)
        ap = one(conn, "SELECT * FROM approvals WHERE approval_id = :ap FOR UPDATE", ap=approval_id)
        assert ap is not None
        if ap["status"] != "pending":
            raise store.ApiError(409, "APPROVAL_NOT_PENDING", f"This approval is already {ap['status']}.")
        exe(conn, "UPDATE approvals SET status = 'rejected', resolved_at = :n WHERE approval_id = :ap", n=clock.now(), ap=approval_id)
        exe(conn, "UPDATE proposed_actions SET status = 'cancelled' WHERE action_id = :a", a=ap["action_id"])
        exe(conn, "UPDATE runs SET state = 'awaiting_buyer', updated_at = :n WHERE run_id = :r AND state = 'waiting_approval'", n=clock.now(), r=ap["run_id"])
        store.append_event(
            conn, clock, session_id, "approval.resolved", "owner", "You rejected the reservation. Nothing was reserved.",
            run_id=ap["run_id"], object_id=approval_id, data={"decision": "reject"},
        )
    return ExecResult(ap["action_id"], "deny", "OWNER_REJECTED", "cancelled", approval_id=approval_id)


# ---------------------------------------------------------------- reconciliation and verification


def reconcile(engine: Engine, clock: Clock, session_id: str, action_id: str) -> ExecResult:
    """Resolve an action whose commit outcome is unknown, by operation identity."""
    with tx(engine) as conn:
        op = one(conn, "SELECT * FROM operations WHERE action_id = :a AND session_id = :s", a=action_id, s=session_id)
        a = one(conn, "SELECT run_id FROM proposed_actions WHERE action_id = :a", a=action_id)
        run_id = a["run_id"] if a else None
        if op is not None:
            store.append_event(conn, clock, session_id, "operation.reconciled", "executor",
                               "An interrupted operation was found committed; no retry was needed.",
                               run_id=run_id, object_id=op["operation_id"], owner_only=True)
            result = ExecResult(action_id, "allow", "DUPLICATE", "succeeded", op["operation_id"], duplicate=True)
            verify_needed = op["kind"] == "reserve_item"
            op_id = op["operation_id"]
        else:
            exe(conn, "UPDATE proposed_actions SET status = 'failed' WHERE action_id = :a AND status IN ('proposed', 'executing', 'unknown')", a=action_id)
            store.append_event(conn, clock, session_id, "operation.reconciled", "executor",
                               "An interrupted operation was confirmed not executed.", run_id=run_id, object_id=action_id, owner_only=True)
            return ExecResult(action_id, "deny", "NOT_EXECUTED", "failed")
    if verify_needed:
        receipt = verify(engine, clock, session_id, op_id)
        return ExecResult(**{**result.as_dict(), "receipt_verdict": receipt["verdict"]})
    return result


VERIFIED_FIELDS = ("operation_id", "action_hash", "session_id", "run_id", "item_id", "buyer_id", "price_minor",
                   "currency", "delivery", "payment_terms", "status", "hold_seconds")


def compare_reservation(expected: dict[str, Any], actual: dict[str, Any] | None, now: dt.datetime) -> tuple[str, list[str]]:
    """Pure comparison used by the verifier. Returns (verdict, mismatching field names)."""
    if actual is None:
        return "unknown", ["reservation_missing"]
    missing = [f for f in VERIFIED_FIELDS if actual.get(f) is None]
    if missing:
        return "unknown", missing
    bad = [f for f in VERIFIED_FIELDS if actual[f] != expected[f]]
    if not bad and actual["expires_at"] <= now:
        bad.append("expired")
    return ("verified", []) if not bad else ("mismatch", bad)


def verify(engine: Engine, clock: Clock, session_id: str, operation_id: str) -> dict[str, Any]:
    """Read the reservation back from the sandbox and match it to the approved action."""
    with tx(engine) as conn:
        op = one(conn, "SELECT * FROM operations WHERE operation_id = :o AND session_id = :s", o=operation_id, s=session_id)
        assert op is not None
        a_row, env = _load_action(conn, session_id, op["action_id"])
        p = env.proposal
        assert p is not None
        expected = {
            "operation_id": operation_id, "action_hash": a_row["action_hash"], "session_id": session_id, "run_id": env.run_id,
            "item_id": env.item_id, "buyer_id": env.buyer_id, "price_minor": p.price_minor, "currency": p.currency,
            "delivery": env.delivery, "payment_terms": env.payment_terms, "status": "active", "hold_seconds": env.hold_seconds,
        }
        r = one(conn, "SELECT * FROM marketplace_reservations WHERE source_operation_id = :o AND session_id = :s", o=operation_id, s=session_id)
        actual = None
        if r is not None:
            actual = {
                "operation_id": r["source_operation_id"], "action_hash": r["action_hash"], "session_id": r["session_id"],
                "run_id": r["run_id"], "item_id": r["item_id"], "buyer_id": r["buyer_id"], "price_minor": int(r["price_minor"]),
                "currency": r["currency"], "delivery": r["delivery"], "payment_terms": r["payment_terms"], "status": r["status"],
                "hold_seconds": int((r["expires_at"] - r["created_at"]).total_seconds()), "expires_at": r["expires_at"],
                "reservation_id": r["reservation_id"], "created_at": r["created_at"],
            }
        verdict, bad = compare_reservation(expected, actual, clock.now())
        receipt_id = new_id("rcp")
        fields = {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in (actual or {}).items()}
        exe(
            conn,
            "INSERT INTO receipts (receipt_id, session_id, run_id, operation_id, reservation_id, verdict, mismatches, fields, verified_at) "
            "VALUES (:rc, :s, :r, :o, :res, :v, CAST(:m AS jsonb), CAST(:f AS jsonb), :n)",
            rc=receipt_id, s=session_id, r=env.run_id, o=operation_id, res=(actual or {}).get("reservation_id"), v=verdict, m=bad, f=fields, n=clock.now(),
        )
        if verdict == "verified":
            exe(conn, "UPDATE runs SET state = 'completed', needs_step = false, state_reason = 'verified_reservation', updated_at = :n WHERE run_id = :r", n=clock.now(), r=env.run_id)
            summary = "Verified: the sandbox reservation matches the exact action you approved."
            etype = "receipt.verified"
        elif verdict == "mismatch":
            summary = "Mismatch: the sandbox record does not match the approved action (" + ", ".join(bad) + ")."
            etype = "receipt.mismatch"
        else:
            summary = "Unknown: the reservation could not be read back. Not reported as complete."
            etype = "receipt.unknown"
        store.append_event(conn, clock, session_id, etype, "verifier", summary, run_id=env.run_id, object_id=receipt_id,
                           data={"verdict": verdict, "mismatches": bad, "operation_id": operation_id})
    return {"receipt_id": receipt_id, "verdict": verdict, "mismatches": bad, "fields": fields}
