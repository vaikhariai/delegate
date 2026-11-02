import { useState } from "react";
import { clockTime, SIGNAL_LABELS, signalLabel } from "../lib";
import type { DelegateEvent } from "../types";
import { Icon, type IconName } from "./Icon";

type Tone = "neutral" | "ok" | "warn" | "danger" | "accent" | "muted";

interface View {
  icon: IconName;
  tone: Tone;
  title: string;
  text?: string;
  signals?: string[];
  kind: string; // stable testid suffix
}

const HIDDEN_BY_DEFAULT = new Set(["model.started", "model.completed", "action.proposed", "session.created", "mandate.drafted"]);

export function describeEvent(e: DelegateEvent): View {
  const d = e.data ?? {};
  switch (e.type) {
    case "session.created":
      return { icon: "info", tone: "muted", title: "Scenario created", text: e.summary, kind: "session" };
    case "mandate.drafted":
      return { icon: "info", tone: "muted", title: "Draft mandate prepared", text: e.summary, kind: "mandate" };
    case "mandate.changed":
      return { icon: "tag", tone: "neutral", title: "You edited the mandate", text: e.summary, kind: "mandate" };
    case "mandate.activated":
      return { icon: "shieldCheck", tone: "ok", title: "Authority active", text: e.summary, kind: "activated" };
    case "mandate.paused":
      return { icon: "pause", tone: "warn", title: "Agent paused", text: e.summary, kind: "paused" };
    case "mandate.resumed":
      return { icon: "play", tone: "ok", title: "Agent resumed", text: e.summary, kind: "resumed" };
    case "mandate.revoked":
      return { icon: "revoke", tone: "danger", title: "Authority revoked", text: e.summary, kind: "revoked" };
    case "mandate.expired":
      return { icon: "clock", tone: "danger", title: "Authority expired", text: e.summary, kind: "expired" };
    case "run.started":
      return { icon: "user", tone: "neutral", title: "Negotiation opened", text: e.summary, kind: "run" };
    case "buyer.message_received":
      return { icon: "user", tone: "neutral", title: "Buyer message · untrusted input", text: typeof d.text === "string" ? `“${d.text}”` : e.summary, kind: "buyer" };
    case "buyer.offer_accepted":
      return { icon: "hand", tone: "accent", title: "Buyer tapped Accept offer", text: e.summary, kind: "accepted" };
    case "risk.signals_detected":
      return { icon: "flag", tone: "warn", title: "Risk signals detected", signals: Array.isArray(d.signals) ? (d.signals as string[]) : [], kind: "risk" };
    case "model.started":
      return { icon: "bot", tone: "muted", title: "Agent working", text: e.summary, kind: "model" };
    case "model.completed":
      return { icon: "bot", tone: "muted", title: "Agent step finished", text: e.summary, kind: "model" };
    case "model.failed":
      return { icon: "alert", tone: "danger", title: "Model unavailable", text: e.summary, kind: "model-failed" };
    case "action.proposed":
      return { icon: "bot", tone: "muted", title: "Agent proposed", text: e.summary, kind: "proposed" };
    case "action.denied":
    case "action.invalid":
      return { icon: "ban", tone: "danger", title: "Blocked by the server", text: e.summary.replace(/^Blocked:\s*/, ""), kind: "blocked" };
    case "operation.committed":
      if (d.kind === "reserve_item")
        return { icon: "receipt", tone: "accent", title: "Reservation written", text: e.summary, kind: "reserved" };
      return { icon: "send", tone: "ok", title: "Sent to buyer", text: typeof d.text === "string" ? `“${d.text}”` : e.summary, kind: "sent" };
    case "operation.reconciled":
      return { icon: "refresh", tone: "neutral", title: "Operation reconciled", text: e.summary, kind: "reconciled" };
    case "approval.requested":
      return { icon: "hourglass", tone: "accent", title: "Approval requested", text: e.summary, kind: "approval" };
    case "approval.resolved":
      return d.decision === "reject"
        ? { icon: "xCircle", tone: "warn", title: "You rejected", text: e.summary, kind: "rejected" }
        : { icon: "checkCircle", tone: "ok", title: "You approved", text: e.summary, kind: "approved" };
    case "approval.cancelled":
      return { icon: "xCircle", tone: "warn", title: "Approval cancelled", text: e.summary, kind: "approval-cancelled" };
    case "receipt.verified":
      return { icon: "shieldCheck", tone: "ok", title: "Verified", text: e.summary, kind: "verified" };
    case "receipt.mismatch":
      return { icon: "alert", tone: "danger", title: "Mismatch", text: e.summary, kind: "mismatch" };
    case "receipt.unknown":
      return { icon: "question", tone: "warn", title: "Unknown outcome", text: e.summary, kind: "unknown" };
    case "run.state_changed":
      return { icon: "info", tone: "warn", title: "Negotiation status", text: e.summary, kind: "state" };
    default:
      return { icon: "info", tone: "neutral", title: e.type, text: e.summary, kind: "other" };
  }
}

export function Timeline({ events }: { events: DelegateEvent[] }) {
  const [all, setAll] = useState(false);
  const shown = (all ? events : events.filter((e) => !HIDDEN_BY_DEFAULT.has(e.type))).slice().reverse();
  return (
    <div className="timeline-wrap">
      <div className="timeline-head">
        <h2 id="activity-title">Activity</h2>
        <label className="toggle">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} data-testid="timeline-all" />
          <span>Show every agent step</span>
        </label>
      </div>
      {shown.length === 0 ? (
        <p className="muted">Nothing yet. Send a buyer message to start.</p>
      ) : (
        <ol className="timeline" aria-labelledby="activity-title" data-testid="timeline">
          {shown.map((e) => {
            const v = describeEvent(e);
            return (
              <li key={e.seq} className={`tl tl-${v.tone}`} data-testid={`tl-${v.kind}`}>
                <span className="tl-icon" aria-hidden="true">
                  <Icon name={v.icon} size={18} />
                </span>
                <div className="tl-body">
                  <div className="tl-top">
                    <strong>{v.title}</strong>
                    <time dateTime={e.at}>{clockTime(e.at, true)}</time>
                  </div>
                  {v.text ? <p className="tl-text">{v.text}</p> : null}
                  {v.signals?.length ? (
                    <ul className="signal-list">
                      {v.signals.map((s) => (
                        <li key={s} className={`signal signal-${SIGNAL_LABELS[s]?.severity ?? "low"}`}>
                          <Icon name="flag" size={14} /> {signalLabel(s)} <span className="sev">{SIGNAL_LABELS[s]?.severity ?? "low"}</span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <details className="tech">
                    <summary>Technical details</summary>
                    <dl>
                      <dt>Event</dt>
                      <dd>
                        #{e.seq} · {e.type} · {e.actor}
                      </dd>
                      {Object.keys(e.data ?? {}).length ? (
                        <>
                          <dt>Data</dt>
                          <dd>
                            <pre>{JSON.stringify(e.data, null, 2)}</pre>
                          </dd>
                        </>
                      ) : null}
                    </dl>
                  </details>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
