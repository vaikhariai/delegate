-- Delegate schema v1. All timestamps are timestamptz (UTC). Money is integer minor units plus currency.
-- Lock order used by the application: mandates -> listings -> runs -> demo_sessions.

CREATE TABLE demo_sessions (
  session_id      text PRIMARY KEY,
  token_hash      text NOT NULL UNIQUE,
  ip_hash         text NOT NULL,
  created_at      timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL,
  deleted_at      timestamptz,
  event_seq       bigint NOT NULL DEFAULT 0,
  provider_calls  integer NOT NULL DEFAULT 0,
  buyer_turns     integer NOT NULL DEFAULT 0
);

CREATE TABLE rate_buckets (
  ip_hash       text NOT NULL,
  window_start  timestamptz NOT NULL,
  count         integer NOT NULL DEFAULT 0,
  PRIMARY KEY (ip_hash, window_start)
);

CREATE TABLE listings (
  session_id          text NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
  item_id             text NOT NULL,
  title               text NOT NULL,
  currency            text NOT NULL CHECK (currency = 'USD'),
  asking_price_minor  bigint NOT NULL CHECK (asking_price_minor > 0),
  public_facts        jsonb NOT NULL,
  state               text NOT NULL DEFAULT 'listed' CHECK (state IN ('listed', 'reserved')),
  PRIMARY KEY (session_id, item_id)
);

-- Private seller data. Never joined into buyer or model projections.
CREATE TABLE seller_private (
  session_id     text NOT NULL,
  item_id        text NOT NULL,
  exact_address  text NOT NULL,
  phone          text NOT NULL,
  notes          text NOT NULL,
  PRIMARY KEY (session_id, item_id),
  FOREIGN KEY (session_id, item_id) REFERENCES listings(session_id, item_id) ON DELETE CASCADE
);

CREATE TABLE mandates (
  mandate_id        text PRIMARY KEY,
  session_id        text NOT NULL,
  item_id           text NOT NULL,
  status            text NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'revoked', 'expired')),
  current_revision  integer NOT NULL,
  active_revision   integer,
  activated_at      timestamptz,
  expires_at        timestamptz,
  updated_at        timestamptz NOT NULL,
  FOREIGN KEY (session_id, item_id) REFERENCES listings(session_id, item_id) ON DELETE CASCADE,
  UNIQUE (session_id, mandate_id)
);

CREATE TABLE mandate_revisions (
  mandate_id   text NOT NULL REFERENCES mandates(mandate_id) ON DELETE CASCADE,
  revision     integer NOT NULL CHECK (revision >= 1),
  content      jsonb NOT NULL,
  created_at   timestamptz NOT NULL,
  activated_at timestamptz,
  PRIMARY KEY (mandate_id, revision)
);

CREATE TABLE runs (
  run_id              text PRIMARY KEY,
  session_id          text NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
  mandate_id          text NOT NULL REFERENCES mandates(mandate_id) ON DELETE CASCADE,
  item_id             text NOT NULL,
  buyer_id            text NOT NULL,
  buyer_display_name  text NOT NULL,
  system              text NOT NULL DEFAULT 'delegate' CHECK (system IN ('delegate', 'b0')),
  state               text NOT NULL CHECK (state IN ('created', 'running', 'awaiting_buyer', 'waiting_approval',
                        'paused', 'blocked', 'completed', 'failed', 'cancelled')),
  state_reason        text,
  needs_step          boolean NOT NULL DEFAULT false,
  next_step           integer NOT NULL DEFAULT 1,
  lease_owner         text,
  lease_until         timestamptz,
  fence               bigint NOT NULL DEFAULT 0,
  last_feedback       jsonb,
  provider_calls      integer NOT NULL DEFAULT 0,
  invalid_corrections integer NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL,
  updated_at          timestamptz NOT NULL,
  UNIQUE (session_id, run_id)
);
CREATE INDEX runs_needing_work ON runs (needs_step, lease_until) WHERE needs_step;

CREATE TABLE buyer_events (
  event_id        text PRIMARY KEY,
  session_id      text NOT NULL,
  run_id          text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('message', 'offer_acceptance')),
  text            text,
  offer_id        text,
  risk_signals    jsonb NOT NULL DEFAULT '[]',
  used_by_action  text,
  created_at      timestamptz NOT NULL,
  FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE,
  CHECK ((kind = 'message' AND text IS NOT NULL) OR (kind = 'offer_acceptance' AND offer_id IS NOT NULL))
);

CREATE TABLE marketplace_offers (
  offer_id      text PRIMARY KEY,
  session_id    text NOT NULL,
  run_id        text NOT NULL,
  item_id       text NOT NULL,
  price_minor   bigint NOT NULL CHECK (price_minor > 0),
  currency      text NOT NULL CHECK (currency = 'USD'),
  status        text NOT NULL CHECK (status IN ('open', 'superseded', 'accepted', 'withdrawn')),
  source        text NOT NULL CHECK (source IN ('listing', 'counter_offer')),
  source_operation_id text UNIQUE,
  created_at    timestamptz NOT NULL,
  FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
);

CREATE TABLE proposed_actions (
  action_id        text PRIMARY KEY,
  session_id       text NOT NULL,
  run_id           text NOT NULL,
  step_no          integer NOT NULL,
  attempt          integer NOT NULL,
  origin           text NOT NULL CHECK (origin IN ('live', 'mock', 'b0', 'injected_test')),
  kind             text,
  raw_proposal     jsonb,
  validation_error text,
  canonical_body   text NOT NULL,
  action_hash      text NOT NULL,
  mandate_id       text NOT NULL,
  mandate_revision integer NOT NULL,
  status           text NOT NULL CHECK (status IN ('proposed', 'invalid', 'denied', 'waiting_approval', 'authorized',
                     'executing', 'succeeded', 'failed', 'unknown', 'cancelled', 'no_action')),
  created_at       timestamptz NOT NULL,
  expires_at       timestamptz NOT NULL,
  provider_call_id text,
  UNIQUE (run_id, step_no, attempt),
  FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
);

CREATE TABLE decisions (
  decision_id      text PRIMARY KEY,
  session_id       text NOT NULL,
  action_id        text NOT NULL REFERENCES proposed_actions(action_id) ON DELETE CASCADE,
  action_hash      text NOT NULL,
  mandate_revision integer NOT NULL,
  phase            text NOT NULL CHECK (phase IN ('proposal', 'execution')),
  verdict          text NOT NULL CHECK (verdict IN ('allow', 'deny', 'approval_required', 'invalid')),
  reason_code      text,
  decided_by       text,
  passed           jsonb NOT NULL,
  checked_at       timestamptz NOT NULL
);

CREATE TABLE approvals (
  approval_id      text PRIMARY KEY,
  session_id       text NOT NULL,
  run_id           text NOT NULL,
  action_id        text NOT NULL UNIQUE REFERENCES proposed_actions(action_id) ON DELETE CASCADE,
  action_hash      text NOT NULL,
  owner_principal  text NOT NULL,
  mandate_revision integer NOT NULL,
  status           text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')),
  created_at       timestamptz NOT NULL,
  expires_at       timestamptz NOT NULL,
  resolved_at      timestamptz,
  consumed_at      timestamptz
);

CREATE TABLE operations (
  operation_id     text PRIMARY KEY,
  session_id       text NOT NULL,
  action_id        text NOT NULL UNIQUE REFERENCES proposed_actions(action_id) ON DELETE CASCADE,
  idempotency_key  text NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('send_message', 'reserve_item')),
  state            text NOT NULL CHECK (state IN ('committed', 'failed')),
  result           jsonb NOT NULL,
  committed_at     timestamptz NOT NULL,
  UNIQUE (session_id, idempotency_key)
);

CREATE TABLE marketplace_messages (
  message_id          text PRIMARY KEY,
  session_id          text NOT NULL,
  run_id              text NOT NULL,
  role                text NOT NULL CHECK (role IN ('buyer', 'seller', 'system')),
  text                text NOT NULL,
  buyer_event_id      text UNIQUE,
  source_operation_id text UNIQUE,
  template_key        text,
  language            text,
  offer_id            text,
  created_at          timestamptz NOT NULL,
  FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
);

CREATE TABLE marketplace_reservations (
  reservation_id      text PRIMARY KEY,
  session_id          text NOT NULL,
  item_id             text NOT NULL,
  run_id              text NOT NULL,
  buyer_id            text NOT NULL,
  price_minor         bigint NOT NULL CHECK (price_minor > 0),
  currency            text NOT NULL CHECK (currency = 'USD'),
  delivery            text NOT NULL,
  payment_terms       text NOT NULL,
  status              text NOT NULL CHECK (status IN ('active', 'expired', 'cancelled')),
  source_operation_id text NOT NULL UNIQUE,
  action_hash         text NOT NULL,
  acceptance_event_id text NOT NULL UNIQUE,
  created_at          timestamptz NOT NULL,
  expires_at          timestamptz NOT NULL,
  FOREIGN KEY (session_id, item_id) REFERENCES listings(session_id, item_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_active_reservation_per_item
  ON marketplace_reservations (session_id, item_id) WHERE status = 'active';

CREATE TABLE receipts (
  receipt_id      text PRIMARY KEY,
  session_id      text NOT NULL,
  run_id          text NOT NULL,
  operation_id    text NOT NULL,
  reservation_id  text,
  verdict         text NOT NULL CHECK (verdict IN ('verified', 'mismatch', 'unknown')),
  mismatches      jsonb NOT NULL,
  fields          jsonb NOT NULL,
  verified_at     timestamptz NOT NULL
);

CREATE TABLE audit_events (
  session_id   text NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
  seq          bigint NOT NULL,
  run_id       text,
  event_type   text NOT NULL,
  actor_type   text NOT NULL CHECK (actor_type IN ('owner', 'buyer', 'agent', 'gate', 'executor', 'verifier', 'system')),
  object_id    text,
  summary      text NOT NULL,
  data         jsonb NOT NULL DEFAULT '{}',
  owner_only   boolean NOT NULL DEFAULT false,
  occurred_at  timestamptz NOT NULL,
  PRIMARY KEY (session_id, seq)
);

CREATE TABLE provider_calls (
  call_id        text PRIMARY KEY,
  session_id     text,
  run_id         text,
  step_no        integer,
  purpose        text NOT NULL,
  model          text NOT NULL,
  mode           text NOT NULL CHECK (mode IN ('live', 'mock', 'b0')),
  prompt_version text NOT NULL,
  status         text NOT NULL,
  http_status    integer,
  latency_ms     integer,
  usage          jsonb,
  request_id     text,
  error_code     text,
  created_at     timestamptz NOT NULL
);
