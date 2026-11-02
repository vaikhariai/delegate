"""Projections. Each view is built from an explicit field list; private seller data is never selected.

- ``public_context``: what the negotiation model (and baseline B0) sees (POLICY.md Appendix B).
- ``buyer_view``: what the simulated buyer sees.
- ``owner_view``: what the seller sees, including private terms and owner-only activity.
- ``evidence``: an export with the same split.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection

from delegate import store
from delegate.db import one, rows
from delegate.renderers import format_money


def _listing_public(conn: Connection, session_id: str, item_id: str) -> dict[str, Any]:
    r = one(
        conn,
        "SELECT item_id, title, currency, asking_price_minor, public_facts, state FROM listings WHERE session_id = :s AND item_id = :i",
        s=session_id, i=item_id,
    )
    assert r is not None
    return {
        "item_id": r["item_id"],
        "title": r["title"],
        "currency": r["currency"],
        "asking_price_minor": int(r["asking_price_minor"]),
        "public_facts": [{"id": f["id"], "text": f["text"]} for f in r["public_facts"]],
        "state": r["state"],
    }


def _conversation(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    out = []
    for i, m in enumerate(
        rows(conn, "SELECT role, text, offer_id, created_at FROM marketplace_messages WHERE run_id = :r ORDER BY created_at, message_id", r=run_id),
        start=1,
    ):
        out.append({"seq": i, "role": m["role"], "text": m["text"], "offer_id": m["offer_id"], "at": m["created_at"].isoformat()})
    return out


def _open_offer(conn: Connection, run_id: str) -> dict[str, Any] | None:
    o = one(conn, "SELECT offer_id, price_minor, currency, source FROM marketplace_offers WHERE run_id = :r AND status = 'open' ORDER BY created_at DESC LIMIT 1", r=run_id)
    if o is None:
        return None
    return {"offer_id": o["offer_id"], "price_minor": int(o["price_minor"]), "currency": o["currency"], "source": o["source"],
            "display": format_money(int(o["price_minor"]), o["currency"])}


def _acceptances(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    out = []
    for e in rows(
        conn,
        "SELECT e.event_id, e.offer_id, e.used_by_action, o.price_minor, o.currency FROM buyer_events e "
        "JOIN marketplace_offers o ON o.offer_id = e.offer_id WHERE e.run_id = :r AND e.kind = 'offer_acceptance' ORDER BY e.created_at",
        r=run_id,
    ):
        out.append({"acceptance_event_id": e["event_id"], "offer_id": e["offer_id"], "price_minor": int(e["price_minor"]),
                    "currency": e["currency"], "used": e["used_by_action"] is not None})
    return out


def _reservation(conn: Connection, run_id: str) -> dict[str, Any] | None:
    r = one(
        conn,
        "SELECT reservation_id, price_minor, currency, created_at, expires_at, "
        "CASE WHEN status = 'active' AND expires_at <= now() THEN 'expired' ELSE status END AS status "
        "FROM marketplace_reservations WHERE run_id = :r ORDER BY created_at DESC LIMIT 1",
        r=run_id,
    )
    if r is None:
        return None
    return {"reservation_id": r["reservation_id"], "price_minor": int(r["price_minor"]), "currency": r["currency"],
            "status": r["status"], "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat()}


def public_context(conn: Connection, session_id: str, run_id: str) -> dict[str, Any]:
    run = store.get_run(conn, session_id, run_id)
    m = store.mandate_row(conn, session_id)
    snap = store.snapshot(conn, m)
    sent = one(
        conn,
        "SELECT count(*) AS n FROM operations o JOIN proposed_actions pa ON pa.action_id = o.action_id WHERE pa.run_id = :r AND o.kind = 'send_message'",
        r=run_id,
    )
    assert sent is not None
    listing = _listing_public(conn, session_id, run["item_id"])
    listing.pop("state")
    feedback = run["last_feedback"]
    pending = one(conn, "SELECT 1 AS x FROM approvals WHERE run_id = :r AND status = 'pending' AND expires_at > now()", r=run_id) is not None
    return {
        "listing": listing,
        "buyer_display_name": run["buyer_display_name"],
        "conversation": [{k: v for k, v in c.items() if k in ("seq", "role", "text")} for c in _conversation(conn, run_id)],
        "open_offer": {k: v for k, v in (_open_offer(conn, run_id) or {}).items() if k in ("offer_id", "price_minor", "currency")} or None,
        "acceptances": [] if pending else [a for a in _acceptances(conn, run_id) if not a.pop("used")],
        "awaiting_owner_approval": pending,
        "reservation": _reservation(conn, run_id),
        "last_feedback": {"code": feedback["code"], "safe_message": feedback["safe_message"]} if feedback else None,
        "messages_remaining": max(0, snap.max_outbound_messages - int(sent["n"])),
    }


def buyer_view(conn: Connection, session_id: str, run_id: str) -> dict[str, Any]:
    run = store.get_run(conn, session_id, run_id)
    return {
        "run_id": run_id,
        "buyer_display_name": run["buyer_display_name"],
        "listing": _listing_public(conn, session_id, run["item_id"]),
        "conversation": _conversation(conn, run_id),
        "open_offer": _open_offer(conn, run_id),
        "reservation": _reservation(conn, run_id),
        "negotiation_open": run["state"] not in ("completed", "failed", "cancelled"),
    }


def mandate_owner_view(conn: Connection, session_id: str) -> dict[str, Any]:
    m = store.mandate_row(conn, session_id)
    current = store.revision_content(conn, m["mandate_id"], m["current_revision"])
    active = store.revision_content(conn, m["mandate_id"], m["active_revision"]) if m["active_revision"] else None
    return {
        "mandate_id": m["mandate_id"],
        "status": m["status"],
        "current_revision": m["current_revision"],
        "active_revision": m["active_revision"],
        "activated_at": m["activated_at"].isoformat() if m["activated_at"] else None,
        "expires_at": m["expires_at"].isoformat() if m["expires_at"] else None,
        "terms": current,
        "active_terms": active,
        "has_unactivated_changes": m["active_revision"] is not None and m["active_revision"] != m["current_revision"],
        "revoked_at": _last_event_at(conn, session_id, "mandate.revoked"),
        "paused_at": _last_event_at(conn, session_id, "mandate.paused") if m["status"] == "paused" else None,
    }


def _last_event_at(conn: Connection, session_id: str, event_type: str) -> str | None:
    r = one(conn, "SELECT max(occurred_at) AS t FROM audit_events WHERE session_id = :s AND event_type = :e", s=session_id, e=event_type)
    return r["t"].isoformat() if r and r["t"] else None


def owner_run_view(conn: Connection, session_id: str, run_id: str) -> dict[str, Any]:
    run = store.get_run(conn, session_id, run_id)
    approvals = rows(
        conn,
        "SELECT a.approval_id, a.action_id, a.action_hash, a.status, a.created_at, a.expires_at, a.mandate_revision, pa.canonical_body "
        "FROM approvals a JOIN proposed_actions pa ON pa.action_id = a.action_id WHERE a.run_id = :r ORDER BY a.created_at DESC",
        r=run_id,
    )
    import json

    pending = None
    import datetime as _dt

    for a in approvals:
        if a["status"] == "pending" and a["expires_at"] > _dt.datetime.now(_dt.timezone.utc):
            body = json.loads(a["canonical_body"])
            p = body["payload"]
            pending = {
                "approval_id": a["approval_id"], "action_id": a["action_id"], "action_hash": a["action_hash"],
                "expires_at": a["expires_at"].isoformat(), "mandate_revision": a["mandate_revision"],
                "item_id": body["item_id"], "item_title": _listing_public(conn, session_id, body["item_id"])["title"],
                "buyer_id": body["buyer_id"], "buyer_display_name": run["buyer_display_name"],
                "price_minor": p["price_minor"], "currency": p["currency"], "price_display": format_money(p["price_minor"], p["currency"]),
                "terms": body.get("terms"), "acceptance_event_id": p["acceptance_event_id"],
            }
            break
    receipt = one(conn, "SELECT * FROM receipts WHERE run_id = :r ORDER BY verified_at DESC LIMIT 1", r=run_id)
    signals: set[str] = set()
    for ev in rows(conn, "SELECT risk_signals FROM buyer_events WHERE run_id = :r", r=run_id):
        signals.update(ev["risk_signals"] or [])
    return {
        "run_id": run_id,
        "state": run["state"],
        "state_reason": run["state_reason"],
        "buyer_id": run["buyer_id"],
        "buyer_display_name": run["buyer_display_name"],
        "system": run["system"],
        "thinking": bool(run["needs_step"]),
        "conversation": _conversation(conn, run_id),
        "open_offer": _open_offer(conn, run_id),
        "reservation": _reservation(conn, run_id),
        "pending_approval": pending,
        "receipt": None if receipt is None else {
            "receipt_id": receipt["receipt_id"], "verdict": receipt["verdict"], "mismatches": receipt["mismatches"],
            "fields": receipt["fields"], "verified_at": receipt["verified_at"].isoformat(), "operation_id": receipt["operation_id"],
        },
        "risk_signals": sorted(signals),
        "provider_calls": int(run["provider_calls"]),
    }


def events(conn: Connection, session_id: str, after_seq: int, *, run_id: str | None, owner: bool, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT seq, run_id, event_type, actor_type, object_id, summary, data, owner_only, occurred_at FROM audit_events WHERE session_id = :s AND seq > :a"
    params: dict[str, Any] = {"s": session_id, "a": after_seq}
    if run_id is not None:
        sql += " AND (run_id = :r OR run_id IS NULL)"
        params["r"] = run_id
    if not owner:
        sql += " AND owner_only = false"
    sql += " ORDER BY seq LIMIT :l"
    params["l"] = limit
    out = []
    for e in rows(conn, sql, **params):
        out.append({"seq": int(e["seq"]), "run_id": e["run_id"], "type": e["event_type"], "actor": e["actor_type"],
                    "object_id": e["object_id"], "summary": e["summary"], "data": e["data"] if owner else {},
                    "at": e["occurred_at"].isoformat()})
    return out


def evidence(conn: Connection, session_id: str, run_id: str, *, view: str, provider_mode: str, release: str) -> dict[str, Any]:
    if view == "buyer":
        return {"view": "buyer", "evidence_modes": ["CONTROLLED_SANDBOX"], **buyer_view(conn, session_id, run_id)}
    owner = owner_run_view(conn, session_id, run_id)
    actions = rows(
        conn,
        "SELECT action_id, step_no, attempt, origin, kind, status, action_hash, mandate_revision, validation_error, created_at "
        "FROM proposed_actions WHERE run_id = :r ORDER BY step_no, attempt",
        r=run_id,
    )
    decisions = rows(conn, "SELECT action_id, phase, verdict, reason_code, decided_by, checked_at FROM decisions WHERE session_id = :s AND action_id IN (SELECT action_id FROM proposed_actions WHERE run_id = :r) ORDER BY checked_at", s=session_id, r=run_id)
    calls = rows(conn, "SELECT purpose, model, mode, prompt_version, status, http_status, latency_ms, usage, request_id, created_at FROM provider_calls WHERE run_id = :r ORDER BY created_at", r=run_id)
    modes = {"CONTROLLED_SANDBOX"}
    for c in calls:
        modes.add({"live": "LIVE_PROVIDER", "mock": "MOCK_ONLY", "b0": "SYNTHETIC_FIXTURE", "unavailable": "NOT_RUN"}[c["mode"]])
    def iso(d: dict[str, Any]) -> dict[str, Any]:
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}
    return {
        "view": "owner",
        "release": release,
        "provider_mode": provider_mode,
        "evidence_modes": sorted(modes),
        "note": "Sandbox reservation in an application-owned fictional marketplace. Not a sale, payment or handover.",
        "run": owner,
        "mandate": mandate_owner_view(conn, session_id),
        "actions": [iso(a) for a in actions],
        "decisions": [iso(d) for d in decisions],
        "provider_calls": [iso(c) for c in calls],
        "events": events(conn, session_id, 0, run_id=run_id, owner=True, limit=1000),
    }
