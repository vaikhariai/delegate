import { useEffect, useState } from "react";
import { api, downloadEvidence } from "../api";
import { Icon, type IconName } from "../components/Icon";
import { clockTime, money, navigate, providerEvidenceMode } from "../lib";
import { useApp } from "../state";

// The verifier's field list (POLICY.md section 8), in reading order: what a seller cares about first, identity checks after.
const BUSINESS_FIELDS: { key: string; label: string; fmt?: (v: unknown, f: Record<string, unknown>) => string }[] = [
  { key: "item_id", label: "Item" },
  { key: "buyer_id", label: "Buyer" },
  { key: "price_minor", label: "Price", fmt: (v, f) => money(Number(v), String(f.currency ?? "USD")) },
  { key: "currency", label: "Currency" },
  { key: "delivery", label: "Delivery", fmt: (v) => (v === "pickup" ? "Pickup only" : String(v)) },
  { key: "payment_terms", label: "Payment", fmt: (v) => (v === "cash_at_pickup" ? "Cash at pickup" : String(v)) },
  { key: "hold_seconds", label: "Hold", fmt: (v) => `${Math.round(Number(v) / 60)} minutes` },
  { key: "status", label: "Reservation status", fmt: (v) => (v === "active" ? "Active hold" : String(v)) },
];
const IDENTITY_FIELDS: { key: string; label: string }[] = [
  { key: "operation_id", label: "Operation" },
  { key: "action_hash", label: "Approved action hash" },
  { key: "session_id", label: "Your session" },
  { key: "run_id", label: "Negotiation" },
];

const MODE_TEXT: Record<string, string> = {
  LIVE_PROVIDER: "Agent steps by the live Meta model",
  MOCK_ONLY: "Agent steps by local rules, not a model",
  CONTROLLED_SANDBOX: "Application-owned marketplace sandbox",
  SYNTHETIC_FIXTURE: "Synthetic fixture",
};

export function Outcome() {
  const { session, sessionLoading, ensureSession, run, runId, meta, notifyError, resetScenario } = useApp();
  const [modes, setModes] = useState<string[] | null>(null);
  const [downloading, setDownloading] = useState<"owner" | "buyer" | null>(null);

  useEffect(() => {
    if (!session && !sessionLoading) void ensureSession();
  }, [session, sessionLoading, ensureSession]);

  const receipt = run?.receipt ?? null;
  useEffect(() => {
    if (!runId || !receipt) return;
    let live = true;
    api
      .evidence(runId, "owner")
      .then((e) => live && setModes(Array.isArray(e.evidence_modes) ? (e.evidence_modes as string[]) : null))
      .catch(() => live && setModes(null));
    return () => {
      live = false;
    };
  }, [runId, receipt]);

  async function download(view: "owner" | "buyer") {
    if (!runId) return;
    setDownloading(view);
    try {
      await downloadEvidence(runId, view);
    } catch (err) {
      notifyError(err, "Could not download the evidence.");
    } finally {
      setDownloading(null);
    }
  }

  if (!run || !runId) {
    return (
      <main id="main" className="page">
        <div className="empty card" data-testid="outcome-empty">
          <Icon name="receipt" size={32} />
          <h1>No outcome yet</h1>
          <p>When a buyer accepts an offer and you approve the reservation, the verified receipt appears here.</p>
          <button type="button" className="btn btn-primary btn-lg" onClick={() => navigate(session?.mandate.status === "draft" ? "/setup" : "/negotiate")}>
            Go to the negotiation <Icon name="arrowRight" />
          </button>
        </div>
      </main>
    );
  }

  const fields = (receipt?.fields ?? {}) as Record<string, unknown>;
  const mismatches = new Set(receipt?.mismatches ?? []);
  const verdict = receipt?.verdict ?? (run.reservation ? "pending" : "none");
  const evidenceModes = modes ?? ["CONTROLLED_SANDBOX", providerEvidenceMode(meta?.provider_mode)];
  const buyerName = run.buyer_display_name;
  const title = session?.listing.title ?? "the laptop";

  const head: Record<string, { icon: IconName; title: string; text: string; tone: string }> = {
    verified: {
      icon: "shieldCheck",
      tone: "ok",
      title: "Reserved in sandbox · Verified",
      text: "The server read the reservation back and every field matches the exact action you approved.",
    },
    mismatch: {
      icon: "alert",
      tone: "danger",
      title: "Mismatch · not verified",
      text: "The sandbox record does not match what you approved. It is not reported as complete.",
    },
    unknown: {
      icon: "question",
      tone: "warn",
      title: "Unknown · not verified",
      text: "The reservation could not be read back. It is not reported as complete.",
    },
    pending: { icon: "hourglass", tone: "neutral", title: "Verifying…", text: "A reservation was written; the receipt is not available yet." },
    none: {
      icon: "info",
      tone: "neutral",
      title: "No reservation in this negotiation",
      text:
        run.state === "cancelled"
          ? "The negotiation ended without a reservation."
          : "Nothing has been reserved yet. A reservation needs the buyer's Accept offer and your approval.",
    },
  };
  const h = head[verdict]!;

  return (
    <main id="main" className="page page-outcome">
      <p className="eyebrow">Step 3 of 3 · Outcome and evidence</p>
      <section className={`outcome-hero tone-${h.tone}`} data-testid="outcome-status" aria-live="polite">
        <Icon name={h.icon} size={44} />
        <div>
          <h1>{h.title}</h1>
          <p>{h.text}</p>
        </div>
      </section>

      {receipt ? (
        <div className="outcome-grid">
          <section className="card receipt" aria-labelledby="receipt-title" data-testid="receipt-panel" data-verdict={receipt.verdict}>
            <h2 id="receipt-title" className="card-title">
              <Icon name="receipt" /> Receipt
            </h2>
            <p className="receipt-summary">
              {title} for <strong>{buyerName}</strong> at <strong>{money(Number(fields.price_minor), String(fields.currency ?? "USD"))}</strong>, held
              until <strong>{clockTime(String(fields.expires_at ?? run.reservation?.expires_at ?? ""))}</strong>.
            </p>
            <ul className="field-checks">
              {BUSINESS_FIELDS.map((f) => (
                <FieldRow key={f.key} label={f.label} value={f.fmt && fields[f.key] !== undefined && fields[f.key] !== null ? f.fmt(fields[f.key], fields) : fields[f.key]} state={fieldState(f.key, fields, mismatches)} testid={`field-${f.key}`} />
              ))}
              <FieldRow
                label="Unexpired when checked"
                value={mismatches.has("expired") ? "Expired" : `Checked ${clockTime(receipt.verified_at, true)}`}
                state={mismatches.has("expired") ? "mismatch" : receipt.verdict === "unknown" ? "unknown" : "match"}
                testid="field-unexpired"
              />
            </ul>
            <details className="tech">
              <summary>Identity checks (technical)</summary>
              <ul className="field-checks compact">
                {IDENTITY_FIELDS.map((f) => (
                  <FieldRow key={f.key} label={f.label} value={fields[f.key]} state={fieldState(f.key, fields, mismatches)} mono testid={`field-${f.key}`} />
                ))}
              </ul>
              <p className="muted small">
                Receipt {receipt.receipt_id} · verified at {clockTime(receipt.verified_at, true)}
              </p>
            </details>
          </section>

          <aside className="outcome-side">
            <section className="card" aria-labelledby="prov-title">
              <h2 id="prov-title" className="card-title">
                <Icon name="info" /> Provenance
              </h2>
              <ul className="prov" data-testid="provenance">
                {evidenceModes.map((m) => (
                  <li key={m}>
                    <span className={`prov-chip prov-${m.toLowerCase()}`} data-testid={`prov-${m}`}>
                      {m}
                    </span>
                    <span className="muted small">{MODE_TEXT[m] ?? ""}</span>
                  </li>
                ))}
              </ul>
              <p className="hint">This is a sandbox hold on a fictional item. It is not a sale, a payment or a handover.</p>
            </section>
            <section className="card" aria-labelledby="ev-title">
              <h2 id="ev-title" className="card-title">
                <Icon name="download" /> Evidence
              </h2>
              <p className="hint">The owner export includes every proposal, gate decision and event. The buyer export contains only what the buyer could see.</p>
              <div className="stack">
                <button type="button" className="btn btn-primary block" onClick={() => void download("owner")} disabled={downloading !== null} data-testid="download-owner-evidence">
                  <Icon name="download" /> {downloading === "owner" ? "Preparing…" : "Download owner evidence (JSON)"}
                </button>
                <button type="button" className="btn btn-secondary block" onClick={() => void download("buyer")} disabled={downloading !== null} data-testid="download-buyer-evidence">
                  <Icon name="download" /> {downloading === "buyer" ? "Preparing…" : "Download buyer evidence (JSON)"}
                </button>
              </div>
            </section>
          </aside>
        </div>
      ) : null}

      <div className="row gap wrap outcome-actions">
        <button type="button" className="btn btn-secondary" onClick={() => navigate("/negotiate")} data-testid="back-to-negotiation">
          Back to the negotiation
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => navigate("/evaluation")}>
          <Icon name="chart" /> See the evaluation
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => void resetScenario()} data-testid="outcome-reset">
          <Icon name="refresh" /> Start a new scenario
        </button>
      </div>
    </main>
  );
}

function fieldState(key: string, fields: Record<string, unknown>, mismatches: Set<string>): "match" | "mismatch" | "unknown" {
  if (mismatches.has(key)) return fields[key] === undefined || fields[key] === null ? "unknown" : "mismatch";
  if (fields[key] === undefined || fields[key] === null) return "unknown";
  return "match";
}

function FieldRow({ label, value, state, mono, testid }: { label: string; value: unknown; state: "match" | "mismatch" | "unknown"; mono?: boolean; testid: string }) {
  const icon: IconName = state === "match" ? "checkCircle" : state === "mismatch" ? "xCircle" : "question";
  const word = state === "match" ? "Matches" : state === "mismatch" ? "Does not match" : "Unknown";
  const shown = value === undefined || value === null ? "not readable" : String(value);
  return (
    <li className={`fc fc-${state}`} data-testid={testid} data-state={state}>
      <Icon name={icon} size={20} />
      <span className="fc-label">{label}</span>
      <span className={`fc-value ${mono ? "mono" : ""}`}>{shown}</span>
      <span className="fc-state">{word}</span>
    </li>
  );
}
