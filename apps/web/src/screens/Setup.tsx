import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Icon } from "../components/Icon";
import { VoiceNote } from "../components/VoiceNote";
import { clockTime, MANDATE_STATUS_LABELS, mmss, money, navigate, parseDollars, remaining, useNow } from "../lib";
import { useApp } from "../state";
import type { DraftResult, Listing, MandateTerms } from "../types";

interface Form {
  asking: string;
  min: string;
  hold: string;
  facts: string[];
}

function fromTerms(t: MandateTerms): Form {
  return {
    asking: String(t.asking_price_minor / 100),
    min: String(t.min_price_minor / 100),
    hold: String(Math.round(t.reservation_hold_seconds / 60)),
    facts: [...t.allowed_public_fact_ids],
  };
}

interface Parsed {
  asking: number | null;
  min: number | null;
  holdSeconds: number | null;
  errors: Partial<Record<"asking" | "min" | "hold" | "facts", string>>;
}

function parseForm(f: Form, listing: Listing): Parsed {
  const errors: Parsed["errors"] = {};
  const asking = parseDollars(f.asking);
  const min = parseDollars(f.min);
  const holdMin = /^\d{1,2}$/.test(f.hold.trim()) ? Number(f.hold.trim()) : null;
  if (asking === null || asking <= 0) errors.asking = "Enter a whole dollar amount, for example 650.";
  else if (asking > listing.asking_price_minor) errors.asking = `In this demo the agent cannot ask more than the listed ${money(listing.asking_price_minor)}.`;
  if (min === null || min <= 0) errors.min = "Type your private minimum in dollars, for example 500.";
  else if (asking !== null && min > asking) errors.min = "Your minimum cannot be above the asking price.";
  if (holdMin === null || holdMin < 5 || holdMin > 60) errors.hold = "Choose between 5 and 60 minutes.";
  if (!f.facts.length) errors.facts = "Pick at least one fact, or the agent cannot answer any question.";
  return { asking, min, holdSeconds: holdMin === null ? null : holdMin * 60, errors };
}

export function Setup() {
  const { session, sessionLoading, ensureSession, refreshSession, notify, notifyError, announce } = useApp();
  const now = useNow();
  const [form, setForm] = useState<Form | null>(null);
  const [formRev, setFormRev] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [goal, setGoal] = useState("");
  const [draft, setDraft] = useState<DraftResult | null>(null);
  const [drafting, setDrafting] = useState<"text" | "voice" | null>(null);
  const [applied, setApplied] = useState<string[]>([]);

  useEffect(() => {
    if (!session && !sessionLoading) void ensureSession();
  }, [session, sessionLoading, ensureSession]);

  // Load the form from the server's current revision whenever that revision changes.
  useEffect(() => {
    if (session && session.mandate.current_revision !== formRev) {
      setForm(fromTerms(session.mandate.terms));
      setFormRev(session.mandate.current_revision);
    }
  }, [session, formRev]);

  const listing = session?.listing;
  const mandate = session?.mandate;
  const parsed = useMemo(() => (form && listing ? parseForm(form, listing) : null), [form, listing]);

  if (!session || !form || !listing || !mandate || !parsed) {
    return (
      <main id="main" className="page">
        <p className="loading" role="status">
          Preparing your fictional scenario…
        </p>
      </main>
    );
  }

  const terms = mandate.terms;
  const changes: Record<string, unknown> = {};
  if (parsed.asking !== null && parsed.asking !== terms.asking_price_minor) changes.asking_price_minor = parsed.asking;
  if (parsed.min !== null && parsed.min !== terms.min_price_minor) changes.min_price_minor = parsed.min;
  if (parsed.holdSeconds !== null && parsed.holdSeconds !== terms.reservation_hold_seconds) changes.reservation_hold_seconds = parsed.holdSeconds;
  const sortedFacts = listing.public_facts.map((f) => f.id).filter((id) => form.facts.includes(id));
  const termFacts = listing.public_facts.map((f) => f.id).filter((id) => terms.allowed_public_fact_ids.includes(id));
  if (sortedFacts.join() !== termFacts.join()) changes.allowed_public_fact_ids = sortedFacts;
  const dirty = Object.keys(changes).length > 0;
  const invalid = Object.keys(parsed.errors).length > 0;
  const ended = mandate.status === "revoked" || mandate.status === "expired";
  const isActiveCurrent = (mandate.status === "active" || mandate.status === "paused") && mandate.active_revision === mandate.current_revision && !dirty;
  const nextRevision = dirty ? mandate.current_revision + 1 : mandate.current_revision;
  const left = remaining(mandate.expires_at, now);

  async function activate() {
    if (!mandate || invalid) return;
    setBusy(true);
    try {
      let revision = mandate.current_revision;
      if (dirty) {
        const r = await api.patchMandate(mandate.mandate_id, mandate.current_revision, changes);
        revision = r.revision;
      }
      const a = await api.activate(mandate.mandate_id, revision);
      await refreshSession();
      announce(`Mandate revision ${a.revision} is active until ${clockTime(a.expires_at)}.`);
      notify("success", `Revision ${a.revision} is active until ${clockTime(a.expires_at)}. The agent may now negotiate inside these limits.`);
      navigate("/negotiate");
    } catch (err) {
      if (err instanceof ApiError && err.code === "REVISION_CONFLICT") {
        notify("error", "The mandate changed since you loaded it. The latest version is shown; review it and activate again.");
        await refreshSession();
      } else notifyError(err, "Could not activate the mandate.");
    } finally {
      setBusy(false);
    }
  }

  async function requestDraft(kind: "text" | "voice", wav?: Blob) {
    setDrafting(kind);
    setApplied([]);
    try {
      const d = kind === "text" ? await api.draftText(goal.trim()) : await api.draftVoice(wav!);
      setDraft(d);
      announce(`Suggestions ready: ${Object.keys(d.suggestions).length} suggested changes, ${d.ambiguities.length} questions to resolve.`);
    } catch (err) {
      notifyError(err, "Could not draft suggestions.");
    } finally {
      setDrafting(null);
    }
  }

  function applySuggestion(key: keyof DraftResult["suggestions"]) {
    if (!draft || !form) return;
    const v = draft.suggestions[key];
    if (v === undefined) return;
    if (key === "asking_price_minor") setForm({ ...form, asking: String((v as number) / 100) });
    if (key === "reservation_hold_seconds") setForm({ ...form, hold: String(Math.round((v as number) / 60)) });
    if (key === "allowed_public_fact_ids") setForm({ ...form, facts: [...(v as string[])] });
    setApplied((a) => [...a, key]);
  }

  const factText = (id: string) => listing.public_facts.find((f) => f.id === id)?.text ?? id;
  const shownFacts = listing.public_facts.filter((f) => form.facts.includes(f.id));

  return (
    <main id="main" className="page page-setup">
      <div className="page-head">
        <div>
          <p className="eyebrow">Step 1 of 3 · Mandate</p>
          <h1>Set the agent&rsquo;s limits</h1>
          <p className="sub">Nothing happens until you activate. Every change becomes a new revision you can see.</p>
        </div>
        <span className={`status-pill status-${mandate.status}`} data-testid="mandate-status">
          <Icon name={mandate.status === "active" ? "shieldCheck" : mandate.status === "draft" ? "info" : "ban"} size={18} />
          {MANDATE_STATUS_LABELS[mandate.status]} · revision {mandate.active_revision ?? mandate.current_revision}
        </span>
      </div>

      <div className="setup-grid">
        <div className="setup-main">
          <section className="card listing-card" aria-labelledby="listing-title" data-testid="listing-card">
            <div className="listing-head">
              <div className="listing-thumb" aria-hidden="true">
                <Icon name="laptop" size={40} />
              </div>
              <div>
                <p className="eyebrow">Your listing · fictional</p>
                <h2 id="listing-title">{listing.title}</h2>
                <p className="price-lg">{money(listing.asking_price_minor)}</p>
              </div>
            </div>
            <ul className="fact-list">
              {listing.public_facts.map((f) => (
                <li key={f.id}>
                  <Icon name="check" size={16} /> {f.text}
                </li>
              ))}
            </ul>
          </section>

          <form
            className="card"
            aria-labelledby="terms-title"
            onSubmit={(e) => {
              e.preventDefault();
              void activate();
            }}
            noValidate
          >
            <h2 id="terms-title" className="card-title">
              <Icon name="tag" /> Price and terms
            </h2>
            <div className="field-grid">
              <div className="field">
                <label htmlFor="asking" className="label">
                  Asking price (USD)
                </label>
                <div className="money-input">
                  <span aria-hidden="true">$</span>
                  <input
                    id="asking"
                    inputMode="decimal"
                    value={form.asking}
                    onChange={(e) => setForm({ ...form, asking: e.target.value })}
                    aria-invalid={!!parsed.errors.asking}
                    aria-describedby="asking-help"
                    disabled={ended}
                    data-testid="asking-price"
                  />
                </div>
                <p id="asking-help" className={parsed.errors.asking ? "field-error" : "hint"}>
                  {parsed.errors.asking ?? "The highest price the agent may offer or accept."}
                </p>
              </div>
              <div className="field field-private">
                <label htmlFor="min" className="label">
                  <Icon name="lock" size={16} /> Private minimum (USD)
                </label>
                <div className="money-input">
                  <span aria-hidden="true">$</span>
                  <input
                    id="min"
                    inputMode="decimal"
                    value={form.min}
                    onChange={(e) => setForm({ ...form, min: e.target.value })}
                    aria-invalid={!!parsed.errors.min}
                    aria-describedby="min-help min-private"
                    disabled={ended}
                    data-testid="min-price"
                  />
                </div>
                <p id="min-private" className="private-note">
                  <Icon name="eyeOff" size={16} /> Only you see this. The agent and buyers never do.
                </p>
                <p id="min-help" className={parsed.errors.min ? "field-error" : "hint"}>
                  {parsed.errors.min ?? "The server blocks any offer or reservation below it."}
                </p>
              </div>
              <div className="field">
                <label htmlFor="hold" className="label">
                  Reservation hold (minutes)
                </label>
                <input
                  id="hold"
                  type="number"
                  min={5}
                  max={60}
                  value={form.hold}
                  onChange={(e) => setForm({ ...form, hold: e.target.value })}
                  aria-invalid={!!parsed.errors.hold}
                  aria-describedby="hold-help"
                  disabled={ended}
                  data-testid="hold-minutes"
                />
                <p id="hold-help" className={parsed.errors.hold ? "field-error" : "hint"}>
                  {parsed.errors.hold ?? "How long an approved reservation holds the laptop for the buyer."}
                </p>
              </div>
            </div>

            <fieldset className="facts" aria-describedby="facts-help">
              <legend className="label">Facts the agent may share</legend>
              <p id="facts-help" className={parsed.errors.facts ? "field-error" : "hint"}>
                {parsed.errors.facts ?? "The agent can only cite these public facts. Everything else stays private."}
              </p>
              <div className="fact-checks">
                {listing.public_facts.map((f) => (
                  <label key={f.id} className="check">
                    <input
                      type="checkbox"
                      checked={form.facts.includes(f.id)}
                      disabled={ended}
                      onChange={(e) =>
                        setForm({ ...form, facts: e.target.checked ? [...form.facts, f.id] : form.facts.filter((x) => x !== f.id) })
                      }
                      data-testid={`fact-${f.id}`}
                    />
                    <span>{f.text}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <div className="fixed-terms">
              <h3>Fixed in this demo</h3>
              <ul>
                <li>
                  <Icon name="lock" size={16} /> <strong>Pickup only.</strong> The agent cannot ship or deliver.
                </li>
                <li>
                  <Icon name="lock" size={16} /> <strong>Cash at pickup.</strong> No QR codes, payment links, transfers or advances.
                </li>
                <li>
                  <Icon name="lock" size={16} /> <strong>Every reservation needs your approval</strong> of that exact action.
                </li>
                <li>
                  <Icon name="lock" size={16} /> <strong>Expires 30 minutes after activation.</strong> Pause or revoke any time.
                </li>
                <li>
                  <Icon name="lock" size={16} /> At most {terms.max_outbound_messages} agent messages and {terms.max_reservations} reservation per negotiation.
                </li>
              </ul>
            </div>
          </form>
          <section className="card" aria-labelledby="assist-title">
            <h2 id="assist-title" className="card-title">
              <Icon name="sparkle" /> Or describe it (optional)
            </h2>
            <p className="hint">
              Say what you want in your own words. The model suggests settings; nothing changes until you apply a suggestion and activate.
              Your private minimum is never suggested. You type it.
            </p>
            <label htmlFor="goal" className="label">
              What should the agent do?
            </label>
            <textarea
              id="goal"
              rows={3}
              maxLength={1000}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Sell my laptop for around $640, pickup only, hold it for 20 minutes once someone agrees."
              data-testid="draft-goal"
            />
            <div className="row gap wrap">
              <button
                type="button"
                className="btn btn-secondary"
                disabled={!goal.trim() || drafting !== null || ended}
                onClick={() => void requestDraft("text")}
                data-testid="draft-submit"
              >
                <Icon name="sparkle" /> {drafting === "text" ? "Reading your description…" : "Suggest settings"}
              </button>
              <VoiceNote disabled={drafting !== null || ended} onWav={(wav) => void requestDraft("voice", wav)} />
            </div>
            {drafting === "voice" ? (
              <p className="waiting" role="status">
                <span className="pulse" aria-hidden="true" /> The model is listening to your voice note…
              </p>
            ) : null}
            {draft ? (
              <div className="draft" data-testid="draft-result">
                <div className="draft-head">
                  <span className="badge badge-neutral">Suggestions only · not active</span>
                  <span className="muted small">
                    {draft.provider_mode === "mock" ? "MOCK_ONLY: local rules, not a model" : `Model: ${draft.model ?? "unknown"}`}
                  </span>
                </div>
                {draft.transcript ? (
                  <p className="transcript">
                    <span className="muted">Heard:</span> &ldquo;{draft.transcript}&rdquo;
                  </p>
                ) : null}
                {Object.keys(draft.suggestions).length ? (
                  <table className="diff">
                    <caption className="sr-only">Suggested changes</caption>
                    <thead>
                      <tr>
                        <th scope="col">Setting</th>
                        <th scope="col">Now</th>
                        <th scope="col">Suggested</th>
                        <th scope="col">
                          <span className="sr-only">Apply</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {draft.suggestions.asking_price_minor !== undefined ? (
                        <tr>
                          <th scope="row">Asking price</th>
                          <td>{money(parsed.asking)}</td>
                          <td className="sugg">{money(draft.suggestions.asking_price_minor)}</td>
                          <td>
                            <ApplyButton done={applied.includes("asking_price_minor")} onClick={() => applySuggestion("asking_price_minor")} testid="apply-asking" />
                          </td>
                        </tr>
                      ) : null}
                      {draft.suggestions.reservation_hold_seconds !== undefined ? (
                        <tr>
                          <th scope="row">Hold</th>
                          <td>{form.hold} min</td>
                          <td className="sugg">{Math.round(draft.suggestions.reservation_hold_seconds / 60)} min</td>
                          <td>
                            <ApplyButton done={applied.includes("reservation_hold_seconds")} onClick={() => applySuggestion("reservation_hold_seconds")} testid="apply-hold" />
                          </td>
                        </tr>
                      ) : null}
                      {draft.suggestions.allowed_public_fact_ids !== undefined ? (
                        <tr>
                          <th scope="row">Facts to share</th>
                          <td>{form.facts.length} facts</td>
                          <td className="sugg">{draft.suggestions.allowed_public_fact_ids.map(factText).join("; ")}</td>
                          <td>
                            <ApplyButton done={applied.includes("allowed_public_fact_ids")} onClick={() => applySuggestion("allowed_public_fact_ids")} testid="apply-facts" />
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                ) : (
                  <p className="muted">No setting could be suggested from that. Your form is unchanged.</p>
                )}
                {draft.ambiguities.length ? (
                  <div className="ambiguities">
                    <h3>
                      <Icon name="question" size={18} /> Please check
                    </h3>
                    <ul>
                      {draft.ambiguities.map((a) => (
                        <li key={a}>{a}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : null}
          </section>

        </div>

        <aside className="setup-side" aria-labelledby="preview-title">
          <section className="card preview-card" data-testid="authority-preview">
            <h2 id="preview-title" className="card-title">
              <Icon name="shieldCheck" /> What you are authorising
            </h2>
            <h3 className="may">The agent may</h3>
            <ul className="may-list">
              <li>
                Answer questions using {shownFacts.length === listing.public_facts.length ? "the listing's public facts" : `${shownFacts.length} chosen facts`}.
              </li>
              <li>
                Counter between <strong>{money(parsed.min)}</strong> and <strong>{money(parsed.asking)}</strong>.
              </li>
              <li>Decline unsafe or out-of-policy requests with fixed, reviewed wording.</li>
              <li>
                Ask you to approve one reservation, held <strong>{form.hold || "—"} minutes</strong>, after the buyer taps Accept offer.
              </li>
            </ul>
            <h3 className="may-not">It may not</h3>
            <ul className="may-not-list">
              <li>Ship or deliver the laptop.</li>
              <li>Share your address, phone number or private notes.</li>
              <li>Accept QR codes, payment links or advance payments.</li>
              <li>Go below your private minimum, or tell anyone what it is.</li>
              <li>Reserve without your approval of the exact action.</li>
            </ul>

            <div className="activation">
              {ended ? (
                <p className="field-error" role="status">
                  This mandate has {mandate.status === "revoked" ? "been revoked" : "expired"}. Start a new scenario to set new limits.
                </p>
              ) : isActiveCurrent ? (
                <>
                  <p className="ok-line" data-testid="active-line">
                    <Icon name="checkCircle" /> Revision {mandate.active_revision} is {mandate.status === "paused" ? "paused" : "active"}
                    {left !== null ? ` · ${mmss(left)} left` : ""}
                  </p>
                  <button type="button" className="btn btn-primary btn-lg block" onClick={() => navigate("/negotiate")} data-testid="go-negotiate">
                    Open the negotiation <Icon name="arrowRight" />
                  </button>
                </>
              ) : (
                <>
                  <p className="muted small">
                    {dirty
                      ? `Your edits become revision ${nextRevision}.`
                      : mandate.has_unactivated_changes
                        ? `Revision ${mandate.current_revision} is saved but not active yet.`
                        : `Revision ${mandate.current_revision} is ready to activate.`}
                    {mandate.status === "active" || mandate.status === "paused"
                      ? " Activating replaces the current authority and cancels any pending approval."
                      : ""}
                  </p>
                  <button
                    type="button"
                    className="btn btn-primary btn-lg block"
                    disabled={busy || invalid}
                    onClick={() => void activate()}
                    data-testid="activate-mandate"
                  >
                    <Icon name="shieldCheck" />
                    {busy ? "Activating…" : `Activate mandate · revision ${nextRevision}`}
                  </button>
                  {invalid ? <p className="field-error">Fix the highlighted fields first.</p> : null}
                </>
              )}
            </div>
          </section>
        </aside>
      </div>

      {/* Phones: the in-page button is several screens down, so the same action is pinned to the bottom. */}
      {!ended ? (
        <div className="sticky-action" role="region" aria-label="Activate">
          {isActiveCurrent ? (
            <button type="button" className="btn btn-primary btn-lg block" onClick={() => navigate("/negotiate")} data-testid="go-negotiate-sticky">
              Open the negotiation <Icon name="arrowRight" />
            </button>
          ) : (
            <>
              <span className="sticky-note">
                {invalid ? "Fix the highlighted fields first." : dirty ? `Your edits become revision ${nextRevision}.` : "Nothing is active until you activate."}
              </span>
              <button
                type="button"
                className="btn btn-primary btn-lg block"
                disabled={busy || invalid}
                onClick={() => void activate()}
                data-testid="activate-mandate-sticky"
              >
                <Icon name="shieldCheck" />
                {busy ? "Activating…" : `Activate mandate · revision ${nextRevision}`}
              </button>
            </>
          )}
        </div>
      ) : null}
    </main>
  );
}

function ApplyButton({ done, onClick, testid }: { done: boolean; onClick: () => void; testid: string }) {
  return (
    <button type="button" className="btn btn-small btn-secondary" onClick={onClick} disabled={done} data-testid={testid}>
      {done ? (
        <>
          <Icon name="check" size={16} /> Applied
        </>
      ) : (
        "Apply to form"
      )}
    </button>
  );
}
