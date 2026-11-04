# Delegate

**Let an agent negotiate for you. You set the limits, the server enforces them, you approve the one commitment, and you get proof of what happened.**

Delegate is a control layer for AI agents that act on a person's behalf, shown on one concrete job: selling a used item on a peer-to-peer marketplace. A seller gives the agent a mandate (asking price, a private minimum, pickup only, cash at pickup, which facts may be shared). For every buyer message, Meta's Muse Spark proposes one typed action. The server, not the model, decides whether that action may run, in ten fixed checks, and buyers only ever see fixed template text. A reservation needs the buyer's explicit "Accept offer" click and the seller's approval of that exact action; the server writes it once, reads it back, and only then shows a verified receipt. The seller can pause or revoke at any moment.

> Everything here is a sandbox: fictional item, people and money in an application-owned marketplace. A reservation is not a sale, a payment or a handover.

## Why

In September 2026 a Toronto seller let Meta's Muse agent handle a Marketplace sale. It shared his home address through a reply template he had set to "always allow", sent "Yep, I'm here!" from his account when the buyer arrived, and on another listing appeared to offer $100 under his minimum; Meta acknowledged the pricing error to AP. Sources and exact quotes: [`docs/INCIDENT_SOURCES.md`](docs/INCIDENT_SOURCES.md). The model was capable; what was missing was a layer the agent cannot talk its way past:

| Reported | What Delegate does instead |
| --- | --- |
| Appeared to offer a price under the seller's minimum | The private minimum is checked by the server on every counter and reservation; the model never sees it |
| Shared the home address through an "always allow" reply template | No template contains the seller's address; buyers only see the public pickup area |
| Sent "Yep, I'm here!" from the seller's account | The model cannot write buyer-facing text; replies are fixed templates and none claims the seller is present |
| An "always allow" setting was treated as approval of each action | Approval binds one exact reservation (buyer, price, pickup, hold) by a hash the server recomputes before executing |

## How it works

- **Buyer channel**: where buyers talk. In this release the buyer chat is simulated in the web app. A WhatsApp Business Cloud API webhook adapter is included (`services/api/delegate/channels/`), fixture-tested and off by default; it is not connected to a live business number.
- **Gate (server)**: Muse Spark proposes one typed action per buyer message (answer with public facts, counter at a price, clarify, decline for a fixed reason, propose a reservation of an accepted offer, or wait). The server parses it strictly, runs ten ordered checks ([`docs/POLICY.md`](docs/POLICY.md)) and renders only fixed templates in English or Hinglish.
- **Seller console (web app)**: the mandate, approval of one exact reservation, revoke, and the verified receipt. Approval is deliberately outside the chat: a typed "yes" never counts.

Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Security and data handling: [`docs/SECURITY_AND_DATA.md`](docs/SECURITY_AND_DATA.md).

## Meta models used

| Where | Model | How |
| --- | --- | --- |
| Negotiation step (every buyer turn) | `muse-spark-1.3` | Meta Model API Chat Completions at `https://api.meta.ai/v1`, `response_format: json_schema` for the typed proposal, one read-only client tool `preview_action` |
| Voice-note mandate draft | `muse-spark-1.2` | Chat Completions with `input_audio` (WAV); returns suggested public fields and ambiguities only |
| Text mandate draft | `muse-spark-1.3` | Chat Completions with `response_format: json_schema` |

The key is held server-side only (`MODEL_API_KEY`). The app labels its mode everywhere: "Live model: Meta Muse Spark", "Live model unavailable", or "MOCK_ONLY: local rules, not a model" for offline development. There is no silent fallback from live to mock. Details: [`docs/META_INTEGRATION.md`](docs/META_INTEGRATION.md).

## What works

- New isolated demo session per visitor, with per-network limits and a 24-hour expiry.
- Mandate review, edit as a new revision with `If-Match`, explicit activation, pause, resume, revoke; optional voice-note drafting in English or Hindi.
- Agent turns through a durable leased worker with fencing tokens; provider calls never hold a database transaction.
- Ten ordered gate checks, strict proposal parsing, fixed renderers in English and Hinglish.
- Deterministic scam-buyer signals (QR "to receive money", fake payment proof, authority claims, shipping, private-info requests) that block reservations even if the model is fooled. They match known phrasings only; scams worded differently are blocked only if the model flags the buyer.
- Exact-action approval bound to a canonical SHA-256 hash, revision, owner and expiry; single use.
- One locked transaction for the final check and the effect; idempotent operations; reconciliation after an unknown commit.
- Outcome verifier that reads the reservation back and returns `verified`, `mismatch` or `unknown`.
- Activity stream over SSE with replay, owner and buyer evidence exports, and a gate lab that replays every frozen policy case with a per-check trace.

## Evaluation

[`evaluation/`](evaluation/) holds a frozen scenario catalogue, an oracle written from the policy independently of the gate code, a keyword-parser baseline, the gate harness and the live evaluation runner. Published results: [`evaluation/RESULTS.json`](evaluation/RESULTS.json), [`evaluation/REPORT.md`](evaluation/REPORT.md) and what they mean in [`evaluation/LIVE_FINDINGS.md`](evaluation/LIVE_FINDINGS.md). Raw episode files are not included; the runner below writes new ones to `evaluation/results/`.

In 106 live negotiations with Muse Spark (52 scripted plus the 18 adversarial probes repeated three times), Delegate committed no forbidden effect. The same model with the mandate checks turned off, inside a private evaluation process only, reserved for a scam buyer 8 times and without the seller's approval 2 times. On the harder language probes a keyword parser with the same checks passed 2 of 24; Muse Spark passed 23 of 24. The claim is safety, not accuracy, and this is a small pilot on probes written for this project, not a statistical estimate. `LIVE_FINDINGS.md` lists what the comparison can and cannot show.

To rerun:

```bash
uv run python evaluation/gate_harness.py --write  # 151 deterministic gate cases, no model
uv run python evaluation/live_eval.py --systems b0 # keyword baseline, no model
# Live runs call the Meta Model API with your own key (MODEL_API_KEY) and are capped by --max-calls:
uv run python evaluation/live_eval.py --systems d,b1 --max-calls 450
uv run python evaluation/aggregate.py              # writes RESULTS.json and REPORT.md from the newest run files
```

## Run locally

Requirements: Python 3.12 with `uv`, Node 22 with npm 10.

```bash
uv sync
(cd apps/web && npm ci && npm run build)
# Live mode needs a Meta Model API key in MODEL_API_KEY or ~/.config/delegate/model_api_key.
uv run uvicorn delegate.app:create_app --factory --app-dir services/api --port 8000
# Offline, clearly labelled MOCK_ONLY:
DELEGATE_PROVIDER_MODE=mock uv run uvicorn delegate.app:create_app --factory --app-dir services/api --port 8000
```

PostgreSQL starts embedded (`pgserver`) under `.pgdata/` unless `DATABASE_URL` is set. `.env.example` lists the settings.

Container: `docker build -t delegate .` builds the web app and the API into one image that listens on port 8080; pass `MODEL_API_KEY` at run time.

## Tests

```bash
uv run pytest            # unit, integration and property tests against real PostgreSQL, the package and the evaluation catalogue
uv run mypy              # strict
uv run ruff check services packages tests
(cd apps/web && npm run typecheck && npx playwright test)   # browser tests against a local mock-mode server
scripts/secret_scan.sh   # tracked files contain no secrets
scripts/release_check.sh # all of the above from a clean clone of a commit
```

## Repository

| Path | Contents |
| --- | --- |
| `services/api/delegate/` | FastAPI app, gate (`policy.py`), executor, runtime, Meta adapter, projections, renderers, risk scanner, channel adapters |
| `packages/mandate_gate/` | The reusable gate as a standalone package: ordered checks, canonical action hashing, approval binding, with examples in other domains |
| `apps/web/` | React + TypeScript web app (seller console and buyer simulator) |
| `migrations/` | PostgreSQL schema |
| `evaluation/` | Scenario catalogue, oracle, baseline, harnesses, results |
| `tests/` | Python tests (real PostgreSQL) and Playwright browser tests |
| `fixtures/` | Fictional demo listing and synthetic voice notes used by the tests |
| `docs/` | Policy, architecture, Meta integration, security and data, incident sources |

## Limits of this release

- **Sandbox only.** Fictional item, people and money.
- **Embedded database, one instance.** The default deployment runs PostgreSQL inside a single container; state is lost on restart. Point `DATABASE_URL` at a managed PostgreSQL for anything durable; the code already uses row locks, leases and fencing that work across instances.
- **Anonymous sessions.** Each visitor gets a per-session bearer token; there are no accounts. In the demo one person plays both buyer and seller, so both share that token; a real deployment would give the buyer channel no owner token.
- **Simulated buyer channel.** The WhatsApp adapter is fixture-tested only.
- **Approvals expire after two minutes**, after which the agent may propose the reservation again, within the run's call budget and the 30-minute mandate.
- **A scam flag cannot be overridden** by the seller for that buyer in that negotiation.
- **Small evaluation** on probes written for this project; see `evaluation/LIVE_FINDINGS.md`.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
