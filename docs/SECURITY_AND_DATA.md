# Security and data

## Threat model

In scope: a malicious buyer (prompt injection, authority claims, scams), a malformed or adversarial model proposal, a curious visitor probing other sessions, replayed or altered approvals, duplicate or stale workers, provider failures, and leaks through logs or exports. Out of scope: a hostile operator of the server itself; the owner controls the database, so receipts are evidence about this sandbox, not third-party attestation.

## Controls

| Boundary | Control | Test |
| --- | --- | --- |
| Session identity | Opaque 256-bit bearer token, stored as SHA-256; every query filtered by the authenticated session; other sessions' IDs return 404 | `test_cross_session_access_is_404` |
| Buyer vs owner | Buyer text is stored as untrusted data and only reaches the model inside the conversation; owner operations are separate routes | `test_late_model_output_after_revocation_is_stale`, gate cases D11–D13 |
| Private terms vs model and buyer | Explicit projections; private address, phone and notes live in a separate table never joined into public views; canary strings checked in the exact provider request body | `test_model_request_contains_no_private_values`, `test_buyer_view_and_buyer_evidence_have_no_private_values` |
| Model vs execution | Strict parser, ten ordered gate checks, fixed templates, final recheck under lock | 151 frozen gate cases; integration tests |
| Approval vs mutation | Hash, revision, owner, expiry, single use, consumed in the effect transaction | `test_changed_hash_cannot_approve`, `test_expired_approval_cannot_execute`, `test_concurrent_double_approval_reserves_once` |
| Scam buyers | Deterministic scanner plus model signals; high-severity signal hard-denies reservation | `test_scam_buyer_reservation_is_hard_denied_even_if_model_proposes_it`, D41–D46 |
| Web | Strict request models (unknown fields rejected), length limits, CSP `script-src 'self'`, `X-Frame-Options: DENY`, `nosniff`, no cookies (no CSRF surface), buyer text rendered as text | `test_strict_request_validation_and_error_envelope` |
| Abuse | New sessions per network per hour, buyer turns per run, provider calls per run and session, global provider concurrency | `test_session_creation_is_rate_limited` |
| Secrets | Meta key server-side only, from Secret Manager in production; never logged or exported | provider evidence test checks the export |

| Spend | Every live provider request first reserves one call atomically against the run (24), session (60) and daily (`DELEGATE_MAX_LIVE_CALLS_PER_DAY`, 1500) caps; failed calls are recorded too; the daily counter survives session deletion | `tests/integration/test_adversarial_fixes.py` |
| Scam text | Scanner normalizes NFKC, lookalike letters, invisible characters and digits inside words, covers English, Hinglish and Devanagari phrasings, and also scans the last three buyer messages together; the model's signals are added and shown to the owner | 19 bypasses caught, 18 ordinary messages not flagged, 0/34 legitimate probes flagged |
| Text safety | Control characters and unpaired surrogates rejected; zero-width and bidi characters stripped at ingest; database errors never log bound values | `test_control_and_format_characters` |

Known limits: the private minimum never appears in buyer or model channels, and the preview tool ignores it, but a persistent buyer can still infer roughly where it is from accept/decline behaviour (about one bit per message; see `POLICY.md` section 7). IP-based limits are coarse and are not identity. Two code reviews (3 Nov 2026) found no authorization, cross-session or disclosure defects; all their other findings are fixed with regression tests.

## Data

All listing, people and prices are fictional (`fixtures/demo_case.json`). Visitors type buyer messages and may record a voice note for the mandate draft. Voice notes are sent to the Meta Model API (Standard mode) for that one request and are not stored by the application; the transcript returned by the model is shown to the owner and not persisted beyond the draft response. Sessions and all their rows are deleted on "Reset" or 24 hours after creation. Google Cloud request logs for the service record client IP addresses and paths under Google Cloud's default retention; the application's own structured logs record timings, status codes and hashed session references, never message contents, tokens or private values (tested in `tests/integration/test_observability.py`).

Do not enter real personal information in the demo.
