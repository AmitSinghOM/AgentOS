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

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.models import Agent, AgentType, WorkflowDefinition
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
engine = Engine(store=store, blobs=store, executors={AgentType.echo.value: EchoExecutor()})

app = FastAPI(title="AgentOS", version="0.2.0-dev")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/agents", status_code=201)
def register_agent(agent: Agent) -> Agent:
    store.put_agent(agent)
    return agent


@app.get("/agents")
def list_agents() -> dict:
    return {"data": store.list_agents()}


@app.post("/workflows", status_code=201)
def define_workflow(wf: WorkflowDefinition) -> WorkflowDefinition:
    try:
        wf.validate_dag()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.put_workflow(wf)
    return wf


@app.post("/workflows/{name}/runs", status_code=202)
def start_run(name: str, response: Response, sync: bool = False,
              idempotency_key: str | None = Header(default=None,
                                                   alias="Idempotency-Key")) -> dict:
    """Enqueue a run and return 202 with its id; a worker (`python -m agentos.worker`)
    advances it. `?sync=true` runs it in-process and returns 201 with the finished run
    (the Phase 0 behaviour, kept for the quick start and tests). Repeating the call with
    the same `Idempotency-Key` header returns the same run (DESIGN §6)."""
    try:
        if sync:
            response.status_code = 201
            return engine.start_run(name, request_id=idempotency_key).model_dump()
        run_id = engine.create_run(name, request_id=idempotency_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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


@app.get("/runs/{run_id}/events")
def get_run_events(run_id: str, after: int = 0) -> dict:
    """The raw log, paged by seq (C15). This is the public API; the folded view above
    is a convenience over it."""
    events = store.read_events(run_id, after_seq=after)
    if not events and after == 0:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return {"data": [e.to_record() for e in events],
            "last_seq": events[-1].seq if events else after}
