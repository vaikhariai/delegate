-- Every buyer message or acceptance increments input_seq; a worker clears needs_step only if input_seq is unchanged
-- since its claim, evaluated atomically in the same UPDATE (fixes a lost-update race on needs_step).
ALTER TABLE runs ADD COLUMN input_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN claimed_input_seq bigint;
ALTER TABLE api_idempotency ADD COLUMN request_hash text NOT NULL DEFAULT '';
