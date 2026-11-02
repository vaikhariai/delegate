"""FastAPI application: routes, auth, error envelope, idempotency, SSE, static web app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text

from delegate import executor, projections, store
from delegate.clock import Clock
from delegate.db import exe, make_engine, migrate, one, tx
from delegate.meta_adapter import MetaAdapter, ProviderError
from delegate.runtime import Worker
from delegate.settings import Settings

log = logging.getLogger("delegate.app")
ROOT = Path(__file__).resolve().parents[3]


def release_sha() -> str:
    env = os.environ.get("DELEGATE_RELEASE")
    if env:
        return env
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartRunBody(Strict):
    buyer_id: str = "buyer-riya"


class BuyerTurnBody(Strict):
    text: str = Field(max_length=600)


class AcceptBody(Strict):
    offer_id: str


class ActivateBody(Strict):
    revision: int


class ResolveBody(Strict):
    decision: str
    action_hash: str


class DraftBody(Strict):
    goal: str = Field(max_length=1000)


def create_app(settings: Settings | None = None, clock: Clock | None = None, engine: Engine | None = None,
               start_worker: bool | None = None, adapter: MetaAdapter | None = None) -> FastAPI:
    settings = settings or Settings()
    clock = clock or Clock()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        eng = engine or make_engine(settings.database_url, settings.pgdata_dir)
        migrate(eng)
        state["engine"] = eng
        state["adapter"] = adapter or MetaAdapter(settings)
        state["release"] = release_sha()
        run_worker = settings.run_worker if start_worker is None else start_worker
        if run_worker:
            state["worker"] = Worker(eng, clock, settings, state["adapter"])
            state["worker"].start()
        log.info("delegate started release=%s provider_mode=%s", state["release"], state["adapter"].mode)
        yield
        if "worker" in state:
            state["worker"].stop()

    app = FastAPI(title="Delegate API", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    def eng() -> Engine:
        e: Engine = state["engine"]
        return e

    # ------------------------------------------------------------ errors

    @app.exception_handler(store.ApiError)
    async def api_error(request: Request, exc: store.ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message, "retryable": exc.retryable,
                                                           "request_id": request.state.request_id})

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"code": exc.code, "message": str(exc), "retryable": exc.retryable,
                                                      "request_id": request.state.request_id})

    @app.middleware("http")
    async def request_id_and_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request.state.request_id = uuid.uuid4().hex[:16]
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception("unhandled error")
            response = JSONResponse(status_code=500, content={"code": "INTERNAL", "message": "Something went wrong.", "retryable": True,
                                                              "request_id": request.state.request_id})
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'"
        )
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse(status_code=422, content={"code": "INVALID_REQUEST", "message": f"Invalid request: {loc} {first.get('msg', '')}".strip(),
                                                      "retryable": False, "request_id": request.state.request_id})

    # ------------------------------------------------------------ auth and idempotency

    def principal(authorization: str | None = Header(default=None)) -> store.Principal:
        token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        return store.authenticate(eng(), clock, token)

    def idempotent(p: store.Principal, key: str | None, route: str, fn: Callable[[], Any], status: int = 200) -> JSONResponse:
        if not key:
            return JSONResponse(status_code=status, content=fn())
        if len(key) > 100:
            raise store.ApiError(400, "BAD_IDEMPOTENCY_KEY", "Idempotency key too long.")
        with eng().connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": f"{p.session_id}:{key}"})
            try:
                with eng().connect() as c:
                    hit = one(c, "SELECT route, status_code, response FROM api_idempotency WHERE session_id = :s AND idem_key = :k", s=p.session_id, k=key)
                if hit is not None:
                    if hit["route"] != route:
                        raise store.ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This idempotency key was used for a different request.")
                    return JSONResponse(status_code=hit["status_code"], content=hit["response"], headers={"Idempotent-Replay": "true"})
                result = fn()
                with tx(eng()) as c:
                    exe(c, "INSERT INTO api_idempotency (session_id, idem_key, route, status_code, response, created_at) "
                           "VALUES (:s, :k, :r, :st, CAST(:resp AS jsonb), :n)",
                        s=p.session_id, k=key, r=route, st=status, resp=result, n=clock.now())
                return JSONResponse(status_code=status, content=result)
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"{p.session_id}:{key}"})
                lock_conn.commit()

    # ------------------------------------------------------------ health and meta

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with eng().connect() as c:
                c.execute(text("SELECT 1"))
            db = "ok"
        except Exception:  # noqa: BLE001
            db = "error"
        mode = state["adapter"].mode
        ok = db == "ok"
        return JSONResponse(status_code=200 if ok else 503, content={"database": db, "provider_mode": mode, "release": state.get("release")})

    @app.get("/v1/meta")
    def meta() -> dict[str, Any]:
        mode = state["adapter"].mode
        return {
            "product": "Delegate",
            "release": state.get("release"),
            "provider_mode": mode,
            "provider_label": {"live": "Live model: Meta Muse Spark", "mock": "MOCK_ONLY: local rules, not a model",
                               "unavailable": "Live model unavailable"}[mode],
            "models": {"negotiation": settings.model, "voice_note": settings.audio_model},
            "sandbox_label": "Marketplace sandbox: fictional item, people and money. A reservation here is not a sale or payment.",
            "buyers": store.FIXTURE["buyers"],
        }

    # ------------------------------------------------------------ sessions

    @app.post("/v1/demo-sessions", status_code=201)
    def create_session(request: Request) -> dict[str, Any]:
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
        sid, token = store.create_session(eng(), clock, ip, settings.sessions_per_ip_per_hour, settings.session_ttl_s)
        return {"session_id": sid, "token": token, "item_id": store.ITEM_ID}

    @app.get("/v1/demo-sessions/current")
    def current_session(p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            s = one(c, "SELECT session_id, created_at, expires_at, provider_calls, buyer_turns FROM demo_sessions WHERE session_id = :s", s=p.session_id)
            assert s is not None
            runs = [dict(r) for r in c.execute(text("SELECT run_id, buyer_id, buyer_display_name, state, created_at FROM runs WHERE session_id = :s ORDER BY created_at"), {"s": p.session_id}).mappings()]
            listing = projections._listing_public(c, p.session_id, store.ITEM_ID)
            mandate = projections.mandate_owner_view(c, p.session_id)
        return {
            "session_id": s["session_id"], "created_at": s["created_at"].isoformat(), "expires_at": s["expires_at"].isoformat(),
            "provider_calls": s["provider_calls"], "buyer_turns": s["buyer_turns"],
            "listing": listing, "mandate": mandate,
            "runs": [{**r, "created_at": r["created_at"].isoformat()} for r in runs],
        }

    @app.delete("/v1/demo-sessions/current", status_code=204)
    def delete_session(p: store.Principal = Depends(principal)) -> Response:
        store.delete_session(eng(), p.session_id)
        return Response(status_code=204)

    @app.get("/v1/listings/{item_id}")
    def listing(item_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            r = one(c, "SELECT 1 AS x FROM listings WHERE session_id = :s AND item_id = :i", s=p.session_id, i=item_id)
            if r is None:
                raise store.ApiError(404, "NOT_FOUND", "Listing not found.")
            return projections._listing_public(c, p.session_id, item_id)

    # ------------------------------------------------------------ mandate

    def _check_mandate(p: store.Principal, mandate_id: str) -> None:
        with eng().connect() as c:
            m = store.mandate_row(c, p.session_id)
        if m["mandate_id"] != mandate_id:
            raise store.ApiError(404, "NOT_FOUND", "Mandate not found.")

    @app.get("/v1/mandates/{mandate_id}")
    def get_mandate(mandate_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        _check_mandate(p, mandate_id)
        with eng().connect() as c:
            return projections.mandate_owner_view(c, p.session_id)

    @app.patch("/v1/mandates/{mandate_id}")
    def patch_mandate(mandate_id: str, body: dict[str, Any], p: store.Principal = Depends(principal),
                      if_match: str | None = Header(default=None)) -> dict[str, Any]:
        _check_mandate(p, mandate_id)
        if if_match is None or not if_match.strip('"').isdigit():
            raise store.ApiError(428, "IF_MATCH_REQUIRED", "Send If-Match with the revision you reviewed.")
        return store.edit_mandate(eng(), clock, p, int(if_match.strip('"')), body)

    @app.post("/v1/mandates/{mandate_id}/activate")
    def activate(mandate_id: str, body: ActivateBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        _check_mandate(p, mandate_id)
        return store.activate(eng(), clock, p, body.revision, settings.mandate_lifetime_s)

    @app.post("/v1/mandates/{mandate_id}/{action}")
    def mandate_action(mandate_id: str, action: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        if action not in ("pause", "resume", "revoke"):
            raise store.ApiError(404, "NOT_FOUND", "Unknown action.")
        _check_mandate(p, mandate_id)
        return store.set_status(eng(), clock, p, action)

    @app.post("/v1/mandate-drafts")
    def draft_text(body: DraftBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        return _draft(p, body.goal, None)

    @app.post("/v1/mandate-drafts/voice")
    async def draft_voice(audio: UploadFile = File(...), p: store.Principal = Depends(principal)) -> dict[str, Any]:
        data = await audio.read(settings.max_audio_bytes + 1)
        if len(data) > settings.max_audio_bytes:
            raise store.ApiError(413, "AUDIO_TOO_LARGE", "Keep the voice note under about 60 seconds.")
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise store.ApiError(415, "UNSUPPORTED_AUDIO", "The voice note must be a WAV recording.")
        return await run_in_threadpool(_draft, p, None, data)

    def _draft(p: store.Principal, goal: str | None, audio: bytes | None) -> dict[str, Any]:
        with eng().connect() as c:
            s = one(c, "SELECT provider_calls FROM demo_sessions WHERE session_id = :s", s=p.session_id)
        if s and int(s["provider_calls"]) >= settings.max_provider_calls_per_session:
            raise store.ApiError(429, "SESSION_QUOTA", "This demo session used its model quota. Start a new scenario.")
        adapter: MetaAdapter = state["adapter"]
        draft, calls = adapter.draft_mandate(goal, audio)
        from delegate.runtime import _record_calls

        _record_calls(eng(), clock, {"session_id": p.session_id, "run_id": None}, 0, calls)
        return _normalize_draft(draft, adapter.mode, calls[0].model if calls else None)

    # ------------------------------------------------------------ runs

    @app.post("/v1/runs", status_code=201)
    def start_run(body: StartRunBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        return store.start_run(eng(), clock, p, body.buyer_id)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            return projections.owner_run_view(c, p.session_id, run_id)

    @app.get("/v1/runs/{run_id}/buyer-view")
    def buyer_view(run_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            return projections.buyer_view(c, p.session_id, run_id)

    @app.post("/v1/runs/{run_id}/buyer-turns", status_code=202)
    def buyer_turn(run_id: str, body: BuyerTurnBody, p: store.Principal = Depends(principal),
                   idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        return idempotent(p, idempotency_key, f"buyer-turns:{run_id}",
                          lambda: store.buyer_turn(eng(), clock, p, run_id, body.text, settings.max_buyer_turns_per_run), 202)

    @app.post("/v1/runs/{run_id}/accept-offer", status_code=202)
    def accept(run_id: str, body: AcceptBody, p: store.Principal = Depends(principal),
               idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        return idempotent(p, idempotency_key, f"accept:{run_id}", lambda: store.accept_offer(eng(), clock, p, run_id, body.offer_id), 202)

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with tx(eng()) as c:
            run = store.get_run(c, p.session_id, run_id, for_update=True)
            if run["state"] != "blocked":
                raise store.ApiError(409, "NOT_BLOCKED", "Only a blocked negotiation can be retried.")
            exe(c, "UPDATE runs SET state = 'running', state_reason = NULL, needs_step = true, last_feedback = NULL, updated_at = :n WHERE run_id = :r",
                n=clock.now(), r=run_id)
            store.append_event(c, clock, p.session_id, "run.state_changed", "owner", "You asked the agent to retry.", run_id=run_id)
        return {"run_id": run_id, "state": "running"}

    @app.post("/v1/approvals/{approval_id}/resolve")
    def resolve(approval_id: str, body: ResolveBody, p: store.Principal = Depends(principal),
                idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        if body.decision not in ("approve", "reject"):
            raise store.ApiError(422, "BAD_DECISION", "Decision must be approve or reject.")
        return idempotent(p, idempotency_key, f"resolve:{approval_id}",
                          lambda: executor.resolve_approval(eng(), clock, p.session_id, approval_id, body.decision, body.action_hash).as_dict())

    @app.get("/v1/runs/{run_id}/evidence")
    def evidence(run_id: str, view: str = Query("owner", pattern="^(owner|buyer)$"), p: store.Principal = Depends(principal)) -> JSONResponse:
        with eng().connect() as c:
            data = projections.evidence(c, p.session_id, run_id, view=view, provider_mode=state["adapter"].mode, release=state.get("release") or "")
        return JSONResponse(content=json.loads(json.dumps(data, default=str)),
                            headers={"Content-Disposition": f'attachment; filename="delegate-evidence-{view}-{run_id}.json"'})

    @app.get("/v1/events")
    def session_events(after: int = 0, run_id: str | None = None, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            if run_id:
                store.get_run(c, p.session_id, run_id)
            return {"events": projections.events(c, p.session_id, after, run_id=run_id, owner=True)}

    @app.get("/v1/runs/{run_id}/events")
    async def sse(run_id: str, request: Request, p: store.Principal = Depends(principal),
                  last_event_id: str | None = Header(default=None), after: int = 0) -> StreamingResponse:
        with eng().connect() as c:
            store.get_run(c, p.session_id, run_id)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def gen() -> AsyncIterator[bytes]:
            nonlocal cursor
            idle = 0.0
            yield b"retry: 2000\n\n"
            while not await request.is_disconnected():
                def fetch(after_seq: int = cursor) -> list[dict[str, Any]]:
                    with eng().connect() as c2:
                        return projections.events(c2, p.session_id, after_seq, run_id=run_id, owner=True)
                evs = await run_in_threadpool(fetch)
                for e in evs:
                    cursor = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
                if evs:
                    idle = 0.0
                else:
                    idle += 0.5
                    if idle >= 15:
                        idle = 0.0
                        yield b": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/v1/evaluations/latest")
    def evaluation_latest() -> JSONResponse:
        path = ROOT / "evaluation" / "RESULTS.json"
        if not path.exists():
            return JSONResponse(status_code=200, content={"status": "no_run_yet"})
        return JSONResponse(content=json.loads(path.read_text()))

    # ------------------------------------------------------------ static web app

    dist = Path(settings.web_dist)
    if not dist.is_absolute():
        dist = ROOT / dist
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


FACT_TOPIC_TO_ID = {"model": "f_model", "memory": "f_memory", "storage": "f_storage", "condition": "f_condition", "battery": "f_battery",
                    "charger": "f_charger", "pickup": "f_pickup", "payment": "f_payment", "availability": "f_availability"}


def _normalize_draft(draft: dict[str, Any], mode: str, model: str | None) -> dict[str, Any]:
    """Turn a model draft into inactive suggestions. Never contains a minimum price."""
    suggestions: dict[str, Any] = {}
    ambiguities = [str(a)[:200] for a in (draft.get("ambiguities") or [])][:6]
    ask = draft.get("asking_price_minor")
    if type(ask) is int and 0 < ask <= 10_000_000:
        if draft.get("currency") == "USD":
            suggestions["asking_price_minor"] = ask
        else:
            ambiguities.append("The asking price had no clear US dollar currency; please confirm it.")
    hold = draft.get("hold_minutes")
    if type(hold) is int and 5 <= hold <= 60:
        suggestions["reservation_hold_seconds"] = hold * 60
    topics = [FACT_TOPIC_TO_ID[t] for t in (draft.get("share_fact_topics") or []) if t in FACT_TOPIC_TO_ID]
    if topics:
        suggestions["allowed_public_fact_ids"] = topics
    if draft.get("pickup_only") is False:
        ambiguities.append("You mentioned something other than pickup. This demo supports pickup only.")
    return {
        "status": "suggestion_only",
        "note": "Suggestions are not active. Review them, enter your private minimum yourself, and activate explicitly.",
        "suggestions": suggestions,
        "ambiguities": ambiguities,
        "transcript": str(draft.get("transcript") or "")[:1000],
        "language_detected": draft.get("language_detected"),
        "provider_mode": mode,
        "model": model,
    }
