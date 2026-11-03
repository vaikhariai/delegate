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

const FITS: { icon: IconName; title: string; text: string; tag: string; here?: boolean }[] = [
  { icon: "user", title: "Buyer channel", text: "Where buyers already talk: WhatsApp Business, Messenger for Pages, a marketplace inbox.", tag: "Simulated in this demo" },
  { icon: "shieldCheck", title: "Delegate gate", text: "Muse Spark proposes one typed action per buyer message; the server checks it against the mandate and sends only fixed template text.", tag: "Server-side, open source as mandate-gate", here: true },
  { icon: "hand", title: "Seller console", text: "Where authority lives: the mandate, approval of one exact reservation, revoke, and the verified receipt.", tag: "This app", here: true },
];

const LESSONS: [string, string][] = [
  ["Agreed to a price under the seller's minimum", "The private minimum is checked by the server on every counter and reservation; the model never sees it."],
  ["Shared the street address through an allowed reply template", "No template contains the seller's address; buyers only see the public pickup area. The address is never sent in chat."],
  ["Replied \u201cYep I\u2019m here!\u201d on the seller's behalf", "The model cannot write buyer-facing text. Replies are fixed templates, and none claims the seller is present."],
  ["An approved setting was treated as approval of each action", "Approval binds one exact reservation (buyer, price, pickup, hold) by a hash the server recomputes before executing. Anything else needs a new approval."],
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
        <p className="incident" data-testid="incident-note">
          <Icon name="info" size={18} />
          <span>
            In September 2026 a Toronto seller let Meta&rsquo;s Muse agent run his Marketplace listings. He reported that it gave
            buyers his street address through a reply template he had allowed, agreed to $100 under his minimum, and replied
            &ldquo;Yep I&rsquo;m here!&rdquo; while he was out (AP; Fortune, 19 Oct 2026). Agents on Meta&rsquo;s platforms need a control
            layer. Delegate is that layer, built on Muse Spark.
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

      <section className="fits" aria-labelledby="fits-title" data-testid="where-it-fits">
        <h2 id="fits-title">Where Delegate fits</h2>
        <ol className="fits-flow">
          {FITS.map((f, i) => (
            <li key={f.title} className={`fits-box${f.here ? " fits-here" : ""}`}>
              <div className="fits-head">
                <Icon name={f.icon} size={20} />
                <h3>{f.title}</h3>
              </div>
              <p>{f.text}</p>
              <p className="fits-tag">{f.tag}</p>
              {i < FITS.length - 1 ? <span className="fits-arrow" aria-hidden="true"><Icon name="arrowRight" size={20} /></span> : null}
            </li>
          ))}
        </ol>
        <p className="fits-note">
          In this demo the buyer&rsquo;s chat is simulated. The approval screen is deliberately outside the chat: a typed
          &ldquo;yes&rdquo; in a conversation never counts as the seller&rsquo;s approval.
        </p>
      </section>

      <section className="lessons" aria-labelledby="lessons-title" data-testid="incident-map">
        <h2 id="lessons-title">What the reported failures need, and the check that provides it</h2>
        <table className="lessons-table">
          <thead>
            <tr>
              <th scope="col">Reported</th>
              <th scope="col">In Delegate</th>
            </tr>
          </thead>
          <tbody>
            {LESSONS.map(([a, b]) => (
              <tr key={a}>
                <td>{a}</td>
                <td>{b}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
