"""FastAPI application: routes, auth, error envelope, idempotency, SSE, static web app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
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

from delegate import budget, executor, lab, obs, projections, signals, store
from delegate.channels import service as channel_service
from delegate.channels import whatsapp
from delegate.channels.base import ChannelError
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


class ChannelBindingBody(Strict):
    channel: str = Field(max_length=20)
    external_user: str = Field(max_length=32)


class BuyerTurnBody(Strict):
    text: str = Field(max_length=600)


class AcceptBody(Strict):
    offer_id: str


class ActivateBody(Strict):
    revision: int


class ResolveBody(Strict):
    decision: str
    action_hash: str


class LabEvalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(max_length=40)
    proposal: Any


class DraftBody(Strict):
    goal: str = Field(max_length=1000)


def client_ip(request: Request) -> str:
    """The address appended by the platform's front end: the right-most X-Forwarded-For hop.

    Left-most entries are client-supplied and must not be trusted for rate limiting.
    """
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"


def _digits(value: str | None, limit: int = 18) -> int | None:
    v = (value or "").strip().strip('"')
    if v and v.isascii() and v.isdigit() and len(v) <= limit:
        return int(v)
    return None


MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_STREAMS_PER_SESSION = 3
_streams: dict[str, int] = {}
_streams_lock = threading.Lock()


def create_app(settings: Settings | None = None, clock: Clock | None = None, engine: Engine | None = None,
               start_worker: bool | None = None, adapter: MetaAdapter | None = None) -> FastAPI:
    settings = settings or Settings()
    clock = clock or Clock()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        obs.configure()
        eng = engine or make_engine(settings.database_url, settings.pgdata_dir, pool_size=20)
        migrate(eng)
        state["engine"] = eng
        state["adapter"] = adapter or MetaAdapter(settings)
        state["release"] = release_sha()
        run_worker = settings.run_worker if start_worker is None else start_worker
        if run_worker:
            # Workers spend nearly all their time waiting on the provider, so several threads serve concurrent users.
            state["worker"] = Worker(eng, clock, settings, state["adapter"], n_threads=settings.worker_threads)
            state["worker"].start()
        stop_channels = threading.Event()
        if settings.whatsapp_enabled and run_worker:
            threading.Thread(target=channel_service.delivery_loop, args=(eng, clock, settings, stop_channels.is_set),
                             name="channel-delivery", daemon=True).start()
        log.info("delegate started", extra={"event": "startup", "release": state["release"], "provider_mode": state["adapter"].mode,
                                            "whatsapp_channel": settings.whatsapp_enabled})
        yield
        stop_channels.set()
        if "worker" in state:
            state["worker"].stop()

    app = FastAPI(title="Delegate API", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Callable[[Request], Any]) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            chunked = "chunked" in request.headers.get("transfer-encoding", "").lower()
            too_big = length is not None and (not length.isascii() or not length.isdigit() or int(length) > MAX_BODY_BYTES)
            if too_big or (length is None and chunked):
                return JSONResponse(status_code=413 if too_big else 411,
                                    content={"code": "BODY_SIZE", "message": "Send a Content-Length of at most 5 MB.", "retryable": False,
                                             "request_id": getattr(request.state, "request_id", None)})
        response: Response = await call_next(request)
        return response

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
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"event": "error", "request_id": request.state.request_id, "path": request.url.path})
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
        if not request.url.path.startswith("/assets/"):
            latency = round((time.perf_counter() - started) * 1000)
            log.info("request", extra={
                "event": "request", "request_id": request.state.request_id, "latency_ms": latency,
                "httpRequest": {"requestMethod": request.method, "requestUrl": request.url.path, "status": response.status_code,
                                "latency": f"{latency / 1000:.3f}s"},
            })
        return response

    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: ("NOT_FOUND", "Not found."), 405: ("METHOD_NOT_ALLOWED", "That method is not allowed here.")}
        code, message = codes.get(exc.status_code, ("HTTP_ERROR", str(exc.detail)))
        return JSONResponse(status_code=exc.status_code, content={"code": code, "message": message, "retryable": False,
                                                                  "request_id": getattr(request.state, "request_id", None)})

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

    def idempotent(p: store.Principal, key: str | None, route: str, fn: Callable[[], Any], status: int = 200,
                   body: Any = None) -> JSONResponse:
        if not key:
            return JSONResponse(status_code=status, content=fn())
        if len(key) > 100:
            raise store.ApiError(400, "BAD_IDEMPOTENCY_KEY", "Idempotency key too long.")
        with eng().connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": f"{p.session_id}:{key}"})
            try:
                with eng().connect() as c:
                    hit = one(c, "SELECT route, status_code, response, request_hash FROM api_idempotency WHERE session_id = :s AND idem_key = :k",
                          s=p.session_id, k=key)
                req_hash = hashlib.sha256(json.dumps([route, body], sort_keys=True, default=str).encode()).hexdigest()
                if hit is not None:
                    if hit["route"] != route or hit["request_hash"] != req_hash:
                        raise store.ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This idempotency key was used for a different request.")
                    return JSONResponse(status_code=hit["status_code"], content=hit["response"], headers={"Idempotent-Replay": "true"})
                result = fn()
                with tx(eng()) as c:
                    exe(c, "INSERT INTO api_idempotency (session_id, idem_key, route, status_code, response, created_at, request_hash) "
                           "VALUES (:s, :k, :r, :st, CAST(:resp AS jsonb), :n, :h)",
                        s=p.session_id, k=key, r=route, st=status, resp=result, n=clock.now(), h=req_hash)
                return JSONResponse(status_code=status, content=result)
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"{p.session_id}:{key}"})
                lock_conn.commit()

    # ------------------------------------------------------------ health and meta

    @app.get("/healthz")
    @app.get("/v1/health")  # Cloud Run's front end reserves /healthz, so probes and monitors use this path
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
        ip = client_ip(request)
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
        rev = _digits(if_match, 6)
        if rev is None:
            raise store.ApiError(428, "IF_MATCH_REQUIRED", "Send If-Match with the revision you reviewed.")
        return store.edit_mandate(eng(), clock, p, rev, body)

    @app.post("/v1/mandates/{mandate_id}/activate")
    def activate(mandate_id: str, body: ActivateBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        _check_mandate(p, mandate_id)
        out = store.activate(eng(), clock, p, body.revision, settings.mandate_lifetime_s)
        signals.notify_work()
        return out

    @app.post("/v1/mandates/{mandate_id}/{action}")
    def mandate_action(mandate_id: str, action: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        if action not in ("pause", "resume", "revoke"):
            raise store.ApiError(404, "NOT_FOUND", "Unknown action.")
        _check_mandate(p, mandate_id)
        out = store.set_status(eng(), clock, p, action)
        signals.notify_work()
        return out

    @app.post("/v1/mandate-drafts")
    def draft_text(body: DraftBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        return _draft(p, store.clean_text(body.goal), None)

    @app.post("/v1/mandate-drafts/voice")
    async def draft_voice(audio: UploadFile = File(...), p: store.Principal = Depends(principal)) -> dict[str, Any]:
        data = await audio.read(settings.max_audio_bytes + 1)
        if len(data) > settings.max_audio_bytes:
            raise store.ApiError(413, "AUDIO_TOO_LARGE", "Keep the voice note under about 60 seconds.")
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise store.ApiError(415, "UNSUPPORTED_AUDIO", "The voice note must be a WAV recording.")
        return await run_in_threadpool(_draft, p, None, data)

    def _draft(p: store.Principal, goal: str | None, audio: bytes | None) -> dict[str, Any]:
        from delegate.runtime import _record_calls

        adapter: MetaAdapter = state["adapter"]
        charge = budget.charger(eng(), clock, settings, p.session_id, None)
        calls: list[Any] = []
        try:
            draft, calls = adapter.draft_mandate(goal, audio, charge=charge)
        except ProviderError as exc:
            calls = exc.calls
            if exc.code in ("MODEL_BUDGET_EXHAUSTED", "DEMO_DAILY_BUDGET_EXHAUSTED"):
                raise store.ApiError(429, exc.code, str(exc)) from exc
            raise
        finally:
            if calls:
                _record_calls(eng(), clock, {"session_id": p.session_id, "run_id": None}, 0, calls)
        used = [c.model for c in calls if c.status in ("ok",)]
        return _normalize_draft(draft, adapter.mode, ", ".join(used) if used else (calls[0].model if calls else None))

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
                          lambda: _woke(store.buyer_turn(eng(), clock, p, run_id, body.text, settings.max_buyer_turns_per_run)), 202,
                          body=body.model_dump())

    if settings.whatsapp_enabled:
        # WhatsApp Business Cloud API adapter. Registered only when DELEGATE_WHATSAPP_ENABLED=1 (off by default).
        @app.get("/v1/channels/whatsapp/webhook", include_in_schema=False)
        def whatsapp_verify(mode: str | None = Query(default=None, alias="hub.mode"),
                            token: str | None = Query(default=None, alias="hub.verify_token"),
                            challenge: str | None = Query(default=None, alias="hub.challenge")) -> Response:
            try:
                return Response(whatsapp.verify_subscription(mode, token, challenge, settings.whatsapp_verify_token), media_type="text/plain")
            except ChannelError as exc:
                raise store.ApiError(403, exc.code, "Webhook verification failed.") from exc

        @app.post("/v1/channels/whatsapp/webhook", include_in_schema=False)
        async def whatsapp_webhook(request: Request) -> dict[str, Any]:
            raw = await request.body()
            if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256"), settings.whatsapp_app_secret):
                raise store.ApiError(403, "BAD_SIGNATURE", "Signature check failed.")
            try:
                msgs = whatsapp.parse_webhook(json.loads(raw))
            except (ValueError, ChannelError) as exc:
                raise store.ApiError(400, "BAD_WEBHOOK", "Not a WhatsApp messages webhook.") from exc
            outcomes = await run_in_threadpool(channel_service.handle_inbound, eng(), clock, settings, msgs)
            if outcomes.get("accepted"):
                signals.notify_work()
            return {"received": len(msgs), "outcomes": outcomes}

        @app.post("/v1/runs/{run_id}/channel-binding", status_code=201)
        def bind_channel(run_id: str, body: ChannelBindingBody, p: store.Principal = Depends(principal)) -> dict[str, Any]:
            return channel_service.bind(eng(), clock, p, run_id, body.channel, body.external_user)

    @app.post("/v1/runs/{run_id}/accept-offer", status_code=202)
    def accept(run_id: str, body: AcceptBody, p: store.Principal = Depends(principal),
               idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        return idempotent(p, idempotency_key, f"accept:{run_id}", lambda: _woke(store.accept_offer(eng(), clock, p, run_id, body.offer_id)), 202,
                          body=body.model_dump())

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with tx(eng()) as c:
            run = store.get_run(c, p.session_id, run_id, for_update=True)
            if run["state"] != "blocked":
                raise store.ApiError(409, "NOT_BLOCKED", "Only a blocked negotiation can be retried.")
            exe(c, "UPDATE runs SET state = 'running', state_reason = NULL, needs_step = true, last_feedback = NULL, updated_at = :n WHERE run_id = :r",
                n=clock.now(), r=run_id)
            store.append_event(c, clock, p.session_id, "run.state_changed", "owner", "You asked the agent to retry.", run_id=run_id)
        signals.notify_work()
        return {"run_id": run_id, "state": "running"}

    @app.post("/v1/approvals/{approval_id}/resolve")
    def resolve(approval_id: str, body: ResolveBody, p: store.Principal = Depends(principal),
                idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        if body.decision not in ("approve", "reject"):
            raise store.ApiError(422, "BAD_DECISION", "Decision must be approve or reject.")
        return idempotent(p, idempotency_key, f"resolve:{approval_id}",
                          lambda: executor.resolve_approval(eng(), clock, p.session_id, approval_id, body.decision, body.action_hash).as_dict(),
                          body=body.model_dump())

    @app.get("/v1/runs/{run_id}/evidence")
    def evidence(run_id: str, view: str = Query("owner", pattern="^(owner|buyer)$"), p: store.Principal = Depends(principal)) -> JSONResponse:
        with eng().connect() as c:
            data = projections.evidence(c, p.session_id, run_id, view=view, provider_mode=state["adapter"].mode, release=state.get("release") or "")
        return JSONResponse(content=json.loads(json.dumps(data, default=str)),
                            headers={"Content-Disposition": f'attachment; filename="delegate-evidence-{view}-{run_id}.json"'})

    @app.get("/v1/events")
    def session_events(after: int = Query(0, ge=0, le=10**12), run_id: str | None = None, p: store.Principal = Depends(principal)) -> dict[str, Any]:
        with eng().connect() as c:
            if run_id:
                store.get_run(c, p.session_id, run_id)
            return {"events": projections.events(c, p.session_id, after, run_id=run_id, owner=True)}

    @app.get("/v1/runs/{run_id}/events")
    async def sse(run_id: str, request: Request, p: store.Principal = Depends(principal),
                  last_event_id: str | None = Header(default=None), after: int = Query(0, ge=0, le=10**12)) -> StreamingResponse:
        with eng().connect() as c:
            store.get_run(c, p.session_id, run_id)
        with _streams_lock:
            if _streams.get(p.session_id, 0) >= MAX_STREAMS_PER_SESSION:
                raise store.ApiError(429, "TOO_MANY_STREAMS", "Close other tabs for this scenario and try again.", True)
            _streams[p.session_id] = _streams.get(p.session_id, 0) + 1
        parsed = _digits(last_event_id, 12)
        cursor: int = parsed if parsed is not None else after
        token = request.headers.get("authorization", "")[7:]

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in _gen():
                    yield chunk
            finally:
                with _streams_lock:
                    _streams[p.session_id] = max(0, _streams.get(p.session_id, 1) - 1)

        async def _gen() -> AsyncIterator[bytes]:
            nonlocal cursor
            idle = 0.0
            checked = 0.0
            yield b"retry: 2000\n\n"
            while not await request.is_disconnected():
                checked += 0.5
                if checked >= 30:
                    checked = 0.0
                    try:
                        await run_in_threadpool(store.authenticate, eng(), clock, token)
                    except store.ApiError:
                        yield b"event: session.ended\ndata: {}\n\n"
                        return
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

    @app.get("/v1/lab/cases")
    def lab_cases() -> dict[str, Any]:
        return lab.list_cases()

    @app.get("/v1/lab/cases/{case_id}")
    def lab_case(case_id: str) -> dict[str, Any]:
        c = lab.get_case(case_id)
        if c is None:
            raise store.ApiError(404, "NOT_FOUND", "No such case.")
        return c

    @app.post("/v1/lab/evaluate")
    def lab_evaluate(body: LabEvalBody, request: Request) -> dict[str, Any]:
        if int(request.headers.get("content-length") or 0) > 20_000:
            raise store.ApiError(413, "BODY_SIZE", "Keep lab proposals under 20 KB.")
        r = lab.evaluate_edited(body.case_id, body.proposal)
        if r is None:
            raise store.ApiError(404, "NOT_FOUND", "No such case.")
        return r

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


def _woke(result: dict[str, Any]) -> dict[str, Any]:
    signals.notify_work()
    return result


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
        "path": draft.get("path"),
    }
