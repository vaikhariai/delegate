"""Sessions, audit events, mandates, runs, buyer turns and acceptances.

Lock order (every transaction that takes more than one of these): mandates -> listings -> runs -> demo_sessions.
Audit event sequence numbers are allocated from demo_sessions.event_seq inside the same transaction, so the
event order is the commit order of each session's state changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine

from delegate import risk
from delegate.clock import Clock
from delegate.db import exe, new_id, one, rows, tx
from delegate.policy import MandateSnapshot

ROOT = Path(__file__).resolve().parents[3]
FIXTURE: dict[str, Any] = json.loads((ROOT / "fixtures" / "demo_case.json").read_text())
ITEM_ID: str = FIXTURE["listing"]["item_id"]
MAX_BUYER_TEXT = 600


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_ip(ip: str) -> str:
    return hashlib.sha256(f"delegate-ip:{ip}".encode()).hexdigest()[:24]


# ---------------------------------------------------------------- audit events


def append_event(
    conn: Connection,
    clock: Clock,
    session_id: str,
    event_type: str,
    actor: str,
    summary: str,
    *,
    run_id: str | None = None,
    object_id: str | None = None,
    data: dict[str, Any] | None = None,
    owner_only: bool = False,
) -> int:
    seq_row = one(
        conn,
        "UPDATE demo_sessions SET event_seq = event_seq + 1 WHERE session_id = :s RETURNING event_seq",
        s=session_id,
    )
    assert seq_row is not None
    seq = int(seq_row["event_seq"])
    exe(
        conn,
        "INSERT INTO audit_events (session_id, seq, run_id, event_type, actor_type, object_id, summary, data, "
        "owner_only, occurred_at) VALUES (:s, :seq, :r, :t, :a, :o, :sum, CAST(:d AS jsonb), :own, :at)",
        s=session_id,
        seq=seq,
        r=run_id,
        t=event_type,
        a=actor,
        o=object_id,
        sum=summary,
        d=data or {},
        own=owner_only,
        at=clock.now(),
    )
    return seq


# ---------------------------------------------------------------- sessions


@dataclass(frozen=True)
class Principal:
    session_id: str

    @property
    def owner(self) -> str:
        return f"owner:{self.session_id}"


def default_mandate_content() -> dict[str, Any]:
    d = FIXTURE["mandate_defaults"]
    return {
        "currency": FIXTURE["listing"]["currency"],
        "asking_price_minor": FIXTURE["listing"]["asking_price_minor"],
        "min_price_minor": FIXTURE["seller_private"]["min_price_minor"],
        "delivery": d["delivery"],
        "payment_terms": d["payment_terms"],
        "allowed_actions": list(d["allowed_actions"]),
        "allowed_public_fact_ids": [f["id"] for f in FIXTURE["listing"]["public_facts"]],
        "reservation_requires_owner_approval": d["reservation_requires_owner_approval"],
        "reservation_hold_seconds": d["reservation_hold_seconds"],
        "max_reservations": d["max_reservations"],
        "max_outbound_messages": d["max_outbound_messages"],
        "lifetime_seconds": d["mandate_lifetime_seconds"],
    }


def create_session(engine: Engine, clock: Clock, ip: str, per_ip_per_hour: int, ttl_s: int) -> tuple[str, str]:
    now = clock.now()
    ip_h = hash_ip(ip)
    window = now.replace(minute=0, second=0, microsecond=0)
    token = secrets.token_urlsafe(32)
    session_id = new_id("ses")
    with tx(engine) as conn:
        bucket = one(
            conn,
            "INSERT INTO rate_buckets (ip_hash, window_start, count) VALUES (:i, :w, 1) "
            "ON CONFLICT (ip_hash, window_start) DO UPDATE SET count = rate_buckets.count + 1 RETURNING count",
            i=ip_h,
            w=window,
        )
        assert bucket is not None
        if int(bucket["count"]) > per_ip_per_hour:
            raise ApiError(429, "RATE_LIMITED", "Too many new demo sessions from this network. Try again later.", True)
        exe(
            conn,
            "INSERT INTO demo_sessions (session_id, token_hash, ip_hash, created_at, expires_at) "
            "VALUES (:s, :t, :i, :c, :e)",
            s=session_id,
            t=_hash_token(token),
            i=ip_h,
            c=now,
            e=now + dt.timedelta(seconds=ttl_s),
        )
        lst = FIXTURE["listing"]
        exe(
            conn,
            "INSERT INTO listings (session_id, item_id, title, currency, asking_price_minor, public_facts) "
            "VALUES (:s, :i, :t, :c, :a, CAST(:f AS jsonb))",
            s=session_id,
            i=lst["item_id"],
            t=lst["title"],
            c=lst["currency"],
            a=lst["asking_price_minor"],
            f=lst["public_facts"],
        )
        priv = FIXTURE["seller_private"]
        exe(
            conn,
            "INSERT INTO seller_private (session_id, item_id, exact_address, phone, notes) VALUES (:s, :i, :a, :p, :n)",
            s=session_id,
            i=lst["item_id"],
            a=priv["exact_address"],
            p=priv["phone"],
            n=priv["notes"],
        )
        mandate_id = new_id("man")
        exe(
            conn,
            "INSERT INTO mandates (mandate_id, session_id, item_id, status, current_revision, updated_at) "
            "VALUES (:m, :s, :i, 'draft', 1, :n)",
            m=mandate_id,
            s=session_id,
            i=lst["item_id"],
            n=now,
        )
        exe(
            conn,
            "INSERT INTO mandate_revisions (mandate_id, revision, content, created_at) VALUES (:m, 1, CAST(:c AS jsonb), :n)",
            m=mandate_id,
            c=default_mandate_content(),
            n=now,
        )
        append_event(conn, clock, session_id, "session.created", "system", "New fictional demo scenario created.")
        append_event(
            conn, clock, session_id, "mandate.drafted", "system", "Draft mandate prepared for your review.",
            object_id=mandate_id,
        )
    return session_id, token


def authenticate(engine: Engine, clock: Clock, token: str | None) -> Principal:
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Start a demo session first.")
    with engine.connect() as conn:
        r = one(
            conn,
            "SELECT session_id, expires_at, deleted_at FROM demo_sessions WHERE token_hash = :t",
            t=_hash_token(token),
        )
    if r is None or r["deleted_at"] is not None:
        raise ApiError(401, "UNAUTHENTICATED", "This demo session does not exist. Start a new one.")
    if clock.now() >= r["expires_at"]:
        raise ApiError(401, "SESSION_EXPIRED", "This demo session expired. Start a new one.")
    return Principal(r["session_id"])


def delete_session(engine: Engine, session_id: str) -> None:
    with tx(engine) as conn:
        exe(conn, "DELETE FROM provider_calls WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM receipts WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM operations WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM approvals WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM decisions WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM marketplace_reservations WHERE session_id = :s", s=session_id)
        exe(conn, "DELETE FROM demo_sessions WHERE session_id = :s", s=session_id)


def cleanup_expired(engine: Engine, clock: Clock) -> int:
    with engine.connect() as conn:
        ids = [r["session_id"] for r in rows(conn, "SELECT session_id FROM demo_sessions WHERE expires_at < :n", n=clock.now())]
    for sid in ids:
        delete_session(engine, sid)
    with tx(engine) as conn:
        exe(conn, "DELETE FROM rate_buckets WHERE window_start < :c", c=clock.now() - dt.timedelta(hours=2))
    return len(ids)


# ---------------------------------------------------------------- mandates


def mandate_row(conn: Connection, session_id: str, *, for_update: bool = False) -> dict[str, Any]:
    r = one(
        conn,
        "SELECT * FROM mandates WHERE session_id = :s" + (" FOR UPDATE" if for_update else ""),
        s=session_id,
    )
    if r is None:
        raise ApiError(404, "NOT_FOUND", "Mandate not found.")
    return r


def revision_content(conn: Connection, mandate_id: str, revision: int) -> dict[str, Any]:
    r = one(conn, "SELECT content FROM mandate_revisions WHERE mandate_id = :m AND revision = :r", m=mandate_id, r=revision)
    assert r is not None
    content: dict[str, Any] = r["content"]
    return content


def expire_if_due(conn: Connection, clock: Clock, m: dict[str, Any]) -> dict[str, Any]:
    """Persist expiry for a locked mandate whose lifetime has passed."""
    if m["status"] in ("active", "paused") and m["expires_at"] is not None and clock.now() >= m["expires_at"]:
        exe(conn, "UPDATE mandates SET status = 'expired', updated_at = :n WHERE mandate_id = :m", n=clock.now(), m=m["mandate_id"])
        _cancel_pending_approvals(conn, clock, m["session_id"], "mandate expired")
        append_event(conn, clock, m["session_id"], "mandate.expired", "system", "The mandate expired.", object_id=m["mandate_id"])
        m = {**m, "status": "expired"}
    return m


def snapshot(conn: Connection, m: dict[str, Any]) -> MandateSnapshot:
    rev = m["active_revision"] or m["current_revision"]
    c = revision_content(conn, m["mandate_id"], rev)
    return MandateSnapshot(
        mandate_id=m["mandate_id"],
        status=m["status"],
        revision=rev,
        currency=c["currency"],
        asking_price_minor=c["asking_price_minor"],
        min_price_minor=c["min_price_minor"],
        allowed_actions=frozenset(c["allowed_actions"]),
        allowed_public_fact_ids=frozenset(c["allowed_public_fact_ids"]),
        delivery=c["delivery"],
        payment_terms=c["payment_terms"],
        reservation_requires_owner_approval=c["reservation_requires_owner_approval"],
        reservation_hold_seconds=c["reservation_hold_seconds"],
        max_reservations=c["max_reservations"],
        max_outbound_messages=c["max_outbound_messages"],
        expires_at=m["expires_at"],
    )


ALLOWED_EDIT_FIELDS = {
    "min_price_minor",
    "asking_price_minor",
    "allowed_public_fact_ids",
    "reservation_hold_seconds",
    "max_outbound_messages",
}


def validate_terms(content: dict[str, Any]) -> dict[str, Any]:
    """Strict validation of owner-edited terms. Returns the content or raises ApiError(422)."""

    def bad(msg: str) -> ApiError:
        return ApiError(422, "INVALID_MANDATE", msg)

    for k in ("min_price_minor", "asking_price_minor", "reservation_hold_seconds", "max_outbound_messages"):
        v = content.get(k)
        if type(v) is not int:
            raise bad(f"{k} must be a whole number")
    if content["min_price_minor"] <= 0:
        raise bad("The minimum price must be above zero.")
    if content["min_price_minor"] > content["asking_price_minor"]:
        raise bad("The minimum price cannot be above the asking price.")
    if content["asking_price_minor"] > 10_000_000:
        raise bad("The asking price is too large for this demo.")
    if not 300 <= content["reservation_hold_seconds"] <= 3600:
        raise bad("The hold must be between 5 and 60 minutes.")
    if not 1 <= content["max_outbound_messages"] <= 20:
        raise bad("The message limit must be between 1 and 20.")
    facts = content.get("allowed_public_fact_ids")
    known = {f["id"] for f in FIXTURE["listing"]["public_facts"]}
    if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts) or not set(facts) <= known:
        raise bad("Allowed facts must be chosen from the listing's public facts.")
    if content.get("delivery") != "pickup_only" or content.get("payment_terms") != "cash_at_pickup":
        raise bad("This demo supports pickup with cash at pickup only.")
    if content.get("reservation_requires_owner_approval") is not True:
        raise bad("Reservations always need your approval in this demo.")
    return content


def edit_mandate(engine: Engine, clock: Clock, p: Principal, if_match: int, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - ALLOWED_EDIT_FIELDS
    if unknown:
        raise ApiError(422, "INVALID_MANDATE", f"These fields cannot be edited: {sorted(unknown)}")
    with tx(engine) as conn:
        m = mandate_row(conn, p.session_id, for_update=True)
        m = expire_if_due(conn, clock, m)
        if m["status"] in ("revoked", "expired"):
            raise ApiError(409, "MANDATE_TERMINAL", "This mandate has ended. Start a new scenario to set new terms.")
        if if_match != m["current_revision"]:
            raise ApiError(409, "REVISION_CONFLICT", "The mandate changed. Reload and review the latest version.")
        content = {**revision_content(conn, m["mandate_id"], m["current_revision"]), **changes}
        validate_terms(content)
        new_rev = m["current_revision"] + 1
        exe(
            conn,
            "INSERT INTO mandate_revisions (mandate_id, revision, content, created_at) VALUES (:m, :r, CAST(:c AS jsonb), :n)",
            m=m["mandate_id"],
            r=new_rev,
            c=content,
            n=clock.now(),
        )
        exe(conn, "UPDATE mandates SET current_revision = :r, updated_at = :n WHERE mandate_id = :m", r=new_rev, n=clock.now(), m=m["mandate_id"])
        append_event(
            conn, clock, p.session_id, "mandate.changed", "owner",
            f"You drafted revision {new_rev}. It takes effect when you activate it.",
            object_id=m["mandate_id"], data={"revision": new_rev, "fields": sorted(changes)}, owner_only=True,
        )
    return {"mandate_id": m["mandate_id"], "revision": new_rev}


def _cancel_pending_approvals(conn: Connection, clock: Clock, session_id: str, why: str) -> int:
    pend = rows(conn, "SELECT approval_id, action_id, run_id FROM approvals WHERE session_id = :s AND status IN ('pending', 'approved') AND consumed_at IS NULL", s=session_id)
    for a in pend:
        exe(conn, "UPDATE approvals SET status = 'cancelled', resolved_at = :n WHERE approval_id = :a", n=clock.now(), a=a["approval_id"])
        exe(conn, "UPDATE proposed_actions SET status = 'cancelled' WHERE action_id = :a AND status = 'waiting_approval'", a=a["action_id"])
        append_event(
            conn, clock, session_id, "approval.cancelled", "system", f"Pending approval cancelled: {why}.",
            run_id=a["run_id"], object_id=a["approval_id"], owner_only=True,
        )
    return len(pend)


def activate(engine: Engine, clock: Clock, p: Principal, revision: int, lifetime_s: int) -> dict[str, Any]:
    with tx(engine) as conn:
        m = mandate_row(conn, p.session_id, for_update=True)
        m = expire_if_due(conn, clock, m)
        if m["status"] in ("revoked", "expired"):
            raise ApiError(409, "MANDATE_TERMINAL", "This mandate has ended. Start a new scenario.")
        if revision != m["current_revision"]:
            raise ApiError(409, "REVISION_CONFLICT", "You reviewed an older version. Reload and review the latest.")
        content = validate_terms(revision_content(conn, m["mandate_id"], revision))
        now = clock.now()
        expires = now + dt.timedelta(seconds=min(lifetime_s, int(content["lifetime_seconds"])))
        cancelled = 0
        if m["active_revision"] is not None and m["active_revision"] != revision:
            cancelled = _cancel_pending_approvals(conn, clock, p.session_id, "your permission changed")
            # Runs blocked on the old revision get a fresh look under the new terms.
            exe(conn, "UPDATE runs SET state = 'awaiting_buyer', updated_at = :n WHERE session_id = :s AND state = 'waiting_approval'", n=now, s=p.session_id)
        exe(
            conn,
            "UPDATE mandates SET status = 'active', active_revision = :r, activated_at = :n, expires_at = :e, updated_at = :n "
            "WHERE mandate_id = :m",
            r=revision, n=now, e=expires, m=m["mandate_id"],
        )
        exe(conn, "UPDATE mandate_revisions SET activated_at = :n WHERE mandate_id = :m AND revision = :r", n=now, m=m["mandate_id"], r=revision)
        append_event(
            conn, clock, p.session_id, "mandate.activated", "owner",
            f"You activated revision {revision}. The agent may negotiate until {expires.strftime('%H:%M UTC')}.",
            object_id=m["mandate_id"], data={"revision": revision, "expires_at": expires.isoformat(), "cancelled_approvals": cancelled},
        )
    return {"mandate_id": m["mandate_id"], "revision": revision, "expires_at": expires.isoformat()}


def set_status(engine: Engine, clock: Clock, p: Principal, action: str) -> dict[str, Any]:
    """pause | resume | revoke, under the mandate lock."""
    with tx(engine) as conn:
        m = mandate_row(conn, p.session_id, for_update=True)
        m = expire_if_due(conn, clock, m)
        now = clock.now()
        if action == "revoke":
            if m["status"] in ("revoked", "expired"):
                return {"status": m["status"], "changed": False}
            exe(conn, "UPDATE mandates SET status = 'revoked', updated_at = :n WHERE mandate_id = :m", n=now, m=m["mandate_id"])
            n_cancel = _cancel_pending_approvals(conn, clock, p.session_id, "you revoked authority")
            exe(
                conn,
                "UPDATE runs SET state = 'cancelled', state_reason = 'mandate_revoked', needs_step = false, updated_at = :n "
                "WHERE session_id = :s AND state NOT IN ('completed', 'failed', 'cancelled')",
                n=now, s=p.session_id,
            )
            seq = append_event(
                conn, clock, p.session_id, "mandate.revoked", "owner",
                "You revoked the agent's authority. No further messages or reservations can be made. "
                "Messages already sent stay sent.",
                object_id=m["mandate_id"], data={"cancelled_approvals": n_cancel, "revoked_at": now.isoformat()},
            )
            return {"status": "revoked", "changed": True, "revoked_at": now.isoformat(), "event_seq": seq}
        if action == "pause":
            if m["status"] != "active":
                raise ApiError(409, "MANDATE_NOT_ACTIVE", "Only an active mandate can be paused.")
            exe(conn, "UPDATE mandates SET status = 'paused', updated_at = :n WHERE mandate_id = :m", n=now, m=m["mandate_id"])
            append_event(conn, clock, p.session_id, "mandate.paused", "owner", "You paused the agent. It cannot send or reserve until you resume.", object_id=m["mandate_id"])
            return {"status": "paused", "changed": True}
        if action == "resume":
            if m["status"] != "paused":
                raise ApiError(409, "MANDATE_NOT_PAUSED", "Only a paused mandate can be resumed.")
            exe(conn, "UPDATE mandates SET status = 'active', updated_at = :n WHERE mandate_id = :m", n=now, m=m["mandate_id"])
            append_event(conn, clock, p.session_id, "mandate.resumed", "owner", "You resumed the agent.", object_id=m["mandate_id"])
            return {"status": "active", "changed": True}
    raise ApiError(400, "BAD_ACTION", "Unknown mandate action.")


# ---------------------------------------------------------------- runs, buyer turns, acceptances


def get_run(conn: Connection, session_id: str, run_id: str, *, for_update: bool = False) -> dict[str, Any]:
    r = one(conn, "SELECT * FROM runs WHERE session_id = :s AND run_id = :r" + (" FOR UPDATE" if for_update else ""), s=session_id, r=run_id)
    if r is None:
        raise ApiError(404, "NOT_FOUND", "Negotiation not found.")
    return r


def start_run(engine: Engine, clock: Clock, p: Principal, buyer_id: str, system: str = "delegate") -> dict[str, Any]:
    buyers = {b["buyer_id"]: b["display_name"] for b in FIXTURE["buyers"]}
    if buyer_id not in buyers:
        raise ApiError(422, "UNKNOWN_BUYER", "Pick one of the demo buyers.")
    with tx(engine) as conn:
        m = mandate_row(conn, p.session_id, for_update=True)
        m = expire_if_due(conn, clock, m)
        if m["status"] != "active":
            raise ApiError(409, "MANDATE_NOT_ACTIVE", "Activate the mandate before the agent can negotiate.")
        existing = one(conn, "SELECT run_id FROM runs WHERE session_id = :s AND buyer_id = :b AND state NOT IN ('completed', 'failed', 'cancelled')", s=p.session_id, b=buyer_id)
        if existing:
            return {"run_id": existing["run_id"], "created": False}
        listing = one(conn, "SELECT * FROM listings WHERE session_id = :s AND item_id = :i", s=p.session_id, i=m["item_id"])
        assert listing is not None
        now = clock.now()
        run_id = new_id("run")
        exe(
            conn,
            "INSERT INTO runs (run_id, session_id, mandate_id, item_id, buyer_id, buyer_display_name, system, state, "
            "created_at, updated_at) VALUES (:r, :s, :m, :i, :b, :bn, :sys, 'awaiting_buyer', :n, :n)",
            r=run_id, s=p.session_id, m=m["mandate_id"], i=m["item_id"], b=buyer_id, bn=buyers[buyer_id], sys=system, n=now,
        )
        exe(
            conn,
            "INSERT INTO marketplace_offers (offer_id, session_id, run_id, item_id, price_minor, currency, status, source, created_at) "
            "VALUES (:o, :s, :r, :i, :p, :c, 'open', 'listing', :n)",
            o=f"offer_list_{run_id}", s=p.session_id, r=run_id, i=m["item_id"], p=listing["asking_price_minor"], c=listing["currency"], n=now,
        )
        append_event(conn, clock, p.session_id, "run.started", "owner", f"Negotiation opened with {buyers[buyer_id]}.", run_id=run_id, object_id=run_id)
    return {"run_id": run_id, "created": True}


def buyer_turn(engine: Engine, clock: Clock, p: Principal, run_id: str, text: str, max_turns: int) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ApiError(422, "EMPTY_MESSAGE", "Type a message first.")
    if len(text) > MAX_BUYER_TEXT:
        raise ApiError(422, "MESSAGE_TOO_LONG", f"Keep buyer messages under {MAX_BUYER_TEXT} characters.")
    signals = risk.scan(text)
    with tx(engine) as conn:
        run = get_run(conn, p.session_id, run_id, for_update=True)
        if run["state"] in ("completed", "failed", "cancelled"):
            raise ApiError(409, "RUN_ENDED", "This negotiation has ended. Start a new one.")
        if run["state"] == "paused":
            raise ApiError(409, "RUN_PAUSED", "The seller paused the agent.")
        count = one(conn, "SELECT count(*) AS n FROM buyer_events WHERE run_id = :r AND kind = 'message'", r=run_id)
        assert count is not None
        if int(count["n"]) >= max_turns:
            raise ApiError(429, "TURN_LIMIT", "This demo negotiation reached its message limit. Start a new scenario.")
        now = clock.now()
        ev = new_id("bev")
        exe(
            conn,
            "INSERT INTO buyer_events (event_id, session_id, run_id, kind, text, risk_signals, created_at) "
            "VALUES (:e, :s, :r, 'message', :t, CAST(:sig AS jsonb), :n)",
            e=ev, s=p.session_id, r=run_id, t=text, sig=signals, n=now,
        )
        exe(
            conn,
            "INSERT INTO marketplace_messages (message_id, session_id, run_id, role, text, buyer_event_id, created_at) "
            "VALUES (:m, :s, :r, 'buyer', :t, :e, :n)",
            m=new_id("msg"), s=p.session_id, r=run_id, t=text, e=ev, n=now,
        )
        new_state = run["state"] if run["state"] == "waiting_approval" else "running"
        exe(conn, "UPDATE runs SET needs_step = true, state = :st, updated_at = :n WHERE run_id = :r", st=new_state, n=now, r=run_id)
        exe(conn, "UPDATE demo_sessions SET buyer_turns = buyer_turns + 1 WHERE session_id = :s", s=p.session_id)
        append_event(
            conn, clock, p.session_id, "buyer.message_received", "buyer", "Buyer message received (untrusted input).",
            run_id=run_id, object_id=ev, data={"text": text, "risk_signals": signals},
        )
        if signals:
            append_event(
                conn, clock, p.session_id, "risk.signals_detected", "system",
                "Risk signals in buyer message: " + ", ".join(f"{s} ({risk.severity(s)})" for s in signals) + ".",
                run_id=run_id, object_id=ev, data={"signals": signals}, owner_only=True,
            )
    return {"event_id": ev, "risk_signals": signals}


def accept_offer(engine: Engine, clock: Clock, p: Principal, run_id: str, offer_id: str) -> dict[str, Any]:
    with tx(engine) as conn:
        run = get_run(conn, p.session_id, run_id, for_update=True)
        if run["state"] in ("completed", "failed", "cancelled"):
            raise ApiError(409, "RUN_ENDED", "This negotiation has ended.")
        offer = one(conn, "SELECT * FROM marketplace_offers WHERE offer_id = :o AND run_id = :r AND session_id = :s", o=offer_id, r=run_id, s=p.session_id)
        if offer is None:
            raise ApiError(404, "NOT_FOUND", "Offer not found.")
        if offer["status"] != "open":
            raise ApiError(409, "OFFER_NOT_OPEN", "That offer is no longer open. Accept the latest offer.")
        dup = one(conn, "SELECT event_id FROM buyer_events WHERE run_id = :r AND kind = 'offer_acceptance' AND offer_id = :o", r=run_id, o=offer_id)
        if dup:
            return {"event_id": dup["event_id"], "created": False}
        now = clock.now()
        ev = new_id("acc")
        exe(
            conn,
            "INSERT INTO buyer_events (event_id, session_id, run_id, kind, offer_id, created_at) VALUES (:e, :s, :r, 'offer_acceptance', :o, :n)",
            e=ev, s=p.session_id, r=run_id, o=offer_id, n=now,
        )
        from delegate.renderers import format_money

        price = format_money(int(offer["price_minor"]), offer["currency"])
        exe(
            conn,
            "INSERT INTO marketplace_messages (message_id, session_id, run_id, role, text, buyer_event_id, offer_id, created_at) "
            "VALUES (:m, :s, :r, 'system', :t, :e, :o, :n)",
            m=new_id("msg"), s=p.session_id, r=run_id, t=f"Buyer accepted the offer of {price} (explicit sandbox acceptance).", e=ev, o=offer_id, n=now,
        )
        exe(conn, "UPDATE runs SET needs_step = true, state = 'running', updated_at = :n WHERE run_id = :r", n=now, r=run_id)
        append_event(
            conn, clock, p.session_id, "buyer.offer_accepted", "buyer", f"Buyer explicitly accepted the offer of {price}.",
            run_id=run_id, object_id=ev, data={"offer_id": offer_id, "price_minor": int(offer["price_minor"]), "currency": offer["currency"]},
        )
    return {"event_id": ev, "created": True}
