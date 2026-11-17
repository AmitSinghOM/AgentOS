"""Layer 2, scenario 3: `pg_latency_under_fanout` (ROADMAP → Phase 2, Chaos network class,
"fan-in waits; no branch output lost (C4)"; chaos plan Layer 2 row `latency 500ms`).

The ROADMAP Phase 2 demo DAG, n1 → [n2, n3, n4] → n5, runs on a worker whose link to Postgres
goes through Toxiproxy. The moment the first branch step starts, 200 ms of latency is added
to every response from Postgres and stays for the rest of the run: every blob write, every
`step.completed` append (five round trips inside one transaction), every lease renew now
takes ~0.2–1.0 s, while three branch steps settle through one serialized `_Log`.

What must be true (C4, C9; chaos plan invariants 1–3, 5):
  - the run completes; every step has exactly ONE `step.completed` and the executor ran each
    step exactly once — latency is slowness, not failure, so no retry and no dead-letter;
  - fan-in waited: n5's inputs are all three branch outputs, none dropped or replaced;
  - the wave was really concurrent under latency (the three branches overlap in wall time)
    and the toxic was really in force when the fan-in ran;
  - total time is bounded (well under the lease TTL and the test budget).
"""
from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from dagentos.agents.echo import EchoExecutor
from dagentos.core.engine import Engine
from dagentos.core.events import StepCompleted, StepDeadLettered, StepFailed, StepStarted
from dagentos.core.models import Agent, AgentType, WorkflowDefinition
from dagentos.worker import Worker

from ..conftest import assert_invariants
from .conftest import LISTEN, UPSTREAM, proxied_store

pytestmark = pytest.mark.network

LATENCY_MS = 200
FAN = WorkflowDefinition(name="fan", version=1, max_parallelism=3, nodes=[
    {"id": "n1", "agent": "g"},
    {"id": "n2", "agent": "g", "depends_on": ["n1"]},
    {"id": "n3", "agent": "g", "depends_on": ["n1"]},
    {"id": "n4", "agent": "g", "depends_on": ["n1"]},
    {"id": "n5", "agent": "g", "depends_on": ["n2", "n3", "n4"]},
])


class LaggyFanout(EchoExecutor):
    """Adds the latency toxic when the first branch step starts; records per-step wall spans,
    each step's inputs, and which toxics were on the proxy when the fan-in ran."""

    def __init__(self, toxiproxy, hold: float) -> None:
        self.toxiproxy, self.hold = toxiproxy, hold
        self.calls: list[str] = []
        self.spans: dict[str, tuple[float, float]] = {}
        self.inputs: dict[str, list[str]] = {}
        self.toxics_at_fan_in: list[str] | None = None
        self._lock = threading.Lock()

    def execute(self, req, progress):
        t0 = time.monotonic()
        with self._lock:
            self.calls.append(req.step_id)
            if req.step_id in ("n2", "n3", "n4") and "lat" not in self.toxiproxy.toxics("pg"):
                self.toxiproxy.latency("pg", LATENCY_MS)          # the wave is now slow
            if req.step_id == "n5":
                self.toxics_at_fan_in = self.toxiproxy.toxics("pg")
        time.sleep(self.hold)
        result = super().execute(req, progress)
        with self._lock:
            self.spans[req.step_id] = (t0, time.monotonic())
            self.inputs[req.step_id] = sorted(req.inputs)
        return result


def test_fan_in_waits_for_every_branch_under_latency_and_nothing_runs_twice(
        toxiproxy, pg_direct, pg_schema):
    pg_direct.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    pg_direct.put_workflow(FAN)
    toxiproxy.proxy("pg", LISTEN, UPSTREAM)
    slow = proxied_store(pg_schema, max_size=4)

    executor = LaggyFanout(toxiproxy, hold=0.3)
    engine = Engine(store=slow, blobs=slow, executors={"echo": executor})
    worker = Worker(engine, slow, lease=slow, queue=slow, holder="W", lease_ttl=60.0)
    run_id = engine.create_run("fan", request_id="fanout")
    slow.push(run_id)

    t0 = time.monotonic()
    assert worker.run_once(timeout=5.0) == run_id
    elapsed = time.monotonic() - t0
    toxiproxy.remove_toxic("pg", "lat")

    run = engine.get_run(run_id)
    assert run.status.value == "completed", run.error
    # Latency slowed the run but never failed a step: one completion, one execution, each.
    events = pg_direct.read_events(run_id)
    assert Counter(e.step_id for e in events if isinstance(e, StepCompleted)) == \
        {"n1": 1, "n2": 1, "n3": 1, "n4": 1, "n5": 1}
    assert Counter(e.step_id for e in events if isinstance(e, StepStarted)) == \
        {"n1": 1, "n2": 1, "n3": 1, "n4": 1, "n5": 1}
    assert not any(isinstance(e, (StepFailed, StepDeadLettered)) for e in events)
    assert Counter(executor.calls) == {"n1": 1, "n2": 1, "n3": 1, "n4": 1, "n5": 1}
    # Fan-in waited for all three branches (C4) — and the toxic was in force when it ran.
    assert executor.inputs["n5"] == ["n2", "n3", "n4"]
    assert executor.toxics_at_fan_in == ["lat"], executor.toxics_at_fan_in
    fan_in = run.steps[-1]
    assert fan_in.node_id == "n5"
    branch_outputs = {s.node_id: s.output for s in run.steps if s.node_id in ("n2", "n3", "n4")}
    assert fan_in.output["received"] == branch_outputs
    # The branches overlapped (one wave under latency, not three sequential ones)...
    starts = [executor.spans[s][0] for s in ("n2", "n3", "n4")]
    ends = [executor.spans[s][1] for s in ("n2", "n3", "n4")]
    assert max(starts) < min(ends), executor.spans
    # ...and the whole run stayed inside its budget: well under the 60 s lease, and no faster
    # than three holds plus six latency-inflated round trips (proves the toxic bit).
    assert 3 * 0.3 + 6 * LATENCY_MS / 1000 < elapsed < 45.0, elapsed
    assert_invariants(pg_direct, run_id)
    slow.close()
