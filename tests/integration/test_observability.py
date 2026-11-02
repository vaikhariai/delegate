"""Logs carry operational fields but never message text, tokens or private values."""

from __future__ import annotations

import io
import json
import logging

from sqlalchemy import Engine

from delegate import runtime
from delegate.clock import Clock
from delegate.obs import JsonFormatter, session_ref
from delegate.store import FIXTURE
from tests.integration.helpers import Harness


def test_structured_logs_are_json_and_free_of_private_content(engine: Engine, real_clock: Clock) -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    h = Harness(engine, real_clock)
    try:
        h.new_session()
        h.activate()
        run_id = h.start_run()
        secret_text = "Is it still available? My number is 9876543210"
        h.say(run_id, secret_text)
        h.step(run_id)
        logging.getLogger("delegate.runtime").info("ops_summary", extra={"event": "ops_summary", **runtime.ops_summary(engine, real_clock)})
    finally:
        h.close()
        root.removeHandler(handler)
        root.setLevel(old_level)
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    events = {line.get("event") for line in lines}
    assert {"request", "provider_call", "ops_summary"} <= events
    req = next(line for line in lines if line.get("event") == "request")
    assert {"requestMethod", "requestUrl", "status", "latency"} <= set(req["httpRequest"]) and req["severity"] == "INFO"
    blob = buf.getvalue()
    for forbidden in (secret_text, "9876543210", h.token, FIXTURE["seller_private"]["exact_address"], "CANARY", "50000"):
        assert forbidden not in blob, forbidden
    assert session_ref("ses_x") == session_ref("ses_x") and session_ref("ses_x") != "ses_x"
