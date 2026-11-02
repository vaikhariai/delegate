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
      out.push({ path: label, numerator: o[a] as number, denominator: o[b] as number, extra: typeof o.note === "string" ? o.note : undefined });
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
  const ratios = data && !noRun ? findRatios(data) : [];
  const limitations = data ? [...stringList(data.limitations), ...stringList((data.summary as Record<string, unknown> | undefined)?.limitations)] : [];
  const meta = data && !noRun
    ? (["status", "generated_at", "created_at", "release", "policy_version", "provider_mode", "model", "catalogue_sha256"] as const)
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
                  <dd className={k.includes("sha") || k === "release" ? "mono" : ""}>{v}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {ratios.length ? (
            <section className="card">
              <h2 className="card-title">Results with denominators</h2>
              <table className="ratios">
                <thead>
                  <tr>
                    <th scope="col">Measure</th>
                    <th scope="col">Count</th>
                    <th scope="col">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {ratios.map((r, i) => (
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
          ) : (
            <p className="muted">The results file has no counts with denominators. See the raw file below.</p>
          )}
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

      <div className="row gap">
        <button type="button" className="btn btn-secondary" onClick={() => navigate("/")}>
          Back to start
        </button>
      </div>
    </main>
  );
}
