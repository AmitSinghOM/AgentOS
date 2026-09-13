"""Shared chaos fixtures: a SQLite-file store (survives a "crash" because the crashed
worker's in-memory state is irrelevant — only the file matters), a counting executor
whose call log is the effect counter, and the five steady-state invariants from
ROADMAP.md → Chaos engineering plan, asserted after every fault."""
from __future__ import annotations

from collections import Counter

import pytest

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.events import StepCompleted, StepStarted
from agentos.core.fold import fold
from agentos.core.models import Agent, AgentType, WorkflowDefinition
from agentos.store.sqlite import SqliteStore
from agentos.worker import Worker

THREE_STEP = WorkflowDefinition(name="three", version=1, nodes=[
    {"id": "s1", "agent": "g"},
    {"id": "s2", "agent": "g", "depends_on": ["s1"]},
    {"id": "s3", "agent": "g", "depends_on": ["s2"]},
])


class CountingEcho(EchoExecutor):
    """Effect counter: every call is one 'side effect'. Shared across workers in a test
    (same process) so the count is global, like a real external system's ledger."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def execute(self, agent, upstream):
        # Which step is this? The upstream keys tell us (s1 has none).
        step = {(): "s1", ("s1",): "s2", ("s2",): "s3"}[tuple(sorted(upstream))]
        self.calls[step] += 1
        return super().execute(agent, upstream)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "chaos.db"


@pytest.fixture
def store(db_path):
    s = SqliteStore(db_path)
    s.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    s.put_workflow(THREE_STEP)
    yield s
    s.close()


@pytest.fixture
def executor():
    return CountingEcho()


def make_worker(store, executor, *, holder: str, faults=None, lease_ttl: float = 30.0) -> Worker:
    """A 'process': its own Engine + Worker over the shared store file."""
    engine = Engine(store=store, blobs=store, executors={"echo": executor}, faults=faults)
    return Worker(engine, store, lease=store, queue=store, holder=holder,
                  lease_ttl=lease_ttl, faults=faults)


def assert_invariants(store, run_id: str) -> None:
    events = store.read_events(run_id)
    seqs = [e.seq for e in events]
    # 1. seq dense and monotonic
    assert seqs == list(range(1, len(events) + 1)), seqs
    # 2. exactly one step.completed per step
    completed = Counter(e.step_id for e in events if isinstance(e, StepCompleted))
    assert all(n == 1 for n in completed.values()), completed
    # 3. every completed step was started before it completed
    first_start = {}
    for e in events:
        if isinstance(e, StepStarted):
            first_start.setdefault(e.step_id, e.seq)
        if isinstance(e, StepCompleted):
            assert first_start[e.step_id] < e.seq
    # 4. terminal, or running with a live lease (never orphaned RUNNING)
    run = fold(events)
    if run.status.value == "running":
        assert store.acquire(run_id, "probe", 0.01) is None, "RUNNING run with no lease holder"
    # 5. fold from scratch equals fold of a JSON round trip (snapshot-equivalence stand-in
    #    until snapshots exist)
    from agentos.core.events import from_record
    assert fold(from_record(e.to_record()) for e in events) == run
