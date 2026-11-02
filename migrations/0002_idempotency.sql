-- Idempotent API mutations: the same (session, key) returns the stored response instead of acting twice.
CREATE TABLE api_idempotency (
  session_id   text NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
  idem_key     text NOT NULL,
  route        text NOT NULL,
  status_code  integer NOT NULL,
  response     jsonb NOT NULL,
  created_at   timestamptz NOT NULL,
  PRIMARY KEY (session_id, idem_key)
);
