import { useState } from "react";
import { Icon, type IconName } from "../components/Icon";
import { ModeChip } from "../components/Chrome";
import { navigate } from "../lib";
import { useApp } from "../state";

const STEPS: { icon: IconName; title: string; text: string }[] = [
  { icon: "lock", title: "You set the limits", text: "Asking price, a private minimum, what the agent may say, and how long it may act." },
  { icon: "shieldCheck", title: "The server enforces them", text: "The model only proposes typed actions. A server gate allows or blocks each one; buyers only see fixed templates." },
  { icon: "hand", title: "You approve the one commitment", text: "A reservation needs the buyer's explicit acceptance and your approval of that exact action." },
  { icon: "receipt", title: "You get proof", text: "The server reads the reservation back and shows a verified receipt, field by field." },
];

export function Start() {
  const { session, ensureSession, meta } = useApp();
  const [busy, setBusy] = useState(false);

  async function begin() {
    setBusy(true);
    const s = await ensureSession();
    setBusy(false);
    if (!s) return;
    navigate(s.mandate.status === "draft" ? "/setup" : "/negotiate");
  }

  return (
    <main id="main" className="page page-start">
      <section className="hero" aria-labelledby="hero-title">
        <p className="eyebrow">Agent commerce that holds up in production</p>
        <h1 id="hero-title">Let an agent negotiate for you.</h1>
        <p className="lead">
          You set the limits. The server enforces them. You approve the one commitment. You get proof.
        </p>
        <p className="incident">
          <Icon name="info" size={18} />
          <span>
            Reported in Sept 2026: a marketplace agent accepted a lowball offer, shared a seller&rsquo;s address and told a buyer
            &ldquo;Yep I&rsquo;m here&rdquo; without asking. Delegate is the controls layer that keeps agent selling inside the
            owner&rsquo;s limits.
          </span>
        </p>
        <div className="hero-actions">
          <button type="button" className="btn btn-primary btn-xl" onClick={() => void begin()} disabled={busy} data-testid="start-button">
            {busy ? "Preparing your scenario…" : session ? "Continue the negotiation" : "Try the negotiation"}
            <Icon name="arrowRight" />
          </button>
          <button type="button" className="btn btn-secondary btn-xl" onClick={() => navigate("/evaluation")} data-testid="start-evaluation">
            <Icon name="chart" /> See the evaluation
          </button>
        </div>
        <div className="hero-meta">
          <ModeChip />
          <span className="sandbox-note" data-testid="sandbox-label">
            <Icon name="shield" size={18} />
            {meta?.sandbox_label ?? "Marketplace sandbox: fictional item, people and money. A reservation here is not a sale or payment."}
          </span>
        </div>
      </section>

      <section className="steps" aria-label="How Delegate works">
        {STEPS.map((s, i) => (
          <article key={s.title} className="step-card">
            <div className="step-num" aria-hidden="true">
              <Icon name={s.icon} size={22} />
            </div>
            <h2>
              <span className="sr-only">Step {i + 1}: </span>
              {s.title}
            </h2>
            <p>{s.text}</p>
          </article>
        ))}
      </section>

      <section className="scenario" aria-labelledby="scenario-title">
        <h2 id="scenario-title">The scenario</h2>
        <p>
          You are selling a fictional used 14-inch ultrabook, listed at <strong>$650</strong>, pickup only, cash at pickup. Your private
          minimum is <strong>$500</strong>. You play both sides: the seller who sets the mandate and approves, and the buyer who asks
          questions, haggles and tries to talk the agent out of its limits.
        </p>
      </section>
    </main>
  );
}
