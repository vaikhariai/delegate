-- Atomic spend accounting. Rows here are never deleted with a session, so deleting a session cannot reset the daily cap.
CREATE TABLE daily_usage (
  day          date PRIMARY KEY,
  live_calls   integer NOT NULL DEFAULT 0
);
