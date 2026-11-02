import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Icon, type IconName } from "../components/Icon";
import { money, navigate } from "../lib";
import type { GateTrace, GateVerdict, LabCase, LabCaseList } from "../types";

// Plain-language meaning of each verdict and reason code (docs/POLICY.md sections 4 and 7).
export const VERDICT_LABELS: Record<GateVerdict, string> = {
  allow: "Allow",
  deny: "Deny",
  invalid: "Invalid",
  approval_required: "Needs approval",
};

const REASONS: Record<string, string> = {
  SCHEMA_INVALID: "The proposal did not follow the action format: a missing or extra field, a wrong type or an unknown value.",
  OUT_OF_SCOPE: "The action points at a session, negotiation or item it does not belong to.",
  FENCE_STALE: "A newer worker owns this negotiation, so this stale step cannot act.",
  RUN_NOT_LIVE: "The negotiation has already ended.",
  MANDATE_NOT_ACTIVE: "The owner has not activated the mandate.",
  MANDATE_PAUSED: "The owner paused the agent.",
  MANDATE_REVOKED: "The owner revoked the agent's authority.",
  MANDATE_EXPIRED: "The mandate's time limit has passed.",
  STALE_REVISION: "The action was built against an older version of the owner's limits.",
  ACTION_EXPIRED: "The action waited too long and expired.",
  ACTION_NOT_ALLOWED: "The mandate does not allow this kind of action.",
  BUDGET_EXHAUSTED: "The message or reservation limit for this negotiation is used up.",
  UNSUPPORTED_CURRENCY: "The listing is priced in US dollars; other currencies are never converted.",
  PRICE_OUT_OF_BOUNDS: "The price is outside the owner's range: below the private minimum or above the asking price.",
  DISCLOSURE_REFERENCE_FORBIDDEN: "It cites a fact that is not on the owner's list of public facts.",
  TERMS_NOT_PERMITTED: "Only pickup with cash at pickup, held for the owner's hold time, is allowed.",
  SCAM_RISK: "This buyer raised a payment or authority scam signal, so no reservation is possible, even with approval.",
  ACCEPTANCE_MISSING: "There is no explicit Accept offer from this buyer behind the reservation.",
  ACCEPTANCE_MISMATCH: "The buyer's acceptance does not match: wrong offer, wrong price, not open, or already used.",
  ITEM_UNAVAILABLE: "The item is already reserved.",
  DUPLICATE: "This action was already carried out; the stored result is returned and nothing happens twice.",
  APPROVAL_REQUIRED: "Everything else passed; the owner must approve this exact reservation first.",
  APPROVAL_EXPIRED: "The owner's approval expired before it was used.",
  APPROVAL_MISMATCH: "The approval was for a different action, revision or owner.",
};

export function reasonText(trace: Pick<GateTrace, "verdict" | "reason_code">): string {
  if (trace.reason_code && REASONS[trace.reason_code]) return REASONS[trace.reason_code]!;
  if (trace.verdict === "allow") return "Every check passed. The server would carry this action out.";
  return trace.reason_code ?? "";
}

const KEY_LABELS: Record<string, string> = {
  "mandate.status": "Mandate status",
  "mandate.expires_in_s": "Mandate expires in (s)",
  "mandate.min_price_minor": "Private minimum",
  "mandate.asking_price_minor": "Asking price",
  "mandate.allowed_actions": "Allowed actions",
  "mandate.allowed_public_fact_ids": "Public facts the agent may cite",
  "mandate.max_outbound_messages": "Message limit",
  "mandate.max_reservations": "Reservation limit",
  "mandate.reservation_hold_seconds": "Hold (s)",
  "action.revision_offset": "Action built against revision offset",
  "action.expires_in_s": "Action expires in (s)",
  "action.session_matches": "Action session matches",
  "action.run_matches": "Action run matches",
  "action.item_matches": "Action item matches",
  "action.delivery": "Action delivery",
  "action.payment_terms": "Action payment terms",
  "action.hold_seconds": "Action hold (s)",
  "run.state": "Negotiation state",
  "run.fence_current": "Worker fence current",
  "counts.outbound_messages": "Messages already sent",
  "counts.reservations": "Reservations already made",
  item_reserved: "Item already reserved",
  buyer_signals: "Buyer risk signals",
  committed_operation: "Already committed",
  offers: "Offers",
  acceptances: "Buyer acceptances",
  approval: "Owner approval",
};

function fmtScalar(key: string, v: unknown): string {
  if (typeof v === "number" && /price_minor$/.test(key)) return money(v);
  if (v === null) return "none";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === "object" ? describeObj(x as Record<string, unknown>) : String(x))).join("; ") : "none";
  if (typeof v === "object") return describeObj(v as Record<string, unknown>);
  return String(v);
}

function describeObj(o: Record<string, unknown>): string {
  return Object.entries(o)
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${fmtScalar(k, v)}`)
    .join(", ");
}

/** Readable "key: value" lines for a case's setup overrides. Nested objects flatten to dotted keys. */
export function overrideLines(o: Record<string, unknown>, prefix = ""): { key: string; label: string; value: string }[] {
  const out: { key: string; label: string; value: string }[] = [];
  for (const [k, v] of Object.entries(o)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v) && !(key in KEY_LABELS)) out.push(...overrideLines(v as Record<string, unknown>, key));
    else out.push({ key, label: KEY_LABELS[key] ?? key.replace(/[._]/g, " "), value: fmtScalar(key, v) });
  }
  return out;
}

const VERDICT_ICON: Record<GateVerdict, IconName> = { allow: "checkCircle", deny: "ban", invalid: "alert", approval_required: "hourglass" };

export function VerdictChip({ verdict, testid }: { verdict: GateVerdict; testid?: string }) {
  return (
    <span className={`verdict verdict-${verdict}`} data-testid={testid}>
      <Icon name={VERDICT_ICON[verdict]} size={15} />
      {VERDICT_LABELS[verdict]}
    </span>
  );
}

const pretty = (v: unknown) => JSON.stringify(v, null, 2);

export function Lab() {
  const [list, setList] = useState<LabCaseList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [family, setFamily] = useState("");
  const [verdict, setVerdict] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(() => new URLSearchParams(window.location.search).get("case"));
  const [detail, setDetail] = useState<LabCase | null>(null);
  const [text, setText] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [result, setResult] = useState<{ trace: GateTrace; edited: boolean; note?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [evalError, setEvalError] = useState<string | null>(null);
  const detailRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    api
      .labCases()
      .then((l) => {
        setList(l);
        setSelected((s) => s ?? l.cases[0]?.case_id ?? null);
      })
      .catch((err) => setListError(err instanceof ApiError ? err.message : "Could not load the gate cases."));
  }, []);

  useEffect(() => {
    if (!selected) return;
    const ctrl = new AbortController();
    setDetail(null);
    setResult(null);
    setJsonError(null);
    setEvalError(null);
    const url = new URL(window.location.href);
    url.searchParams.set("case", selected);
    window.history.replaceState({}, "", url.pathname + url.search);
    api
      .labCase(selected, ctrl.signal)
      .then((c) => {
        setDetail(c);
        setText(pretty(c.proposal));
        setResult({ trace: c.trace, edited: false });
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setEvalError(err instanceof ApiError ? err.message : "Could not load this case.");
      });
    return () => ctrl.abort();
  }, [selected]);

  const filtered = useMemo(() => {
    if (!list) return [];
    const q = query.trim().toLowerCase();
    return list.cases.filter(
      (c) =>
        (!family || c.family === family) &&
        (!verdict || c.expected.verdict === verdict) &&
        (!q || `${c.case_id} ${c.title} ${c.expected.reason_code ?? ""} ${list.families[c.family] ?? ""}`.toLowerCase().includes(q)),
    );
  }, [list, family, verdict, query]);

  function pick(id: string) {
    setSelected(id);
    // On narrow screens the detail sits below the list; take keyboard and screen-reader users there.
    window.setTimeout(() => detailRef.current?.focus({ preventScroll: window.innerWidth >= 1024 }), 50);
  }

  async function evaluate() {
    if (!detail) return;
    let proposal: unknown;
    try {
      proposal = JSON.parse(text);
    } catch (e) {
      setJsonError(`This is not valid JSON, so nothing was sent. ${e instanceof Error ? e.message : ""}`.trim());
      return;
    }
    setJsonError(null);
    setEvalError(null);
    setBusy(true);
    try {
      const r = await api.labEvaluate(detail.case_id, proposal);
      setResult({ trace: r.trace, edited: true, note: r.note });
    } catch (err) {
      setEvalError(err instanceof ApiError ? err.message : "Could not evaluate the proposal.");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    if (!detail) return;
    setText(pretty(detail.proposal));
    setJsonError(null);
    setEvalError(null);
    setResult({ trace: detail.trace, edited: false });
  }

  const summary = list?.summary;
  const families = list ? Object.entries(list.families).filter(([id]) => list.cases.some((c) => c.family === id)) : [];
  const overrides = detail ? overrideLines(detail.setup_overrides) : [];
  const trace = result?.trace ?? null;

  return (
    <main id="main" className="page page-lab">
      <div className="page-head">
        <div>
          <p className="eyebrow">Evidence · Gate lab</p>
          <h1>Gate lab</h1>
          <p className="sub">Deterministic replay of {summary?.total ?? 151} hand-written cases · no model involved.</p>
        </div>
        <div className="row gap wrap">
          <span className="prov-chip prov-synthetic_fixture" data-testid="lab-evidence-mode" title="Hand-written fixture cases, not live traffic">
            {list?.evidence_mode ?? "SYNTHETIC_FIXTURE"}
          </span>
          {summary ? (
            <span className={`status-pill ${summary.passed === summary.total ? "status-active" : "status-revoked"}`} data-testid="lab-summary">
              <Icon name={summary.passed === summary.total ? "checkCircle" : "alert"} size={18} />
              {summary.passed}/{summary.total} match the hand-written expectation
            </span>
          ) : null}
        </div>
      </div>
      <p className="lab-intro">
        Each case fixes a setup (the owner&rsquo;s limits, the negotiation, offers, acceptances, approvals) and a raw model proposal. The
        expected verdict was written by hand from the policy before the gate existed; the actual verdict is the real server gate, run now. Pick
        a case, edit the proposal and see which of the ten checks decides.
      </p>

      {listError ? (
        <div className="card empty" role="alert">
          <Icon name="alert" size={32} />
          <h2>Could not load the gate cases</h2>
          <p>{listError}</p>
        </div>
      ) : (
        <div className="lab-grid">
          <section className="card lab-list" aria-labelledby="lab-cases-title">
            <h2 id="lab-cases-title" className="card-title">
              Cases
              {summary ? (
                <span className="lab-mini-summary">
                  <Icon name="checkCircle" size={16} /> {summary.passed}/{summary.total} match
                </span>
              ) : null}
            </h2>
            <div className="lab-filters">
              <div className="field">
                <label htmlFor="lab-family" className="label">
                  Family
                </label>
                <select id="lab-family" value={family} onChange={(e) => setFamily(e.target.value)} data-testid="lab-family">
                  <option value="">All families</option>
                  {families.map(([id, title]) => (
                    <option key={id} value={id}>
                      {id} · {title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="lab-verdict" className="label">
                  Expected verdict
                </label>
                <select id="lab-verdict" value={verdict} onChange={(e) => setVerdict(e.target.value)} data-testid="lab-verdict">
                  <option value="">Any verdict</option>
                  {(Object.keys(VERDICT_LABELS) as GateVerdict[]).map((v) => (
                    <option key={v} value={v}>
                      {VERDICT_LABELS[v]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field lab-search">
                <label htmlFor="lab-search" className="label">
                  Search
                </label>
                <input
                  id="lab-search"
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Case id, title or reason code"
                  data-testid="lab-search"
                />
              </div>
            </div>
            <p className="muted small" role="status" data-testid="lab-count">
              {list ? `Showing ${filtered.length} of ${list.cases.length} cases` : "Loading cases…"}
            </p>
            <ul className="lab-cases" data-testid="lab-cases">
              {filtered.map((c) => (
                <li key={c.case_id}>
                  <button
                    type="button"
                    className={`lab-case ${selected === c.case_id ? "on" : ""}`}
                    aria-current={selected === c.case_id ? "true" : undefined}
                    onClick={() => pick(c.case_id)}
                    data-testid={`lab-case-${c.case_id}`}
                  >
                    <span className={`lab-pass ${c.pass ? "ok" : "bad"}`}>
                      <Icon name={c.pass ? "checkCircle" : "xCircle"} size={18} title={c.pass ? "Matches expectation" : "Does not match expectation"} />
                    </span>
                    <span className="lab-case-main">
                      <span className="lab-case-id mono">{c.case_id}</span>
                      <span className="lab-case-title">{c.title}</span>
                    </span>
                    <span className="lab-case-verdicts">
                      <span className="sr-only">Expected </span>
                      <VerdictChip verdict={c.expected.verdict} />
                      {c.actual.verdict !== c.expected.verdict ? (
                        <>
                          <span className="sr-only">, actual </span>
                          <VerdictChip verdict={c.actual.verdict} />
                        </>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <section className="card lab-detail" aria-labelledby="lab-detail-title" data-testid="lab-detail">
            {!detail ? (
              <p className="loading" role="status">
                {evalError ?? "Loading the case…"}
              </p>
            ) : (
              <>
                <p className="eyebrow">
                  {detail.family} · {list?.families[detail.family] ?? ""} <span className="prov-chip prov-synthetic_fixture lab-detail-chip">SYNTHETIC_FIXTURE · no model</span>
                </p>
                <h2 id="lab-detail-title" ref={detailRef} tabIndex={-1} data-testid="lab-detail-title">
                  <span className="mono">{detail.case_id}</span> {detail.title}
                </h2>
                <div className="lab-expect">
                  <span>
                    Expected (hand-written): <VerdictChip verdict={detail.expected.verdict} testid="lab-expected" />{" "}
                    <code>{detail.expected.reason_code ?? "no code"}</code>
                  </span>
                  <span>
                    Actual (gate, frozen proposal): <VerdictChip verdict={detail.trace.verdict} />{" "}
                    <code>{detail.trace.reason_code ?? "no code"}</code>
                  </span>
                </div>

                <h3 className="lab-h3">Setup differences from the defaults</h3>
                {overrides.length ? (
                  <dl className="lab-overrides" data-testid="lab-overrides">
                    {overrides.map((o) => (
                      <div key={o.key}>
                        <dt>{o.label}</dt>
                        <dd>{o.value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="muted" data-testid="lab-overrides">
                    None. Default setup: active mandate, private minimum $500, asking $650, pickup and cash at pickup, the listing offer of $650
                    open, no acceptances, no approval.
                  </p>
                )}
                <details className="tech">
                  <summary>Full effective setup</summary>
                  <pre>{pretty(detail.effective_setup)}</pre>
                </details>

                <div className="lab-work">
                <div className="lab-editor">
                  <label htmlFor="lab-proposal" className="label">
                    Proposal: the model&rsquo;s raw output (editable JSON)
                  </label>
                  <textarea
                    id="lab-proposal"
                    className="mono-input"
                    rows={13}
                    spellCheck={false}
                    value={text}
                    onChange={(e) => {
                      setText(e.target.value);
                      if (jsonError) setJsonError(null);
                    }}
                    aria-invalid={jsonError ? true : undefined}
                    aria-describedby={jsonError ? "lab-json-error" : "lab-proposal-help"}
                    data-testid="lab-proposal"
                  />
                  {jsonError ? (
                    <p id="lab-json-error" className="field-error" role="alert" data-testid="lab-json-error">
                      {jsonError}
                    </p>
                  ) : (
                    <p id="lab-proposal-help" className="hint">
                      Change a price, a fact id or the kind of action. Evaluating runs the real gate against this case&rsquo;s setup; nothing is stored or
                      sent.
                    </p>
                  )}
                  <div className="row gap wrap">
                    <button type="button" className="btn btn-primary" onClick={() => void evaluate()} disabled={busy} data-testid="lab-evaluate">
                      <Icon name="shieldCheck" /> {busy ? "Evaluating…" : "Evaluate"}
                    </button>
                    <button type="button" className="btn btn-secondary" onClick={reset} data-testid="lab-reset">
                      <Icon name="refresh" /> Reset
                    </button>
                  </div>
                  {evalError ? (
                    <p className="field-error" role="alert">
                      {evalError}
                    </p>
                  ) : null}
                </div>

                <div className="lab-result" aria-live="polite" aria-atomic="true" data-testid="lab-result" data-edited={result?.edited ? "true" : "false"}>
                  {trace ? (
                    <>
                      <h3 className="lab-h3">{result?.edited ? "Result for your edited proposal" : "Result for the frozen proposal"}</h3>
                      <div className="lab-verdict-line">
                        <VerdictChip verdict={trace.verdict} testid="lab-result-verdict" />
                        <code data-testid="lab-result-code">{trace.reason_code ?? "no code"}</code>
                      </div>
                      <p className="lab-reason" data-testid="lab-result-reason">
                        {reasonText(trace)}
                        {trace.detail ? <span className="muted"> ({trace.detail})</span> : null}
                      </p>
                      <ol className="lab-checks" aria-label="Gate checks in order" data-testid="lab-checks">
                        {trace.checks.map((c, i) => (
                          <li key={c.name} className={`lab-check lab-check-${c.status}`} data-testid={`lab-check-${c.name}`} data-status={c.status}>
                            <span className="lab-check-icon" aria-hidden="true">
                              {c.status === "passed" ? "✓" : c.status === "decided" ? "●" : "–"}
                            </span>
                            <span className="lab-check-num">{i + 1}</span>
                            <span className="lab-check-label">{c.label}</span>
                            <span className="lab-check-status">
                              {c.status === "passed" ? "passed" : c.status === "decided" ? "decided" : "not run"}
                            </span>
                          </li>
                        ))}
                      </ol>
                      {result?.note ? <p className="hint">{result.note}</p> : null}
                    </>
                  ) : null}
                </div>
                </div>
              </>
            )}
          </section>
        </div>
      )}

      <div className="row gap wrap">
        <button type="button" className="btn btn-secondary" onClick={() => navigate("/evaluation")}>
          <Icon name="chart" /> Back to the evaluation
        </button>
      </div>
    </main>
  );
}
