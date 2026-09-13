"""The ports are real seams: the engine must run against a store and an executor that
the core has never heard of. If this test needs any adapter import, the layering is
broken (docs/DEVELOPMENT_STRUCTURE.md §1)."""
from __future__ import annotations

import pytest

from agentos.core.engine import Engine
from agentos.core.models import Agent, AgentType, RunStatus, WorkflowDefinition, WorkflowRun


class FakeStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.workflows: dict[str, WorkflowDefinition] = {}
        self.runs: dict[str, WorkflowRun] = {}
        self.put_run_calls = 0

    def put_agent(self, agent: Agent) -> None:
        self.agents[agent.name] = agent

    def get_agent(self, name: str) -> Agent | None:
        return self.agents.get(name)

    def list_agents(self) -> list[Agent]:
        return list(self.agents.values())

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        self.workflows[wf.name] = wf

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        return self.workflows.get(name)

    def put_run(self, run: WorkflowRun) -> None:
        self.put_run_calls += 1
        self.runs[run.id] = run

    def get_run(self, run_id: str) -> WorkflowRun | None:
        return self.runs.get(run_id)


class CountingExecutor:
    """Stands in for any future model: the core only sees an opaque dict come back."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, agent: Agent, upstream: dict[str, dict]) -> dict:
        self.calls.append((agent.name, upstream))
        return {"n": len(self.calls), "from": sorted(upstream)}


def _diamond() -> WorkflowDefinition:
    return WorkflowDefinition(name="diamond", nodes=[
        {"id": "a", "agent": "x", "depends_on": []},
        {"id": "b", "agent": "x", "depends_on": ["a"]},
        {"id": "c", "agent": "x", "depends_on": ["a"]},
        {"id": "d", "agent": "x", "depends_on": ["b", "c"]},
    ])


def test_engine_runs_against_unknown_adapters():
    store, executor = FakeStore(), CountingExecutor()
    store.put_agent(Agent(name="x", type=AgentType.echo))
    store.put_workflow(_diamond())
    engine = Engine(store=store, executors={"echo": executor})

    run = engine.run_workflow("diamond")

    assert run.status == RunStatus.completed
    assert [s.node_id for s in run.steps] == ["a", "b", "c", "d"]
    # Upstream outputs flow along edges; d saw both b and c.
    assert run.steps[-1].output["from"] == ["b", "c"]
    # Started + finished are both persisted through the port.
    assert store.put_run_calls == 2
    assert store.get_run(run.id) is run


def test_missing_executor_fails_the_run_not_the_process():
    store = FakeStore()
    store.put_agent(Agent(name="x", type=AgentType.llm))
    store.put_workflow(_diamond())
    engine = Engine(store=store, executors={"echo": CountingExecutor()})

    run = engine.run_workflow("diamond")

    assert run.status == RunStatus.failed
    assert "no executor registered" in (run.error or "")
    assert run.ended_at is not None


def test_topological_order_is_deterministic_and_single_sourced():
    wf = _diamond()
    assert wf.topological_order() == ["a", "b", "c", "d"]
    # validate_dag is an alias, not a second implementation.
    wf.validate_dag()
    cyclic = WorkflowDefinition(name="loop", nodes=[
        {"id": "x", "agent": "e", "depends_on": ["y"]},
        {"id": "y", "agent": "e", "depends_on": ["x"]},
    ])
    with pytest.raises(ValueError, match="cycle"):
        cyclic.topological_order()
    with pytest.raises(ValueError, match="cycle"):
        cyclic.validate_dag()


def test_core_package_imports_no_adapter():
    """Belt to import-linter's braces: a direct assertion that loading the core
    does not drag an adapter or framework module into sys.modules."""
    import importlib
    import sys

    for mod in [m for m in sys.modules if m.startswith("agentos")]:
        del sys.modules[mod]
    importlib.import_module("agentos.core.engine")
    importlib.import_module("agentos.core.ports")
    loaded = {m for m in sys.modules if m.startswith("agentos.")}
    assert not {m for m in loaded if m.startswith(("agentos.api", "agentos.store",
                                                   "agentos.agents"))}, loaded
    assert "fastapi" not in {m.split(".")[0] for m in sys.modules
                             if m in loaded}  # core never pulls the transport
