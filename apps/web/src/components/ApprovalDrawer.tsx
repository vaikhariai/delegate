import { useRef, useState } from "react";
import { api, ApiError, newKey } from "../api";
import { mmss, remaining, useNow } from "../lib";
import type { Mandate, PendingApproval } from "../types";
import { Modal } from "./Chrome";
import { Icon } from "./Icon";

const STALE = "This request changed or expired; review the new one.";

export function ApprovalDrawer({
  approval,
  mandate,
  onClose,
  onResolved,
}: {
  approval: PendingApproval;
  mandate: Mandate | null;
  onClose: () => void;
  onResolved: (decision: "approve" | "reject", result: Record<string, unknown> | null) => void;
}) {
  const now = useNow(500);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // One idempotency key per decision on this approval, so a double click or retry never resolves twice.
  const keys = useRef({ approve: newKey(), reject: newKey() });

  const left = remaining(approval.expires_at, now) ?? 0;
  const expired = left <= 0;
  const revisionChanged = mandate !== null && mandate.active_revision !== approval.mandate_revision;
  const notActive = mandate !== null && mandate.status !== "active";
  const disabled = busy !== null || expired || stale || revisionChanged || notActive;
  const holdMin = Math.round((approval.terms?.hold_seconds ?? 0) / 60);
  const delivery = approval.terms?.delivery === "pickup" ? "pickup only" : approval.terms?.delivery ?? "pickup only";
  const payment = approval.terms?.payment_terms === "cash_at_pickup" ? "cash at pickup" : approval.terms?.payment_terms ?? "cash at pickup";

  async function resolve(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      const r = await api.resolveApproval(approval.approval_id, decision, approval.action_hash, keys.current[decision]);
      onResolved(decision, r);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 409 || err.status === 410)) {
        setStale(true);
        setError(`${STALE} (${err.message})`);
      } else if (err instanceof ApiError) setError(err.message);
      else setError("Could not reach the server. Your decision was not recorded; try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal labelledBy="approval-title" describedBy="approval-effect" onClose={onClose} variant="drawer" testid="approval-drawer" initialFocus="[data-testid=approve-button]">
      <div className="drawer-head">
        <p className="eyebrow">
          <Icon name="hourglass" size={16} /> Your approval is needed
        </p>
        <button type="button" className="icon-btn" aria-label="Close and decide later" onClick={onClose} data-testid="approval-close">
          <Icon name="x" />
        </button>
      </div>
      <h2 id="approval-title">Approve this exact reservation?</h2>
      <p id="approval-effect" className="effect" data-testid="approval-effect">
        Reserve <strong>{approval.item_title}</strong> for <strong>{approval.buyer_display_name}</strong> at{" "}
        <strong className="price-inline">{approval.price_display}</strong>, {delivery}, {payment}, held <strong>{holdMin} minutes</strong>.
      </p>

      <dl className="approval-grid">
        <div>
          <dt>Price</dt>
          <dd className="big" data-testid="approval-price">
            {approval.price_display}
          </dd>
        </div>
        <div>
          <dt>Buyer</dt>
          <dd>{approval.buyer_display_name}</dd>
        </div>
        <div>
          <dt>Terms</dt>
          <dd>
            {delivery[0]!.toUpperCase() + delivery.slice(1)} · {payment}
          </dd>
        </div>
        <div>
          <dt>Hold</dt>
          <dd>{holdMin} minutes after you approve</dd>
        </div>
        <div>
          <dt>Under authority</dt>
          <dd data-testid="approval-revision">
            Revision {approval.mandate_revision}
            {revisionChanged ? <span className="badge badge-danger">changed since</span> : <span className="badge badge-ok">current</span>}
          </dd>
        </div>
        <div>
          <dt>This request expires in</dt>
          <dd className={left < 30 ? "countdown urgent" : "countdown"} data-testid="approval-countdown" aria-live="off">
            <Icon name="clock" size={18} /> {expired ? "expired" : mmss(left)}
          </dd>
        </div>
      </dl>

      <p className="hint">
        The buyer tapped Accept offer on this price. When you approve, the server checks your limits again and writes the reservation once.
        It is a sandbox hold, not a sale or payment.
      </p>

      {expired || revisionChanged || notActive || stale ? (
        <p className="stale" role="alert" data-testid="approval-stale">
          <Icon name="alert" /> {error ?? STALE}
        </p>
      ) : error ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="drawer-actions">
        <button type="button" className="btn btn-primary btn-lg" disabled={disabled} onClick={() => void resolve("approve")} data-testid="approve-button">
          <Icon name="checkCircle" /> {busy === "approve" ? "Approving and verifying…" : `Approve ${approval.price_display}`}
        </button>
        <button type="button" className="btn btn-secondary btn-lg" disabled={disabled} onClick={() => void resolve("reject")} data-testid="reject-button">
          <Icon name="xCircle" /> {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>

      <details className="tech">
        <summary>Technical details</summary>
        <dl>
          <dt>Action hash (what you approve)</dt>
          <dd className="mono" data-testid="approval-hash">
            {approval.action_hash}
          </dd>
          <dt>Approval</dt>
          <dd className="mono">{approval.approval_id}</dd>
          <dt>Action</dt>
          <dd className="mono">{approval.action_id}</dd>
          <dt>Buyer acceptance</dt>
          <dd className="mono">{approval.acceptance_event_id}</dd>
        </dl>
      </details>
    </Modal>
  );
}
