"""FastAPI surface for AgentOS. Phase 0: synchronous runs. Phase 1 moves run
execution onto a worker via a Redis queue; POST /runs returns 202 + run id.

This module is the composition root: the one place that knows which concrete
store and executors are wired into the core. Nothing in `agentos.core` does."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.models import Agent, AgentType, WorkflowDefinition
from agentos.store.memory import MemoryStore

store = MemoryStore()
engine = Engine(store=store, executors={AgentType.echo.value: EchoExecutor()})

app = FastAPI(title="AgentOS", version="0.1.0")


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


@app.post("/workflows/{name}/runs", status_code=201)
def start_run(name: str) -> dict:
    try:
        run = engine.run_workflow(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return run.model_dump()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run.model_dump()
