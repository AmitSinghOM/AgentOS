"""FastAPI surface for AgentOS. Phase 1 slice 1: runs are event-sourced; state is always
the fold of the log. Execution is still synchronous; the next slice moves it to a
worker and POST /runs returns 202 + run id.

This module is the composition root: the one place that knows which concrete store,
blob store and executors are wired into the core. Nothing in `agentos.core` does.

Configuration (env):
  AGENTOS_STORE        memory | sqlite      (default: sqlite)
  AGENTOS_SQLITE_PATH  file path            (default: ./agentos.db)
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import ControlNotAllowed, Engine, RetryNotAllowed
from agentos.core.models import Agent, AgentType, BlobRef, Principal, WorkflowDefinition
from agentos.core.ports import ConflictError
from agentos.observability import build_observers
from agentos.plugins import describe, discover_executors, store_pricing_snapshots
from agentos.store.memory import MemoryStore
from agentos.store.sqlite import SqliteStore


def build_store():
    kind = os.environ.get("AGENTOS_STORE", "sqlite").lower()
    if kind == "memory":
        return MemoryStore()
    if kind == "sqlite":
        return SqliteStore(os.environ.get("AGENTOS_SQLITE_PATH", "agentos.db"))
    if kind == "postgres":
        from agentos.store.postgres import PostgresStore
        return PostgresStore(os.environ["AGENTOS_PG_DSN"],
                             schema=os.environ.get("AGENTOS_PG_SCHEMA"))
    raise RuntimeError(f"unknown AGENTOS_STORE {kind!r} (memory | sqlite | postgres)")


store = build_store()
observers, prometheus = build_observers()
queue_depth = None
if prometheus is not None and hasattr(store, "queue_depth"):
    from agentos.observability.prometheus import QueueDepthCollector
    queue_depth = QueueDepthCollector(prometheus.registry, store.queue_depth)
executors = {AgentType.echo.value: EchoExecutor(), **discover_executors()}
pricing_snapshots = store_pricing_snapshots(executors, store)
engine = Engine(store=store, blobs=store, executors=executors,
                lease=store if hasattr(store, "acquire") else None, observers=observers)

app = FastAPI(title="AgentOS", version="0.5.0-dev")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/executors")
def list_executors() -> list[dict]:
    """Which executors this deployment can dispatch to, with each plugin's own
    `describe()` (models, aliases, pricing snapshot) and `health()` (is its model server
    reachable, does it have the model). The first thing to check when a step fails."""
    return describe(executors)


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


@app.post("/workflows", status_code=201)
def define_workflow(wf: WorkflowDefinition) -> WorkflowDefinition:
    try:
        wf.validate_dag()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.put_workflow(wf)
    return wf


class RunStartBody(BaseModel):
    """Optional body for `POST /workflows/{name}/runs`. `inputs` reach every step under
    the reserved key `run`, so an agent's prompt template can say `{run.topic}`."""

    inputs: dict = {}


@app.post("/workflows/{name}/runs", status_code=202)
def start_run(name: str, response: Response, sync: bool = False,
              body: RunStartBody | None = None,
              idempotency_key: str | None = Header(default=None,
                                                   alias="Idempotency-Key")) -> dict:
    """Enqueue a run and return 202 with its id; a worker (`python -m agentos.worker`)
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


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = engine.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run.model_dump()


class RetryBody(BaseModel):
    principal: Principal | None = None
    reason: str = ""


@app.post("/runs/{run_id}/steps/{step_id}/retry", status_code=202)
def retry_step(run_id: str, step_id: str, body: RetryBody | None = None,
               sync: bool = False, response: Response = None) -> dict:  # type: ignore[assignment]
    """Reopen a dead-lettered or failed step (C11). Records who asked (A2) as a
    `step.retry_requested` event, then re-enqueues the run (or, with `?sync=true`,
    advances it in-process). 409 if the step is not in a retryable state."""
    body = body or RetryBody()
    try:
        engine.request_retry(run_id, step_id, principal=body.principal, reason=body.reason)
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
def get_run_events(run_id: str, after: int = 0) -> dict:
    """The raw log, paged by seq (C15). This is the public API; the folded view above
    is a convenience over it."""
    events = store.read_events(run_id, after_seq=after)
    if not events and after == 0:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return {"data": [e.to_record() for e in events],
            "last_seq": events[-1].seq if events else after}


class ControlBody(BaseModel):
    principal: Principal | None = None
    reason: str = ""


def _control(action, run_id: str, body: ControlBody | None) -> dict:
    body = body or ControlBody()
    try:
        return action(run_id, principal=body.principal, reason=body.reason).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControlNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, body: ControlBody | None = None) -> dict:
    """Persist a cancel request (C5). An idle or paused run is cancelled immediately; a
    running one at the worker's next boundary — the in-flight step is interrupted at its
    next progress() call or recorded if it finishes first (C4). Idempotent. 409 if the
    run is already terminal. A client disconnect never changes run state; only this
    endpoint does."""
    return _control(engine.request_cancel, run_id, body)


@app.post("/runs/{run_id}/pause", status_code=202)
def pause_run(run_id: str, body: ControlBody | None = None) -> dict:
    """Persist a pause request: the current wave finishes and is recorded, then the run
    is `paused` and leaves the queue. 409 if terminal or already paused."""
    return _control(engine.request_pause, run_id, body)


@app.post("/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str, body: ControlBody | None = None) -> dict:
    """Append run.resumed and re-enqueue. 409 unless the run is paused."""
    out = _control(engine.resume, run_id, body)
    store.push(run_id)
    return out


# ---- human-in-the-loop (C7)

class DecisionBody(BaseModel):
    principal: Principal              # mandatory: every decision names who made it (A2)
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
def approve(run_id: str, approval_id: str, body: DecisionBody, sync: bool = False,
            response: Response = None) -> dict:  # type: ignore[assignment]
    """Grant. 403 if the principal kind may not approve these effect classes; 409 if the
    approval is not pending. Re-enqueues the run (or `?sync=true` advances it)."""
    try:
        run = engine.approve(run_id, approval_id, principal=body.principal, reason=body.reason)
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
def reject(run_id: str, approval_id: str, body: DecisionBody) -> dict:
    """Reject: the step is dead-lettered naming the decider; the run fails. Reopen it with
    POST /runs/{id}/steps/{step}/retry, which re-requests approval."""
    try:
        return engine.reject(run_id, approval_id, principal=body.principal,
                             reason=body.reason).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ControlNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
