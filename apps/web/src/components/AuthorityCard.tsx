import { useState } from "react";
import { api } from "../api";
import { clockTime, MANDATE_STATUS_LABELS, mmss, money, remaining, useNow } from "../lib";
import { useApp } from "../state";
import type { DelegateEvent, Listing, Mandate } from "../types";
import { Modal } from "./Chrome";
import { Icon } from "./Icon";

const STATUS_ICON = { draft: "info", active: "shieldCheck", paused: "pause", revoked: "revoke", expired: "clock" } as const;

export function AuthorityCard({ mandate, listing, events }: { mandate: Mandate; listing: Listing; events: DelegateEvent[] }) {
  const { refreshSession, refreshRun, notify, notifyError, announce } = useApp();
  const now = useNow();
  const [busy, setBusy] = useState<"pause" | "resume" | "revoke" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmedRevokeAt, setConfirmedRevokeAt] = useState<string | null>(null);
  const t = mandate.active_terms ?? mandate.terms;
  const left = remaining(mandate.expires_at, now);
  const ended = mandate.status === "revoked" || mandate.status === "expired";
  const revokedEvent = [...events].reverse().find((e) => e.type === "mandate.revoked");
  const revokedAt = confirmedRevokeAt ?? (revokedEvent?.data?.revoked_at as string | undefined) ?? revokedEvent?.at;
  const facts = listing.public_facts.filter((f) => t.allowed_public_fact_ids.includes(f.id));

  async function act(action: "pause" | "resume" | "revoke") {
    setBusy(action);
    try {
      const r = await api.mandateAction(mandate.mandate_id, action);
      if (action === "revoke" && r.revoked_at) setConfirmedRevokeAt(r.revoked_at);
      await refreshSession();
      await refreshRun();
      if (action === "revoke") {
        const at = r.revoked_at ? clockTime(r.revoked_at, true) : "now";
        announce(`Authority revoked at ${at}, confirmed by the server.`);
        notify("success", `Revoked at ${at} (server-confirmed). The agent can no longer send or reserve. Messages already sent stay sent.`);
      } else if (action === "pause") {
        announce("Agent paused.");
        notify("info", "Paused. The agent cannot send or reserve until you resume.");
      } else {
        announce("Agent resumed.");
        notify("info", "Resumed. The agent replies to the next buyer message.");
      }
    } catch (err) {
      notifyError(err, `Could not ${action} the agent.`);
    } finally {
      setBusy(null);
      setConfirming(false);
    }
  }

  return (
    <section className={`card authority authority-${mandate.status}`} aria-labelledby="authority-title" data-testid="authority-card">
      <div className="authority-top">
        <div className="authority-status">
          <h2 id="authority-title" className="sr-only">
            Agent authority
          </h2>
          <span className={`status-pill status-${mandate.status}`} data-testid="authority-status">
            <Icon name={STATUS_ICON[mandate.status]} size={18} />
            {MANDATE_STATUS_LABELS[mandate.status]}
          </span>
          <span className="authority-meta">
            <span data-testid="authority-revision">Revision {mandate.active_revision ?? mandate.current_revision}</span>
            {mandate.status === "active" || mandate.status === "paused" ? (
              <span className={left !== null && left < 120 ? "urgent" : ""} data-testid="authority-expiry">
                <Icon name="clock" size={16} /> {left === 0 ? "time is up" : `${mmss(left)} left`}
              </span>
            ) : null}
          </span>
        </div>
        <div className="authority-controls" role="group" aria-label="Agent controls">
          {mandate.status === "paused" ? (
            <button type="button" className="btn btn-secondary" onClick={() => void act("resume")} disabled={busy !== null} aria-label="Resume" data-testid="resume-button">
              <Icon name="play" size={18} /> <span className="ctl-label">{busy === "resume" ? "Resuming…" : "Resume"}</span>
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void act("pause")}
              disabled={busy !== null || mandate.status !== "active"}
              aria-label="Pause"
              data-testid="pause-button"
            >
              <Icon name="pause" size={18} /> <span className="ctl-label">{busy === "pause" ? "Pausing…" : "Pause"}</span>
            </button>
          )}
          <button type="button" className="btn btn-danger" onClick={() => setConfirming(true)} disabled={busy !== null || ended} data-testid="revoke-button">
            <Icon name="revoke" size={18} /> Revoke
          </button>
        </div>
      </div>

      {mandate.status === "revoked" ? (
        <p className="revoked-line" role="status" data-testid="revoked-confirmation">
          <Icon name="revoke" size={18} /> {revokedAt ? `Revoked at ${clockTime(revokedAt, true)} · confirmed by the server.` : "Revoked · confirmed by the server."} No further messages or reservations.
        </p>
      ) : mandate.status === "expired" ? (
        <p className="revoked-line" role="status">
          <Icon name="clock" size={18} /> The mandate expired. Start a new scenario to continue.
        </p>
      ) : mandate.status === "paused" ? (
        <p className="paused-line" role="status">
          <Icon name="pause" size={18} /> Paused. The agent cannot send or reserve until you resume.
        </p>
      ) : null}

      <div className="auth-details">
        <dl className="auth-terms">
          <div>
            <dt>Counter range</dt>
            <dd>
              {money(t.min_price_minor)} – {money(t.asking_price_minor)}
              <span className="private-tag" title="Only you see the minimum">
                <Icon name="eyeOff" size={14} /> min is private
              </span>
            </dd>
          </div>
          <div>
            <dt>Terms</dt>
            <dd>Pickup only · cash at pickup</dd>
          </div>
          <div>
            <dt>Reservation</dt>
            <dd>Needs your approval · held {Math.round(t.reservation_hold_seconds / 60)} min</dd>
          </div>
          <div>
            <dt>May share</dt>
            <dd>{facts.length === listing.public_facts.length ? "All public listing facts" : `${facts.length} chosen facts`}</dd>
          </div>
          <div>
            <dt>Never</dt>
            <dd>Ship · share address or phone · QR or advance payment</dd>
          </div>
        </dl>
      </div>

      {confirming ? (
        <Modal labelledBy="revoke-title" describedBy="revoke-desc" onClose={() => setConfirming(false)} testid="revoke-dialog" initialFocus="[data-testid=revoke-cancel]">
          <h2 id="revoke-title">Revoke the agent&rsquo;s authority?</h2>
          <p id="revoke-desc">
            The agent stops at once: no further messages, offers or reservations, and any pending approval is cancelled. Messages already
            sent stay sent. You cannot undo this in this scenario.
          </p>
          <div className="dialog-actions">
            <button type="button" className="btn btn-secondary" onClick={() => setConfirming(false)} data-testid="revoke-cancel">
              Keep the agent working
            </button>
            <button type="button" className="btn btn-danger" onClick={() => void act("revoke")} disabled={busy !== null} data-testid="revoke-confirm">
              <Icon name="revoke" size={18} /> {busy === "revoke" ? "Revoking…" : "Revoke now"}
            </button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
