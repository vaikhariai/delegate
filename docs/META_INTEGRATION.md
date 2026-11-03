# Meta integration

## Models and endpoints

| Use | Model | Endpoint | Features used |
| --- | --- | --- | --- |
| Negotiation step | `muse-spark-1.3` | `POST https://api.meta.ai/v1/chat/completions` | `response_format: json_schema` (strict), client function tool `preview_action`, `parallel_tool_calls: false`, `reasoning_effort: low`, `max_completion_tokens: 4000` |
| Voice-note mandate draft | `muse-spark-1.2` | same | `input_audio` (WAV, base64), `response_format: json_schema` |
| Text mandate draft | `muse-spark-1.3` | same | `response_format: json_schema` |

Sources: dev.meta.ai docs for models, chat completions, tool calling and structured output, read 2026-11-02. The docs recommend `muse-spark-1.2` for audio input. All are Standard-mode models; all demo data is fictional.

## What the model does and does not do

The model reads the public listing, the conversation, the current public offer, explicit acceptances and the last safe feedback code. It chooses one typed action: answer with cited public facts, counter at a price, ask a clarifying question on a fixed topic, decline for a fixed reason, propose a reservation of an accepted offer, or wait. It also reports buyer risk signals and a one-line rationale for the seller.

It never writes buyer-facing text, never sees the minimum price, address, phone or notes, and has no tool that can change authority, approve, or execute. `preview_action` is read-only: it runs the gate against current state, with the private minimum removed, and returns a verdict and a safe message, so it cannot be used to probe the floor. Previews are capped at one per step because each is a full model round trip (6–14 s on the live API).

## Bounds

- Per step: up to three proposals (one plus two corrections after invalid output) and at most two gate denials before the run is marked blocked with a visible retry.
- Per run: 24 provider calls. Per session: 60 live calls. Global concurrency: 4.
- Transient errors (timeout, network, 429, 5xx) retry twice with backoff; 401/403 and 400 do not.
- If a request with tools and a response schema is rejected with HTTP 400, the adapter retries once without tools and remembers that for the process. This is recorded in the call log as `retry_without_tools`.

## Evidence

Every provider call is stored in `provider_calls` with model, mode, prompt version, HTTP status, latency, token usage and the provider request ID, but never the key or full raw response. Owner evidence exports include these records. `scripts/probe_meta.py` writes the live probe to `evidence/PROVIDER_PROBE.json`.

## Status

See `STATUS.md` for whether the live probe has run. Until a key exists the deployed app shows "Live model unavailable" and nothing is presented as live.
