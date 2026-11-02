# Delegate

**Let an agent negotiate for you. You set the limits, the server enforces them, you approve the one commitment, and you get proof of what happened.**

Delegate is a working prototype of agent commerce that holds up in production. A seller hands a fictional used-laptop sale to an agent under an explicit mandate: asking price, a private minimum, pickup only, cash at pickup, which facts may be shared, and a 30-minute lifetime. A Meta Muse Spark model reads the public conversation and proposes typed actions. The server decides whether each action may run. Buyers only ever see reviewed template text. A reservation needs the buyer's explicit "Accept offer" click and the seller's approval of that exact action. The server then reads the reservation back and shows a verified receipt. The seller can pause or revoke at any moment.

Why now: in September 2026 a seller reported that a consumer AI agent handling his Marketplace messages accepted a lowball offer, shared his building address and told a buyer "Yep I'm here" without asking him (Fortune, 19 Oct 2026; TechRepublic; Malwarebytes). Delegate is the controls layer for that situation: limits the model cannot talk its way past, approval bound to the exact commitment, and evidence.

> Everything here is a sandbox: fictional item, people and money in an application-owned marketplace. A reservation is not a sale, a payment or a handover.

## Meta models used

| Where | Model | How |
| --- | --- | --- |
| Negotiation step (every buyer turn) | `muse-spark-1.3` (Standard mode) | Meta Model API Chat Completions at `https://api.meta.ai/v1`, `response_format: json_schema` for the typed proposal, one read-only client tool `preview_action` |
| Voice-note mandate draft | `muse-spark-1.2` (Standard mode) | Chat Completions with `input_audio` (WAV); returns suggested public fields and ambiguities only |
| Text mandate draft | `muse-spark-1.3` | Chat Completions with `response_format: json_schema` |

The key is held server-side only (`MODEL_API_KEY`, from Secret Manager in production). The app labels its mode everywhere: "Live model: Meta Muse Spark", "Live model unavailable", or "MOCK_ONLY: local rules, not a model" for offline development. There is no silent fallback from live to mock.

## What works

- New isolated demo session per visitor, with per-network limits and a 24-hour expiry.
- Mandate review, edit as a new revision with `If-Match`, explicit activation, pause, resume, revoke.
- Agent turns through a durable leased worker with fencing tokens; provider calls never hold a database transaction.
- Eleven ordered gate checks (`docs/POLICY.md`), strict proposal parsing, fixed renderers in English and Hinglish.
- Deterministic scam-buyer signals (QR "to receive money", fake payment proof, authority claims, shipping, private-info requests) that block reservations even if the model is fooled.
- Exact-action approval bound to a canonical SHA-256 hash, revision, owner and expiry; single use.
- One locked transaction for the final check and the effect; idempotent operations; reconciliation after an unknown commit.
- Outcome verifier that reads the reservation back and returns `verified`, `mismatch` or `unknown`.
- Activity stream over SSE with replay, owner and buyer evidence exports.

## Repository

| Path | Contents |
| --- | --- |
| `services/api/delegate/` | FastAPI app, gate (`policy.py`), executor, runtime, Meta adapter, projections, renderers, risk scanner |
| `packages/mandate_gate/` | The reusable open-source gate: ordered checks, canonical action hashing, approval binding (Apache-2.0) |
| `apps/web/` | React + TypeScript web app |
| `migrations/` | PostgreSQL schema |
| `evaluation/` | Frozen scenario catalogue, independent oracle, baseline B0, gate harness, live evaluation runner |
| `tests/` | Unit and integration tests against real PostgreSQL |
| `docs/` | Policy, architecture, decisions, Meta integration, security and data, operations |

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

PostgreSQL starts embedded (`pgserver`) under `.pgdata/` unless `DATABASE_URL` is set.

## Tests

```bash
uv run pytest            # unit, integration (real PostgreSQL), evaluation catalogue
uv run mypy              # strict
uv run python evaluation/gate_harness.py   # 151 frozen gate cases
```

## Licence

Apache-2.0.
