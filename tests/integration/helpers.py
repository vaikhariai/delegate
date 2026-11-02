from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from delegate import runtime
from delegate.app import create_app
from delegate.clock import Clock
from delegate.meta_adapter import MetaAdapter
from delegate.settings import Settings


def mock_settings(**over: Any) -> Settings:
    base = Settings(provider_mode="mock", run_worker=False, web_dist="__none__")
    return Settings(**{**base.__dict__, **over})


class Harness:
    """A TestClient app with a synchronous worker for deterministic flows."""

    def __init__(self, engine: Engine, clock: Clock, settings: Settings | None = None, adapter: MetaAdapter | None = None) -> None:
        self.settings = settings or mock_settings()
        self.adapter = adapter or MetaAdapter(self.settings)
        self.engine, self.clock = engine, clock
        self.app = create_app(self.settings, clock, engine, start_worker=False, adapter=self.adapter)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.token = ""

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def h(self, token: str | None = None, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}", **extra}

    def new_session(self) -> dict[str, Any]:
        r = self.client.post("/v1/demo-sessions")
        assert r.status_code == 201, r.text
        data: dict[str, Any] = r.json()
        self.token = data["token"]
        return data

    def current(self, token: str | None = None) -> dict[str, Any]:
        r = self.client.get("/v1/demo-sessions/current", headers=self.h(token))
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    def activate(self) -> str:
        m = self.current()["mandate"]
        r = self.client.post(f"/v1/mandates/{m['mandate_id']}/activate", json={"revision": m["current_revision"]}, headers=self.h())
        assert r.status_code == 200, r.text
        return str(m["mandate_id"])

    def start_run(self, buyer: str = "buyer-riya") -> str:
        r = self.client.post("/v1/runs", json={"buyer_id": buyer}, headers=self.h())
        assert r.status_code == 201, r.text
        return str(r.json()["run_id"])

    def say(self, run_id: str, text: str, key: str | None = None) -> Any:
        headers = self.h(**({"Idempotency-Key": key} if key else {}))
        return self.client.post(f"/v1/runs/{run_id}/buyer-turns", json={"text": text}, headers=headers)

    def step(self, run_id: str) -> str:
        run = runtime.claim(self.engine, self.clock, "test-worker", run_id)
        assert run is not None, "nothing to claim"
        return runtime.process(self.engine, self.clock, self.settings, self.adapter, run)

    def run_view(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/v1/runs/{run_id}", headers=self.h())
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out
