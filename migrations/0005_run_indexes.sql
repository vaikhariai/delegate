-- Per-run lookups used on every view and gate evaluation.
CREATE INDEX IF NOT EXISTS buyer_events_run ON buyer_events (run_id, created_at);
CREATE INDEX IF NOT EXISTS messages_run ON marketplace_messages (run_id, created_at);
CREATE INDEX IF NOT EXISTS offers_run ON marketplace_offers (run_id, status);
CREATE INDEX IF NOT EXISTS actions_run ON proposed_actions (run_id, step_no, attempt);
CREATE INDEX IF NOT EXISTS approvals_run ON approvals (run_id, status);
CREATE INDEX IF NOT EXISTS reservations_run ON marketplace_reservations (run_id);
CREATE INDEX IF NOT EXISTS receipts_run ON receipts (run_id, verified_at);
CREATE INDEX IF NOT EXISTS calls_run ON provider_calls (run_id, created_at);
CREATE INDEX IF NOT EXISTS calls_created ON provider_calls (created_at) WHERE mode = 'live';
CREATE INDEX IF NOT EXISTS decisions_action ON decisions (action_id);
CREATE INDEX IF NOT EXISTS events_session_run ON audit_events (session_id, run_id, seq);
