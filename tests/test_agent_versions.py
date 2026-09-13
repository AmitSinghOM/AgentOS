"""Agent versioning: runs pin the version of every agent at start and resolve against the
pin on every attempt, so redeploying an agent never changes a running workflow."""
from __future__ import annotations

from agentos.core.engine import Engine
from agentos.core.events import RunStarted, StepStarted
from agentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Provenance,
    RunStatus,
    StepResult,
    WorkflowDefinition,
)
from agentos.store.memory import MemoryStore


class ConfigEcho:
    """Output reveals which agent version ran: the config message."""

    name, version = "cfg", "test"

    def execute(self, req, progress):
        return StepResult(output={"msg": req.agent.config["msg"], "v": req.agent.version},
                          effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                          provenance=Provenance(executor="cfg", executor_version="test"))


def _setup():
    store = MemoryStore()
    store.put_agent(Agent(name="g", version=1, type=AgentType.echo, config={"msg": "v1"}))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    return store, Engine(store=store, blobs=store, executors={"echo": ConfigEcho()})


def test_run_pins_agent_versions_at_start_and_ignores_later_deploys():
    store, eng = _setup()
    run_id = eng.create_run("w")
    started = store.read_events(run_id)[0]
    assert isinstance(started, RunStarted) and started.agent_versions == {"g": 1}

    # Half-way: a completes on v1, then the agent is redeployed as v2.
    store.put_workflow(store.get_workflow("w"))                    # unchanged
    store.put_agent(Agent(name="g", version=2, type=AgentType.echo, config={"msg": "v2"}))
    assert store.get_agent("g").version == 2                       # latest is v2 now

    run = eng.advance(run_id)
    assert run.status is RunStatus.completed
    assert [s.output["msg"] for s in run.steps] == ["v1", "v1"]    # pinned, not latest
    assert run.agent_versions == {"g": 1}
    assert all(e.agent_version == 1 for e in store.read_events(run_id)
               if isinstance(e, StepStarted))

    # A NEW run pins the new version.
    fresh = eng.start_run("w")
    assert fresh.agent_versions == {"g": 2}
    assert [s.output["msg"] for s in fresh.steps] == ["v2", "v2"]


def test_pinned_version_missing_fails_in_the_log_not_silently_upgrades():
    store, eng = _setup()
    run_id = eng.create_run("w")
    # Simulate a store where v1 vanished (e.g. restored from an older backup) but v3 exists.
    store._agents.pop(("g", 1))
    store.put_agent(Agent(name="g", version=3, type=AgentType.echo, config={"msg": "v3"}))
    run = eng.advance(run_id)
    assert run.status is RunStatus.failed
    assert "unknown agent 'g' v1" in run.error
    assert run.steps == []                                         # never ran on v3


def test_unpinned_legacy_run_resolves_latest():
    """Runs recorded before pinning existed (empty agent_versions) must still advance."""
    store, eng = _setup()
    run_id = eng.create_run("w")
    # Rewrite the log's first event as a legacy record without pins.
    rec = store._events[run_id][0]
    rec["agent_versions"] = {}
    run = eng.advance(run_id)
    assert run.status is RunStatus.completed and run.agent_versions == {}


def test_agent_api_versions(monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    body = {"name": "g", "version": 1, "type": "echo", "config": {"msg": "one"}}
    assert c.post("/agents", json=body).status_code == 201
    assert c.post("/agents", json=body).status_code == 201                        # idempotent
    assert c.post("/agents", json=body | {"config": {"msg": "two"}}).status_code == 409
    assert c.post("/agents", json=body | {"version": 2, "config": {"msg": "two"}}).status_code == 201
    assert [a["version"] for a in c.get("/agents").json()["data"]] == [2]
    got = c.get("/agents/g", params={"version": 1}).json()
    assert got["agent"]["config"] == {"msg": "one"} and got["versions"] == [1, 2]
    assert c.get("/agents/g", params={"version": 7}).status_code == 404
    assert c.get("/agents/nope").status_code == 404
