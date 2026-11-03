-- Channel adapter seam (WhatsApp Business Cloud API). Unused unless the adapter is enabled.
-- A binding ties one external buyer (for WhatsApp, the wa_id) to one negotiation run.
CREATE TABLE channel_bindings (
  channel          text NOT NULL,
  external_user    text NOT NULL,
  session_id       text NOT NULL,
  run_id           text NOT NULL,
  last_inbound_at  timestamptz,
  created_at       timestamptz NOT NULL,
  PRIMARY KEY (channel, external_user),
  UNIQUE (run_id),
  FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, run_id) ON DELETE CASCADE
);

-- Every inbound message ID seen, so a redelivered or replayed webhook is processed at most once.
CREATE TABLE channel_inbound (
  channel              text NOT NULL,
  external_message_id  text NOT NULL,
  received_at          timestamptz NOT NULL,
  outcome              text NOT NULL,
  PRIMARY KEY (channel, external_message_id)
);

-- One delivery record per seller message sent (or refused) on an external channel.
CREATE TABLE channel_deliveries (
  message_id           text PRIMARY KEY REFERENCES marketplace_messages(message_id) ON DELETE CASCADE,
  channel              text NOT NULL,
  external_user        text NOT NULL,
  status               text NOT NULL CHECK (status IN ('sent', 'outside_window', 'error')),
  provider_message_id  text,
  detail               text,
  created_at           timestamptz NOT NULL
);
