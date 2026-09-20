"""Worker loop resilience (production review 2026-09-20, A1).

`run_forever` is the process. A transient store error — a PostgreSQL connection reset, an
SQLite `database is locked` past the busy timeout — must not take the worker down with every
run it will ever serve; it must be logged with the run id, backed off, and the loop must go on.
`run_once` keeps raising so tests and `--once` see the sharp edge.
"""
from __future__ import annotations

import logging

import pytest

from dagentos.agents.echo import EchoExecutor
from dagentos.core.engine import Engine
from dagentos.core.models import Agent, AgentType, RunStatus, WorkflowDefinition
from dagentos.store.memory import MemoryStore
from dagentos.worker import Worker


class FlakyStore(MemoryStore):
    """Raises a transient error from `acquire` the first `fail` times, then behaves."""

    def __init__(self, fail: int) -> None:
        super().__init__()
        self.fail = fail
        self.raised = 0

    def acquire(self, run_id, holder, ttl_seconds):
        if self.raised < self.fail:
            self.raised += 1
            raise ConnectionResetError("simulated: connection reset by peer")
        return super().acquire(run_id, holder, ttl_seconds)


def _setup(fail: int):
    store = FlakyStore(fail)
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="one", version=1, nodes=[{"id": "n1", "agent": "a"}]))
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()}, lease=store)
    return store, eng


def test_run_once_still_raises_a_transient_store_error():
    store, eng = _setup(fail=1)
    run_id = eng.create_run("one")
    store.push(run_id)
    worker = Worker(eng, store, lease=store, queue=store, holder="w")
    with pytest.raises(ConnectionResetError):
        worker.run_once(timeout=0.2)


def test_run_forever_survives_a_transient_store_error_and_finishes_the_run(caplog):
    store, eng = _setup(fail=1)
    store.visibility_seconds = 0.05                # redeliver the un-acked run quickly
    run_id = eng.create_run("one")
    store.push(run_id)
    ticks = iter(range(100))

    def clock() -> float:                          # deterministic: never reaches the sweep
        return float(next(ticks))
    worker = Worker(eng, store, lease=store, queue=store, holder="w", clock=clock)
    worker.error_backoff_seconds = 0.0            # do not sleep in the test

    iterations = {"n": 0}

    def stop() -> bool:
        iterations["n"] += 1
        run = eng.get_run(run_id, hydrate=False)
        return iterations["n"] > 20 or (run is not None and run.status is RunStatus.completed)

    with caplog.at_level(logging.ERROR, logger="agentos.worker"):
        worker.run_forever(stop=stop, sweep_interval=1000.0)

    assert store.raised == 1
    assert eng.get_run(run_id, hydrate=False).status is RunStatus.completed
    hits = [r for r in caplog.records if run_id in r.getMessage()]
    assert hits, "the failure must be logged with the run id"
    assert any(r.exc_info and "connection reset" in str(r.exc_info[1]) for r in hits), \
        "the traceback must ride along"
