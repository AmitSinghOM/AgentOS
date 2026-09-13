"""The ports are real seams: the engine must run against a store and an executor that
the core has never heard of. If this test needs any adapter import, the layering is
broken (docs/DEVELOPMENT_STRUCTURE.md §1)."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest

from agentos.core.engine import Engine
from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import (
    Agent,
    AgentType,
    BlobRef,
    Effect,
    EffectClass,
    Provenance,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
)
from agentos.core.ports import ConflictError


class FakeStore:
    """Minimal in-test implementation of Store + BlobStore, written without looking at
    agentos.store so the port is exercised from a stranger's perspective."""

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.workflows: dict[str, WorkflowDefinition] = {}
        self.logs: dict[str, list[dict]] = {}
        self.requests: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.appends = 0

    def put_agent(self, agent): self.agents[(agent.name, agent.version)] = agent

    def get_agent(self, name, version=None):
        if version is not None:
            return self.agents.get((name, version))
        vs = [v for (n, v) in self.agents if n == name]
        return self.agents[(name, max(vs))] if vs else None

    def list_agents(self): return list(self.agents.values())
    def list_agent_versions(self, name): return sorted(v for (n, v) in self.agents if n == name)
    def put_workflow(self, wf): self.workflows[wf.name] = wf
    def get_workflow(self, name): return self.workflows.get(name)

    def append_events(self, run_id: str, expected_seq: int, events: Sequence[Event],
                      *, fence: int | None = None):
        log = self.logs.setdefault(run_id, [])
        if len(log) != expected_seq:
            raise ConflictError("stale")
        out = []
        for i, ev in enumerate(events, start=expected_seq + 1):
            ev = ev.model_copy(update={"seq": i})
            if isinstance(ev, RunStarted):
                self.requests[ev.request_id] = run_id
            log.append(ev.to_record())
            out.append(ev)
        self.appends += 1
        return out

    def read_events(self, run_id, after_seq=0):
        return [from_record(r) for r in self.logs.get(run_id, []) if r["seq"] > after_seq]

    def list_run_ids(self): return list(self.logs)
    def run_id_for_request(self, request_id): return self.requests.get(request_id)

    def put(self, data: bytes, media_type="application/json"):
        d = hashlib.sha256(data).hexdigest()
        self.blobs[d] = data
        return BlobRef(sha256=d, size=len(data), media_type=media_type)

    def get(self, ref): return self.blobs[ref.sha256]
    def exists(self, ref): return ref.sha256 in self.blobs


class CountingExecutor:
    """Stands in for any future model: the core only sees an opaque output come back."""

    name = "counting"
    version = "test"

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()

    def execute(self, req: StepRequest, progress) -> StepResult:
        self.calls.append((req.agent.name, req.inputs))
        if req.agent.name in self.fail_on:
            raise RuntimeError(f"{req.agent.name} exploded")
        return StepResult(
            output={"n": len(self.calls), "from": sorted(req.inputs)},
            effects=[Effect(effect_class=EffectClass.compute)],
            provenance=Provenance(executor=self.name, executor_version=self.version),
        )


def _diamond(agent="x") -> WorkflowDefinition:
    return WorkflowDefinition(name="diamond", nodes=[
        {"id": "a", "agent": agent, "depends_on": []},
        {"id": "b", "agent": agent, "depends_on": ["a"]},
        {"id": "c", "agent": agent, "depends_on": ["a"]},
        {"id": "d", "agent": agent, "depends_on": ["b", "c"]},
    ])


def _engine(executor=None, agent_type=AgentType.echo):
    store = FakeStore()
    store.put_agent(Agent(name="x", type=agent_type))
    store.put_workflow(_diamond())
    ex = executor or CountingExecutor()
    return store, ex, Engine(store=store, blobs=store, executors={"echo": ex})


def test_engine_runs_against_unknown_adapters():
    store, _executor, engine = _engine()
    run = engine.start_run("diamond")

    assert run.status is RunStatus.completed
    assert [s.node_id for s in run.steps] == ["a", "b", "c", "d"]
    assert run.steps[-1].output["from"] == ["b", "c"]      # outputs flow along edges
    assert all(s.output_ref is not None for s in run.steps)  # log holds refs, not bytes
    types = [r["event_type"] for r in store.logs[run.id]]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert types.count("step.started") == 4 and types.count("step.completed") == 4
    # b and c are independent → they share a wave; every start precedes its completion.
    for sid in "abcd":
        recs = store.logs[run.id]
        st = next(i for i, r in enumerate(recs) if r["event_type"] == "step.started"
                  and r["step_id"] == sid)
        co = next(i for i, r in enumerate(recs) if r["event_type"] == "step.completed"
                  and r["step_id"] == sid)
        assert st < co
    assert engine.get_run(run.id) == run                     # fold is the only truth


def test_completed_steps_are_replayed_not_re_executed():
    """C1 foundation: call advance() again on a finished run and on a half-finished
    one; the executor is never invoked for a step that already has step.completed."""
    store, executor, engine = _engine()
    run = engine.start_run("diamond")
    calls_after_first = len(executor.calls)

    engine.advance(run.id)                                   # already terminal
    assert len(executor.calls) == calls_after_first

    # Rewind the log to just after `a` completed and advance: only b, c, d run.
    recs = store.logs[run.id]
    cut = next(i for i, r in enumerate(recs)
               if r["event_type"] == "step.completed" and r["step_id"] == "a") + 1
    store.logs[run.id] = recs[:cut]
    resumed = engine.advance(run.id)
    assert resumed.status is RunStatus.completed
    assert len(executor.calls) == calls_after_first + 3            # b, c, d — never a
    assert [s.node_id for s in resumed.steps] == ["a", "b", "c", "d"]


def test_run_start_is_idempotent_on_request_id():
    store, executor, engine = _engine()
    first = engine.start_run("diamond", request_id="client-req-7")
    second = engine.start_run("diamond", request_id="client-req-7")
    assert first.id == second.id and len(store.logs) == 1
    assert len(executor.calls) == 4                          # not 8


def test_changed_workflow_version_refuses_to_resume():
    """C3: a run pinned to v1 must not be advanced against v2 by position."""
    store, executor, engine = _engine()
    run = engine.start_run("diamond")
    store.logs[run.id] = store.logs[run.id][:3]              # a done, nothing else
    store.put_workflow(_diamond().model_copy(update={"version": 2}))
    resumed = engine.advance(run.id)
    assert resumed.status is RunStatus.failed
    assert "v2" in resumed.error and "pinned v1" in resumed.error
    assert len(executor.calls) == 4                          # nothing re-ran


def test_step_failure_is_recorded_not_raised():
    store, _executor, engine = _engine(CountingExecutor(fail_on={"x"}))
    run = engine.start_run("diamond")
    assert run.status is RunStatus.failed
    assert "exploded" in (run.error or "")
    types = [r["event_type"] for r in store.logs[run.id]]
    # No retries configured → the crash is terminal → the poison step is dead-lettered
    # with the cause, and the run fails. One code path for every poison step (C11).
    assert types == ["run.started", "step.started", "step.failed", "step.dead_lettered",
                     "run.failed"]
    assert "exploded" in run.dead_lettered["a"]


def test_missing_executor_fails_the_run_not_the_process():
    _store, _executor, engine = _engine(agent_type=AgentType.llm)
    run = engine.start_run("diamond")
    assert run.status is RunStatus.failed
    assert "no executor registered" in (run.error or "")
    assert run.ended_at is not None


def test_topological_order_is_deterministic_and_single_sourced():
    wf = _diamond()
    assert wf.topological_order() == ["a", "b", "c", "d"]
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
    """Belt to import-linter's braces: loading the core must not drag an adapter or
    framework module into sys.modules. Runs in a scratch module table and restores the
    original afterwards so other tests keep a single identity for every class."""
    import importlib
    import sys

    saved = {m: mod for m, mod in sys.modules.items() if m.startswith("agentos")}
    for m in saved:
        del sys.modules[m]
    try:
        for name in ("agentos.core.engine", "agentos.core.ports", "agentos.core.fold",
                     "agentos.core.events", "agentos.core.upcast"):
            importlib.import_module(name)
        loaded = {m for m in sys.modules if m.startswith("agentos.")}
        assert not {m for m in loaded if m.startswith(("agentos.api", "agentos.store",
                                                       "agentos.agents"))}, loaded
    finally:
        for m in [m for m in sys.modules if m.startswith("agentos")]:
            del sys.modules[m]
        sys.modules.update(saved)
