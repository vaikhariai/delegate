"""Structured logging for Cloud Logging (one JSON object per line on stdout).

Fields follow the Cloud Logging structured-log conventions (`severity`, `message`, `httpRequest`). Logs never contain
buyer or seller text, tokens, keys or private mandate values. Session IDs are hashed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sys
from typing import Any

_RESERVED = set(logging.LogRecord("x", 0, "x", 0, "x", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "severity": record.levelname,
            "time": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(out, default=str, ensure_ascii=False)


def configure() -> None:
    """JSON logs when DELEGATE_LOG_FORMAT=json (set in the container); plain text otherwise."""
    if os.environ.get("DELEGATE_LOG_FORMAT", "text") != "json":
        logging.basicConfig(level=logging.INFO)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).disabled = True  # replaced by our request log, which carries the request ID


def session_ref(session_id: str | None) -> str | None:
    """A short non-reversible reference for correlating logs of one session."""
    if not session_id:
        return None
    return hashlib.sha256(f"delegate-log:{session_id}".encode()).hexdigest()[:12]
