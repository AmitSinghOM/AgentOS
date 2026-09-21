"""FastAPI surface for AgentOS. Phase 1 slice 1: runs are event-sourced; state is always
the fold of the log. Execution is still synchronous; the next slice moves it to a
worker and POST /runs returns 202 + run id.

This module is the composition root: the one place that knows which concrete store,
blob store and executors are wired into the core. Nothing in `dagentos.core` does.

Configuration (env):
  AGENTOS_STORE           memory | sqlite | postgres  (default: sqlite)
  AGENTOS_SQLITE_PATH     file path                   (default: ./agentos.db)
  AGENTOS_SNAPSHOT_EVERY  events between run snapshots (default: 200; 0 disables, C15)
  AGENTOS_STREAM_*        SSE poll / keep-alive / max seconds (see dagentos.api.stream)
  AGENTOS_AUTH            asserted | bearer            (default: asserted — warns; see dagentos.api.auth)
  AGENTOS_AUTH_TOKENS     token file (SHA-256 hashes → principals), required for bearer
  AGENTOS_POLICY          operator policy ceiling file (see dagentos.core.policy); unset → warns
  AGENTOS_SIGNING_KEYS    HMAC keyring file that seals idle/terminal events (dagentos.core.seal)
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from dagentos.agents.echo import EchoExecutor
from dagentos.agents.tool import ToolExecutor
from dagentos.api.auth import AuthConfig, auth_middleware, openapi_security, principal_for
from dagentos.api.stream import MEDIA_TYPE, StreamConfig, parse_after, stream_run
from dagentos.api.ui import mount_ui
from dagentos.core.engine import ControlNotAllowed, Engine, RetryNotAllowed
from dagentos.core.fold import FoldError, fold
from dagentos.core.integrity import IntegrityError, verify
from dagentos.core.models import Agent, AgentType, BlobRef, Principal, WorkflowDefinition
from dagentos.core.policy import policy_from_env
from dagentos.core.ports import ConflictError
from dagentos.core.seal import keyring_from_env, verify_seals
from dagentos.observability import build_observers, store_resolver
from dagentos.plugins import describe, discover_executors, store_pricing_snapshots
from dagentos.store.factory import store_from_env

store = store_from_env()
observers, prometheus = build_observers(resolve=store_resolver(store))
queue_depth = None
if prometheus is not None and hasattr(store, "queue_depth"):
    from dagentos.observability.prometheus import QueueDepthCollector
    queue_depth = QueueDepthCollector(prometheus.registry, store.queue_depth)
executors = {AgentType.echo.value: EchoExecutor(), AgentType.tool.value: ToolExecutor(),
             **discover_executors()}
pricing_snapshots = store_pricing_snapshots(executors, store)


def snapshot_every_from_env() -> int:
    """AGENTOS_SNAPSHOT_EVERY: events a run's log may grow before its folded state is
    cached (C15). 0 disables snapshots (every read folds the whole log)."""
    raw = os.environ.get("AGENTOS_SNAPSHOT_EVERY", "200")
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"AGENTOS_SNAPSHOT_EVERY must be an integer >= 0, got {raw!r}") from None
    if value < 0:
        raise RuntimeError(f"AGENTOS_SNAPSHOT_EVERY must be an integer >= 0, got {raw!r}")
    return value


engine = Engine(store=store, blobs=store, executors=executors,
                lease=store if hasattr(store, "acquire") else None, observers=observers,
                snapshot_every=snapshot_every_from_env(), policy=policy_from_env(),
                keyring=keyring_from_env())

stream_config = StreamConfig.from_env()
auth_config = AuthConfig.from_env()


def max_body_bytes_from_env() -> int:
    """AGENTOS_MAX_BODY_BYTES: largest request body the API accepts (default 1 MiB). A
    workflow definition or agent config far above this is not a use case; it is a way to
    hold a request worker. Enforced before the body is read."""
    raw = os.environ.get("AGENTOS_MAX_BODY_BYTES", str(1024 * 1024))
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"AGENTOS_MAX_BODY_BYTES must be an integer > 0, got {raw!r}") from None
    if value <= 0:
        raise RuntimeError(f"AGENTOS_MAX_BODY_BYTES must be an integer > 0, got {raw!r}")
    return value


max_body_bytes = max_body_bytes_from_env()

app = FastAPI(title="AgentOS", version="0.14.0")


@app.middleware("http")
async def _limit_body(request: Request, call_next):
    """Production pass A3: refuse an over-limit body on its declared length, before any of
    it is read (413), and refuse a bodyful request that declares no length (411) so chunked
    encoding cannot walk around the limit. GET/HEAD/OPTIONS carry no body and pass through."""
    if request.method in ("POST", "PUT", "PATCH"):
        declared = request.headers.get("content-length")
        if declared is None:
            # No length and no chunked framing means no body at all (HTTP/1.1 §3.3.3) —
            # e.g. `curl -X POST .../cancel`, with or without a stray Content-Type. Only
            # chunked framing can carry a body of undeclared size, so only that is refused.
            if "chunked" in request.headers.get("transfer-encoding", "").lower():
                return JSONResponse(status_code=411, content={
                    "detail": "Content-Length is required; chunked request bodies are not accepted"})
        elif not declared.isdigit() or int(declared) > max_body_bytes:
            return JSONResponse(status_code=413, content={
                "detail": f"request body exceeds AGENTOS_MAX_BODY_BYTES ({max_body_bytes})"})
    return await call_next(request)


@app.middleware("http")
async def _authenticate(request: Request, call_next):
    """Phase 8 #1: the credential decides who the caller is; the body may not. Middleware,
    not a per-route dependency, so a route added later is protected by default."""
    return await auth_middleware(request, call_next, config=auth_config)


def _openapi() -> dict:
    if app.openapi_schema is None:
        from fastapi.openapi.utils import get_openapi
        app.openapi_schema = openapi_security(
            get_openapi(title=app.title, version=app.version, routes=app.routes),
            config=auth_config)
    return app.openapi_schema


app.openapi = _openapi  # type: ignore[method-assign]


@app.exception_handler(RequestValidationError)
async def _log_rejected_payload(request: Request, exc: RequestValidationError) -> Response:
    """C12: a control payload that fails validation — an unexpected field, a missing
    principal — is rejected 422 AND logged with what was smuggled, so an attempt to feed
    the scheduler a client-authored tool call leaves a trace even though no event is
    appended and no step starts."""
    extras = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("type") == "extra_forbidden"})
    if extras:
        logger.warning("rejected control payload on %s %s: unexpected field(s) %s",
                       request.method, request.url.path, extras)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up. Says nothing about the store — that is `/ready`."""
    return {"status": "ok"}


#: `GET /ready` gives the store this long. The image HEALTHCHECK allows 3 s per probe; a
#: pool acquire that would wait psycopg_pool's default 30 s must fail inside the window.
READY_TIMEOUT_SECONDS = 2.0


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness (production pass 2, A4): one real, time-bounded store round-trip, so an
    orchestrator stops routing traffic to an API whose database is unreachable instead of
    letting every request fail. On failure the body names the exception CLASS only; the
    message — which may carry a host name or DSN — goes to the log. No token required:
    probes carry none (auth.OPEN_PATHS)."""
    try:
        store.ping(timeout=READY_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a probe reports, it never raises
        logger.warning("readiness: store unreachable: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=503,
                            content={"status": "unavailable", "store": type(exc).__name__})
    return JSONResponse(content={"status": "ready"})


@app.get("/executors")
def list_executors() -> list[dict]:
    """Which executors this deployment can dispatch to, with each plugin's own
    `describe()` (models, aliases, pricing snapshot) and `health()` (is its model server
    reachable, does it have the model). The first thing to check when a step fails."""
    return describe(executors)


@app.get("/me")
def me(request: Request) -> dict:
    """Who the API will record as the decider for this caller (Phase 9 inbox). In bearer mode
    the token's Principal; in asserted mode `principal` is null and the client must supply
    one in each decision body — and should label it unverified."""
    principal = request.state.principal
    return {"mode": auth_config.mode.value,
            "principal": principal.model_dump() if principal is not None else None}


@app.get("/policy")
def get_policy() -> dict:
    """The operator ceiling every workflow budget is intersected with (Phase 8 #2), and
    its sha256 — the value `governance.policy_applied` records on each run. Nulls when no
    policy is configured (every workflow's own budget is then the only limit)."""
    if engine.policy is None:
        return {"sha256": None, "policy": None,
                "note": "AGENTOS_POLICY unset: no operator ceiling"}
    doc = engine.policy.model_dump(mode="json")
    for key in ("allowed_executors", "effect_ceiling", "always_approve"):
        if doc[key] is not None:
            doc[key] = sorted(doc[key])
    return {"sha256": engine.policy_digest, "policy": doc}


@app.get("/blobs/{sha256}")
def get_blob(sha256: str) -> Response:
    """Fetch a content-addressed payload: step inputs/outputs, run inputs, or the pricing
    table behind a step's `cost.pricing_snapshot_hash` (§11 A6)."""
    ref = BlobRef(sha256=sha256, size=0)
    if not store.exists(ref):
        raise HTTPException(status_code=404, detail=f"no blob {sha256!r}")
    return Response(content=store.get(ref), media_type="application/json")


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus exposition. Every number here is derived from the event log."""
    if prometheus is None:
        raise HTTPException(status_code=404, detail="prometheus-client not installed or disabled")
    if queue_depth is not None:
        queue_depth.refresh()                 # the one metric that is not an event fact
    body, content_type = prometheus.render()
    return Response(content=body, media_type=content_type)


@app.post("/agents", status_code=201)
def register_agent(agent: Agent) -> Agent:
    """Register an immutable agent version. Re-posting the identical definition is a
    no-op; a different body under an existing (name, version) is 409 — bump the version."""
    try:
        store.put_agent(agent)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return agent


@app.get("/agents")
def list_agents() -> dict:
    """Latest version of every agent."""
    return {"data": store.list_agents()}


@app.get("/agents/{name}")
def get_agent(name: str, version: int | None = None) -> dict:
    agent = store.get_agent(name, version=version)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {name!r}"
                            + (f" v{version}" if version else ""))
    return {"agent": agent, "versions": store.list_agent_versions(name)}


@app.get("/workflows/{name}")
def get_workflow(name: str) -> WorkflowDefinition:
    """The CURRENT definition. Runs pin `workflow_version`; a run whose pinned version differs
    from this one cannot advance (C3) — the UI says so rather than drawing the wrong graph."""
    wf = store.get_workflow(name)
    if wf is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {name!r}")
    return wf


@app.post("/workflows", status_code=201)
def define_workflow(wf: WorkflowDefinition) -> WorkflowDefinition:
    try:
        wf.validate_dag()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.put_workflow(wf)
    return wf


# C12 trust boundary (docs/TRUST_BOUNDARY.md §1): control payloads carry a principal and a
# reason and NOTHING else. A body smuggling a tool call, an output or a next step is
# rejected 422 before it reaches the engine, and logged; the scheduler only ever dispatches
# steps it derives from the workflow definition.
_STRICT = ConfigDict(extra="forbid")
logger = logging.getLogger("agentos.api")


class RunStartBody(BaseModel):
    """Optional body for `POST /workflows/{name}/runs`. `inputs` reach every step under
    the reserved key `run`, so an agent's prompt template can say `{run.topic}`."""

    inputs: dict = {}


@app.post("/workflows/{name}/runs", status_code=202)
def start_run(name: str, response: Response, sync: bool = False,
              body: RunStartBody | None = None,
              idempotency_key: str | None = Header(default=None,
                                                   alias="Idempotency-Key")) -> dict:
    """Enqueue a run and return 202 with its id; a worker (`python -m dagentos.worker`)
    advances it. `?sync=true` runs it in-process and returns 201 with the finished run
    (the Phase 0 behaviour, kept for the quick start and tests). Repeating the call with
    the same `Idempotency-Key` header returns the same run (DESIGN §6)."""
    inputs = body.inputs if body is not None else None
    try:
        if sync:
            response.status_code = 201
            return engine.start_run(name, request_id=idempotency_key,
                                    inputs=inputs).model_dump()
        run_id = engine.create_run(name, request_id=idempotency_key, inputs=inputs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.push(run_id)
    run = engine.get_run(run_id)
    assert run is not None
    return run.model_dump()


@app.get("/runs")
def list_runs(limit: int = 50) -> dict:
    """Newest-first summary of runs for the operator UI's landing page. `limit` is clamped
    to 1..500. Costs `limit` folds, not one per run in the store: `list_run_ids` is ordered
    by creation, so we walk it newest-first and stop once the page is full. (`/approvals`
    and the recovery sweep still scan — they filter on folded status; see ROADMAP.)"""
    limit = max(1, min(limit, 500))
    out = []
    for run_id in reversed(store.list_run_ids()):
        if len(out) == limit:
            break
        run = engine.get_run(run_id, hydrate=False)
        if run is None:
            continue
        out.append({"id": run.id, "workflow": run.workflow,
                    "workflow_version": run.workflow_version, "status": run.status.value,
                    "total_cost": run.total_cost, "last_seq": run.last_seq,
                    "started_at": run.started_at.isoformat(),
                    "pending_approvals": sum(1 for a in run.approvals.values()
                                             if a.status.value == "pending")})
    return {"data": out}


@app.get("/runs/{run_id}")
def get_run(run_id: str, at: int | None = None) -> dict:
    """The folded run. `?at=k` (Phase 9 time-travel) folds only the log prefix through seq k —
    the same fold, over fewer events; `fold_from == fold` at every cut point is pinned by the
    golden corpus. `at` must be within 1..last_seq (1 is `run.started` alone). Historical
    folds verify the chain prefix too, so a tampered prefix is refused at k, not only at the
    tail."""
    if at is not None:
        return _run_at(run_id, at)
    try:
        run = engine.get_run(run_id)
    except FoldError as exc:
        # C12: a tampered log is refused, loudly, rather than folded into state.
        logger.error("run %s: %s", run_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run.model_dump()


def _run_at(run_id: str, at: int) -> dict:
    events = store.read_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    last = events[-1].seq
    if at < 1 or at > last:
        raise HTTPException(status_code=422, detail=f"at must be within 1..{last} for run {run_id!r}")
    try:
        return fold([e for e in events if e.seq <= at]).model_dump()
    except FoldError as exc:
        logger.error("run %s at %s: %s", run_id, at, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/runs/{run_id}/integrity")
def run_integrity(run_id: str) -> dict:
    """Verify the run's tamper-evident event chain (C12) and its seals (Phase 8 #3).
    `hashed` counts the events that carry a hash (pre-v0.6.0 logs have none and verify
    trivially). `seals.state` is unsigned | verified | unverifiable | INVALID; `ok` is
    false when either the chain or a seal fails."""
    events = store.read_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    seals = verify_seals(events, engine.keyring).as_dict()
    try:
        hashed = verify(events)
    except IntegrityError as exc:
        return {"run_id": run_id, "ok": False, "events": len(events), "error": str(exc),
                "seals": seals}
    return {"run_id": run_id, "ok": seals["state"] != "INVALID", "events": len(events),
            "hashed": hashed, "seals": seals}


class RetryBody(BaseModel):
    model_config = _STRICT
    principal: Principal | None = None
    reason: str = ""


@app.post("/runs/{run_id}/steps/{step_id}/retry", status_code=202)
def retry_step(run_id: str, step_id: str, request: Request, body: RetryBody | None = None,
               sync: bool = False, response: Response = None) -> dict:  # type: ignore[assignment]
    """Reopen a dead-lettered or failed step (C11). Records who asked (A2) as a
    `step.retry_requested` event, then re-enqueues the run (or, with `?sync=true`,
    advances it in-process). 409 if the step is not in a retryable state."""
    body = body or RetryBody()
    principal = principal_for(request, body.principal, config=auth_config, required=False)
    try:
        engine.request_retry(run_id, step_id, principal=principal, reason=body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RetryNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if sync:
        response.status_code = 200
        return engine.advance_until_terminal(run_id).model_dump()
    store.push(run_id)
    run = engine.get_run(run_id)
    assert run is not None
    return run.model_dump()


@app.get("/runs/{run_id}/events")
def get_run_events(run_id: str, after: int = 0, limit: int = 1000) -> dict:
    """The raw log, paged by seq (C15). This is the public API; the folded view above
    is a convenience over it. `limit` is clamped to 1..5000; `has_more` says whether a
    page after `last_seq` exists, so a client walks `after=last_seq` until it is false."""
    limit = max(1, min(limit, 5000))
    events = store.read_events(run_id, after_seq=after, limit=limit + 1)
    if not events and after == 0:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    has_more = len(events) > limit
    events = events[:limit]
    return {"data": [e.to_record() for e in events],
            "last_seq": events[-1].seq if events else after,
            "has_more": has_more}


@app.get("/runs/{run_id}/stream")
def stream_run_events(run_id: str, after: int = 0,
                      last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    """Server-Sent Events over the log (Phase 7): one frame per record, `id` = seq,
    `event` = event_type, `data` = the same JSON as GET /runs/{id}/events. Resumable via
    `Last-Event-ID` (or `?after=`), closes on the terminal event or after
    AGENTOS_STREAM_MAX_SECONDS. Works from any process — it reads the store, not a bus."""
    try:
        start = parse_after(last_event_id, after)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not store.read_events(run_id, after_seq=0):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return StreamingResponse(
        stream_run(store, run_id, after=start, config=stream_config),
        media_type=MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ControlBody(BaseModel):
    model_config = _STRICT
    principal: Principal | None = None
    reason: str = ""


def _control(action, run_id: str, request: Request, body: ControlBody | None) -> dict:
    body = body or ControlBody()
    principal = principal_for(request, body.principal, config=auth_config, required=False)
    try:
        return action(run_id, principal=principal, reason=body.reason).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControlNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, request: Request, body: ControlBody | None = None) -> dict:
    """Persist a cancel request (C5). An idle or paused run is cancelled immediately; a
    running one at the worker's next boundary — the in-flight step is interrupted at its
    next progress() call or recorded if it finishes first (C4). Idempotent. 409 if the
    run is already terminal. A client disconnect never changes run state; only this
    endpoint does."""
    return _control(engine.request_cancel, run_id, request, body)


@app.post("/runs/{run_id}/pause", status_code=202)
def pause_run(run_id: str, request: Request, body: ControlBody | None = None) -> dict:
    """Persist a pause request: the current wave finishes and is recorded, then the run
    is `paused` and leaves the queue. 409 if terminal or already paused."""
    return _control(engine.request_pause, run_id, request, body)


@app.post("/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str, request: Request, body: ControlBody | None = None) -> dict:
    """Append run.resumed and re-enqueue. 409 unless the run is paused."""
    out = _control(engine.resume, run_id, request, body)
    store.push(run_id)
    return out


# ---- human-in-the-loop (C7)

class DecisionBody(BaseModel):
    model_config = _STRICT
    # Every decision names who made it (A2). In asserted mode the body must carry it
    # (422 otherwise); in bearer mode the token supplies it and a body value is a 422.
    principal: Principal | None = None
    reason: str = ""


@app.get("/runs/{run_id}/approvals")
def list_run_approvals(run_id: str) -> dict:
    run = engine.get_run(run_id, hydrate=False)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return {"data": list(run.approvals.values()), "status": run.status}


@app.get("/approvals")
def list_pending_approvals() -> dict:
    """Every pending approval across suspended runs — the operator's inbox.
    (Scans runs; a store index arrives with the Phase 3 operator surface.)"""
    out = []
    for run_id in store.list_run_ids():
        run = engine.get_run(run_id, hydrate=False)
        if run is None or run.status.value != "suspended":
            continue
        out += [{"run_id": run_id, "workflow": run.workflow, **a.model_dump()}
                for a in run.approvals.values() if a.status.value == "pending"]
    return {"data": out}


@app.post("/runs/{run_id}/approvals/{approval_id}/approve", status_code=202)
def approve(run_id: str, approval_id: str, body: DecisionBody, request: Request,
            sync: bool = False, response: Response = None) -> dict:  # type: ignore[assignment]
    """Grant. 403 if the principal kind may not approve these effect classes; 409 if the
    approval is not pending. Re-enqueues the run (or `?sync=true` advances it)."""
    principal = principal_for(request, body.principal, config=auth_config, required=True)
    assert principal is not None
    try:
        run = engine.approve(run_id, approval_id, principal=principal, reason=body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControlNotAllowed as exc:
        code = 403 if "principal" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if run.status.value == "running":
        if sync:
            response.status_code = 200
            return engine.advance_until_terminal(run_id).model_dump()
        store.push(run_id)
    return run.model_dump()


@app.post("/runs/{run_id}/approvals/{approval_id}/reject", status_code=200)
def reject(run_id: str, approval_id: str, body: DecisionBody, request: Request) -> dict:
    """Reject: the step is dead-lettered naming the decider; the run fails. Reopen it with
    POST /runs/{id}/steps/{step}/retry, which re-requests approval."""
    principal = principal_for(request, body.principal, config=auth_config, required=True)
    assert principal is not None
    try:
        return engine.reject(run_id, approval_id, principal=principal,
                             reason=body.reason).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControlNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# Operator UI (Phase 9): static bundle at /ui when built; see dagentos/api/ui.py.
mount_ui(app)
