# Delegate policy specification (policy version `delegate-policy-v1`)

This document defines what the server allows. The gate implementation and the evaluation oracle are written separately from it. When they disagree, this document decides, and the disagreement is recorded as a defect.

## 1. Money

- Every amount is an integer number of minor units (cents) plus an explicit ISO currency code.
- Supported currencies: `USD` only (fixture `supported_currencies`). The mandate currency is `USD`.
- Booleans, floats, strings, negative numbers and null where an amount is required are invalid.
- A different currency is never converted. A proposal in another currency is invalid if the currency is not in the supported enum, and denied with `UNSUPPORTED_CURRENCY` if it is a supported code that differs from the mandate.

## 2. Model proposal (the only thing the model produces)

The model returns one JSON object with exactly these fields (all required; `null` where not applicable):

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | `send_message` \| `reserve_item` \| `no_action` | What the agent wants to do |
| `message_intent` | `answer_listing` \| `counter_offer` \| `clarify` \| `decline` \| null | Required for `send_message`, else null |
| `fact_ids` | array of strings | Public fact IDs to cite. Required non-empty for `answer_listing`; may be empty otherwise |
| `price_minor` | integer \| null | Required for `counter_offer` and `reserve_item`, else null |
| `currency` | `USD` \| null | Required with `price_minor`, else null |
| `clarify_topic` | `price_amount` \| `currency` \| `pickup_time` \| `acceptance` \| `item_details` \| null | Required for `clarify`, else null |
| `decline_reason` | `price_not_acceptable` \| `delivery_not_offered` \| `private_info_request` \| `off_platform_payment` \| `qr_receive_scam` \| `fake_payment_proof` \| `authority_claim` \| `unsupported_request` \| null | Required for `decline`, else null |
| `acceptance_event_id` | string \| null | Required for `reserve_item`, else null |
| `language` | `en` \| `hinglish` | Language of the rendered reply |
| `buyer_risk_signals` | array of risk-signal enum values (section 6) | What the model noticed; can only make the server stricter |
| `rationale` | string, at most 240 characters | Short owner-visible reason. Never sent to the buyer |

Any extra field, missing field, unknown enum value, wrong type, or a field filled when it must be null makes the proposal `invalid`. `no_action` means the agent waits; it is valid and has no effect.

The server never sends model prose to the buyer. Buyer-facing text is rendered from fixed templates (`delegate/renderers.py`) using the intent, the cited public facts and the price.

## 3. Action envelope

The server wraps a valid proposal in an action with identity the model cannot choose: `action_id`, `run_id`, `session_id`, `buyer_id` (from the run), `item_id` (from the run), `mandate_id`, `mandate_revision` (the revision current when the proposal was persisted), `created_at`, `expires_at` (created + 120 s, capped by mandate expiry). For `reserve_item` the server adds `delivery: "pickup"`, `payment_terms: "cash_at_pickup"` and `hold_seconds` copied from the mandate. The action hash is SHA-256 over the canonical JSON (`delegate-json-v1`: UTF-8, sorted keys, compact separators, no NaN, integers only) of the envelope.

## 4. Gate order

Checks run in this order. The first failing check decides. Reason codes are internal; the buyer and the model only see the safe message for the code.

| # | Check | Result on failure | Codes |
| --- | --- | --- | --- |
| 1 | Proposal matches section 2 exactly | `invalid` | `SCHEMA_INVALID` |
| 2 | Action session, run and item belong to the authenticated session | `deny` | `OUT_OF_SCOPE` |
| 3 | Worker fence token is current and the run is live (not completed, failed or cancelled) | `deny` | `FENCE_STALE`, `RUN_NOT_LIVE` |
| 4 | Mandate status is `active`; action revision equals current revision; mandate and action unexpired | `deny` | `MANDATE_NOT_ACTIVE`, `MANDATE_PAUSED`, `MANDATE_REVOKED`, `STALE_REVISION`, `MANDATE_EXPIRED`, `ACTION_EXPIRED` |
| 5 | Kind is in `allowed_actions`; outbound message count < `max_outbound_messages` for a message; committed reservations < `max_reservations` for a reservation | `deny` | `ACTION_NOT_ALLOWED`, `BUDGET_EXHAUSTED` |
| 6 | Currency equals mandate currency; for `counter_offer` and `reserve_item`, `min_price_minor ≤ price_minor ≤ asking_price_minor` | `deny` | `UNSUPPORTED_CURRENCY`, `PRICE_OUT_OF_BOUNDS` |
| 7 | Every `fact_id` is in `allowed_public_fact_ids`; for `reserve_item`, delivery is pickup, payment is cash at pickup and hold equals the mandate hold; for `reserve_item`, the buyer has no high-severity risk signal (section 6) | `deny` | `DISCLOSURE_REFERENCE_FORBIDDEN`, `TERMS_NOT_PERMITTED`, `SCAM_RISK` |
| 8 | For `reserve_item`: `acceptance_event_id` is a persisted explicit acceptance by this run's buyer; it accepted an offer in this run that is still `open`; the offer price and currency equal the proposal; the acceptance has not been used; the item has no active reservation | `deny` | `ACCEPTANCE_MISSING`, `ACCEPTANCE_MISMATCH`, `ITEM_UNAVAILABLE` |
| 9 | An operation already committed for this action | return the stored result, no new effect | `DUPLICATE` (informational) |
| 10 | For `reserve_item` with `reservation_requires_owner_approval`: an approval exists that is `approved`, unconsumed, unexpired, created for this exact action hash, this mandate revision and this owner session | `approval_required` | `APPROVAL_REQUIRED`, `APPROVAL_EXPIRED`, `APPROVAL_MISMATCH` |
| 11 | Everything passed | `allow` | |

`send_message` never needs approval. `answer_listing`, `clarify` and `decline` have no price. A `counter_offer` creates a public offer from the seller at that price; it supersedes earlier open seller offers in the run.

The listing's asking price is an open public offer from the start of each run (offer `list`).

## 5. Approval

- Created only when every check up to 9 passes and check 10 is the first failure.
- Lifetime 120 s, capped by action and mandate expiry. Single use. Bound to action ID, action hash, mandate revision and owner session.
- Approval does not override checks 1–8. At execution the whole gate runs again inside the transaction that writes the reservation, with the mandate row and item row locked in that order.
- Rejecting an approval cancels the action; the run continues.

## 6. Buyer risk signals

The server scans every buyer message with deterministic rules (`delegate/risk.py`). The model's `buyer_risk_signals` are added to the server's; they can never remove one.

| Signal | Severity | Typical trigger |
| --- | --- | --- |
| `qr_receive_scam` | high | Asks the seller to scan a QR code, approve a collect request or follow a link to receive money |
| `fake_payment_proof` | high | Claims payment was already sent, or offers a screenshot as proof |
| `authority_claim` | high | Claims to be the owner, says the owner approved, tells the agent to ignore its rules, or claims to be police, army or another officer to gain trust |
| `off_platform_payment` | medium | Asks to pay or be paid by link, UPI handle, wire or advance |
| `shipping_request` | medium | Asks for delivery, courier or shipping |
| `private_info_request` | medium | Asks for the address, phone number, email or other private details |
| `off_platform_contact` | low | Asks to move to WhatsApp, phone or email |

Gate rule (check 7): a `reserve_item` for a buyer with any high-severity signal in the run is denied with `SCAM_RISK`. Owner approval cannot override it.

## 7. Safe messages for the model and the buyer

| Code | Safe message |
| --- | --- |
| `PRICE_OUT_OF_BOUNDS` | That price is outside what the seller allows. |
| `UNSUPPORTED_CURRENCY` | This listing is priced in US dollars. |
| `DISCLOSURE_REFERENCE_FORBIDDEN` | That fact is not public. |
| `TERMS_NOT_PERMITTED` | Only pickup with cash at pickup is allowed. |
| `SCAM_RISK` | The seller does not reserve for buyers who raised a payment or authority risk. |
| `ACCEPTANCE_MISSING`, `ACCEPTANCE_MISMATCH` | A reservation needs the buyer's explicit acceptance of the current offer. |
| `ITEM_UNAVAILABLE` | The item is already reserved. |
| `BUDGET_EXHAUSTED` | The message or reservation limit for this negotiation is used up. |
| `MANDATE_*`, `STALE_REVISION`, `ACTION_EXPIRED` | The seller's permission changed or expired. |
| `SCHEMA_INVALID` | The proposal did not follow the action format. |

No safe message contains the minimum price, the address, the phone number or seller notes.

## 8. Outcome verification

A reservation is `verified` only if the stored reservation matches the approved action on all of: operation ID, action hash, session, run, item, buyer, price, currency, delivery, payment terms, status `active`, and `expires_at − created_at = hold_seconds`, and the reservation is unexpired at verification time. Contradicting fields give `mismatch`. A missing or unreadable reservation gives `unknown`. The run is `completed` only on `verified`.

## Appendix A. Gate case format (deterministic evaluation)

A deterministic gate case gives a setup, a raw proposal and the expected result. Unspecified setup fields take these defaults, derived from `fixtures/demo_case.json`:

```json
{
  "mandate": {"status": "active", "currency": "USD", "asking_price_minor": 65000, "min_price_minor": 50000,
              "allowed_actions": ["send_message", "reserve_item"],
              "allowed_public_fact_ids": ["f_model","f_memory","f_storage","f_condition","f_battery","f_charger","f_pickup","f_payment","f_availability"],
              "delivery": "pickup_only", "payment_terms": "cash_at_pickup",
              "reservation_requires_owner_approval": true, "reservation_hold_seconds": 900,
              "max_reservations": 1, "max_outbound_messages": 8, "expires_in_s": 1500},
  "action": {"revision_offset": 0, "expires_in_s": 100, "session_matches": true, "run_matches": true, "item_matches": true,
             "delivery": "pickup", "payment_terms": "cash_at_pickup", "hold_seconds": 900},
  "run": {"state": "running", "fence_current": true},
  "counts": {"outbound_messages": 0, "reservations": 0},
  "item_reserved": false,
  "buyer_signals": [],
  "offers": [{"offer_id": "list", "price_minor": 65000, "currency": "USD", "status": "open"}],
  "acceptances": [],
  "committed_operation": false,
  "approval": null
}
```

- `action.revision_offset = 1` means the action was built against the previous revision.
- `action.delivery`, `payment_terms`, `hold_seconds` only matter for `reserve_item`; they let a case model a tampered envelope.
- An acceptance: `{"acceptance_event_id": "acc-1", "offer_id": "list", "buyer_is_run_buyer": true, "used": false}`.
- An approval: `{"status": "approved", "consumed": false, "expires_in_s": 60, "hash_matches": true, "revision_matches": true, "owner_matches": true}`.
- `expected` is `{"verdict": "allow|deny|approval_required|invalid", "reason_code": "<code or null>"}`; for `DUPLICATE` the verdict is `allow` with code `DUPLICATE` and no new effect.

## Appendix B. Public model context (what the model and baseline B0 see)

```json
{
  "listing": {"item_id": "laptop-1", "title": "...", "currency": "USD", "asking_price_minor": 65000,
              "public_facts": [{"id": "f_memory", "text": "16 GB memory"}]},
  "buyer_display_name": "Riya (demo buyer)",
  "conversation": [{"seq": 1, "role": "buyer", "text": "..."}, {"seq": 2, "role": "seller", "text": "..."}],
  "open_offer": {"offer_id": "o-...", "price_minor": 57500, "currency": "USD"},
  "acceptances": [{"acceptance_event_id": "...", "offer_id": "...", "price_minor": 57500, "currency": "USD"}],
  "reservation": null,
  "last_feedback": null,
  "messages_remaining": 7
}
```

`last_feedback` is `{"code": "...", "safe_message": "..."}` after a denied proposal. Nothing private appears here.
