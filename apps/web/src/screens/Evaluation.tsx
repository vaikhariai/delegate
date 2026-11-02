import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Icon } from "../components/Icon";
import { navigate } from "../lib";

// What the evaluation will report (spec section 14.5). Shown as definitions only; values come from the server's results file.
const PLANNED: { name: string; def: string }[] = [
  { name: "Valid task completion", def: "Feasible episodes with a verified correct outcome and no forbidden effect." },
  { name: "Forbidden effect rate", def: "Adversarial episodes in which a forbidden effect was actually committed." },
  { name: "Gate-oracle agreement", def: "Deterministic gate decisions matching the hand-written oracle." },
  { name: "False success", def: "Episodes reported complete without matching evidence." },
  { name: "Routine autonomy", def: "Routine replies completed without asking you." },
  { name: "Approval burden", def: "Approvals per feasible episode, required and unnecessary counted separately." },
];

interface Ratio {
  path: string;
  numerator: number;
  denominator: number;
  extra?: string;
}

function humanize(path: string): string {
  return path
    .split(".")
    .filter((p) => !/^\d+$/.test(p))
    .map((p) => p.replace(/_/g, " "))
    .join(" › ");
}

/** Find every {numerator, denominator} (or {passed, total} / {n, of}) object in the results, wherever it sits. */
function findRatios(node: unknown, path: string[] = [], out: Ratio[] = []): Ratio[] {
  if (!node || typeof node !== "object") return out;
  const o = node as Record<string, unknown>;
  const pairs: [string, string][] = [
    ["numerator", "denominator"],
    ["passed", "total"],
    ["n", "of"],
  ];
  for (const [a, b] of pairs) {
    if (typeof o[a] === "number" && typeof o[b] === "number") {
      const label = typeof o.name === "string" ? o.name : typeof o.label === "string" ? o.label : typeof o.metric === "string" ? o.metric : path.join(".");
      const extra = [o.note, o.scope, o.evidence_mode].filter((x): x is string => typeof x === "string").join(" · ");
      out.push({ path: label, numerator: o[a] as number, denominator: o[b] as number, extra: extra || undefined });
      return out;
    }
  }
  for (const [k, v] of Object.entries(o)) findRatios(v, [...path, k], out);
  return out;
}

function stringList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

export function Evaluation() {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evaluation()
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load the evaluation."));
  }, []);

  const noRun = data?.status === "no_run_yet";
  const systems = data && !noRun && data.systems && typeof data.systems === "object" ? (data.systems as Record<string, unknown>) : {};
  const overall = data && !noRun ? findRatios({ ...data, systems: undefined }) : [];
  const perSystem = Object.entries(systems).map(([name, v]) => [name, findRatios(v)] as const);
  const notRun = data ? stringList(data.systems_not_run) : [];
  const suite = data?.test_suite as { tests?: number; failures?: number; errors?: number; skipped?: number } | undefined;
  const ratios = [...overall, ...perSystem.flatMap(([, r]) => r)];
  const limitations = data ? [...stringList(data.limitations), ...stringList((data.summary as Record<string, unknown> | undefined)?.limitations)] : [];
  const meta = data && !noRun
    ? (["status", "schema_version", "generated_at", "created_at", "commit", "release", "policy_version", "provider_mode", "model", "scenarios_sha256", "catalogue_sha256"] as const)
        .filter((k) => data[k] !== undefined && typeof data[k] !== "object")
        .map((k) => [k, String(data[k])] as const)
    : [];

  return (
    <main id="main" className="page page-eval">
      <div className="page-head">
        <div>
          <p className="eyebrow">Evidence</p>
          <h1>Evaluation</h1>
          <p className="sub">Read from the server&rsquo;s latest results file. Every number shows its denominator. Nothing here is hand-entered.</p>
        </div>
      </div>

      {error ? (
        <div className="card empty" role="alert">
          <Icon name="alert" size={32} />
          <h2>Could not load the evaluation</h2>
          <p>{error}</p>
        </div>
      ) : !data ? (
        <p className="loading" role="status">
          Loading results…
        </p>
      ) : noRun ? (
        <div className="eval-empty" data-testid="eval-empty">
          <section className="card empty">
            <Icon name="chart" size={36} />
            <h2>No evaluation run has been published yet</h2>
            <p>
              When the evaluation runs, its results file appears here with every count and denominator. Until then this page shows no scores,
              because there are none to show.
            </p>
          </section>
          <section className="card">
            <h2 className="card-title">What will be measured</h2>
            <ul className="planned">
              {PLANNED.map((p) => (
                <li key={p.name}>
                  <strong>{p.name}</strong>
                  <span>{p.def}</span>
                  <span className="badge badge-neutral">not yet measured</span>
                </li>
              ))}
            </ul>
            <p className="hint">
              Limits that will apply to any result: one fictional workflow, synthetic buyers, a small number of live-model runs. A zero count of
              violations in a small suite is not a guarantee.
            </p>
          </section>
        </div>
      ) : (
        <div className="eval-results" data-testid="eval-results">
          {meta.length ? (
            <dl className="eval-meta card">
              {meta.map(([k, v]) => (
                <div key={k}>
                  <dt>{k.replace(/_/g, " ")}</dt>
                  <dd className={k.includes("sha") || k === "release" || k === "commit" ? "mono" : ""}>{v}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {suite && typeof suite.tests === "number" ? (
            <p className="suite-line" data-testid="eval-suite">
              <Icon name={suite.failures || suite.errors ? "alert" : "checkCircle"} /> Backend test suite: {suite.tests} tests, {suite.failures ?? 0} failures,{" "}
              {suite.errors ?? 0} errors, {suite.skipped ?? 0} skipped.
            </p>
          ) : null}
          {overall.length ? <RatioTable title="Overall" rows={overall} /> : null}
          {overall.length ? (
            <p className="muted">
              Want to see one of those gate decisions?{" "}
              <a
                href="/lab"
                onClick={(e) => {
                  e.preventDefault();
                  navigate("/lab");
                }}
              >
                Replay any case in the gate lab
              </a>
              , edit its proposal and watch which check decides.
            </p>
          ) : null}
          {perSystem.map(([name, rows]) => (rows.length ? <RatioTable key={name} title={`System ${name.toUpperCase()}`} rows={rows} /> : null))}
          {notRun.length ? (
            <p className="muted" data-testid="eval-not-run">
              <Icon name="info" size={18} /> Not run yet, so no numbers are shown for: {notRun.map((n) => n.toUpperCase()).join(", ")}.
            </p>
          ) : null}
          {!ratios.length ? <p className="muted">The results file has no counts with denominators. See the raw file below.</p> : null}
          <section className="card">
            <h2 className="card-title">Limitations</h2>
            {limitations.length ? (
              <ul>
                {limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">The results file lists no limitations. Treat small samples as a pilot, not a guarantee.</p>
            )}
          </section>
          <details className="tech card">
            <summary>Raw results file</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
          </details>
        </div>
      )}

      <div className="row gap wrap">
        <button type="button" className="btn btn-primary" onClick={() => navigate("/lab")} data-testid="eval-open-lab">
          <Icon name="shieldCheck" /> Open the gate lab: replay every case
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => navigate("/")}>
          Back to start
        </button>
      </div>
    </main>
  );
}

function RatioTable({ title, rows }: { title: string; rows: Ratio[] }) {
  return (
    <section className="card">
      <h2 className="card-title">{title}</h2>
      <table className="ratios">
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">Count</th>
            <th scope="col">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.path}-${i}`}>
              <th scope="row">
                {humanize(r.path)}
                {r.extra ? <span className="muted small block">{r.extra}</span> : null}
              </th>
              <td className="num">
                {r.numerator} / {r.denominator}
              </td>
              <td className="num">{r.denominator ? `${((100 * r.numerator) / r.denominator).toFixed(1)}%` : "n/a (no cases)"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
