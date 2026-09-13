"""Layer 2, headline scenario: the lease-expiry race over a real slow network.

Worker A talks to Postgres through Toxiproxy. Mid-run we add 2 s of latency to A's link,
which is longer than A's lease TTL. A's *next* heartbeat therefore fails; before that, A's
in-flight step completes and A tries to commit it over the slow link. Meanwhile worker B
(direct link) sees the expired lease, takes it with a higher fence, and finishes the run.

What must be true afterwards (ROADMAP → Chaos engineering plan, Layer 2, row 1):
  - exactly one advancement per step (one step.completed each);
  - A's late write was REJECTED by the fence (a ConflictError), not merged;
  - B's log is the only log.
"""
from __future__ import annotations

import threading
import time

import pytest

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.events import StepCompleted
from agentos.core.models import Agent, AgentType, WorkflowDefinition
from agentos.core.ports import ConflictError
from agentos.store.postgres import PostgresStore
from agentos.worker import Worker

from .conftest import LISTEN, PG_DSN, UPSTREAM, proxied_dsn

pytestmark = pytest.mark.network

THREE = WorkflowDefinition(name="three", version=1, nodes=[
    {"id": "s1", "agent": "g"},
    {"id": "s2", "agent": "g", "depends_on": ["s1"]},
    {"id": "s3", "agent": "g", "depends_on": ["s2"]},
])


class SlowStep(EchoExecutor):
    """Step s2 takes `hold` seconds — long enough for the latency toxic to be applied
    and for A's lease to lapse while the step is 'running'."""

    def __init__(self, hold: float, on_s2: threading.Event) -> None:
        self.hold, self.on_s2 = hold, on_s2
        self.calls: list[str] = []

    def execute(self, agent, upstream):
        step = {(): "s1", ("s1",): "s2", ("s2",): "s3"}[tuple(sorted(upstream))]
        self.calls.append(step)
        if step == "s2":
            self.on_s2.set()
            time.sleep(self.hold)
        return super().execute(agent, upstream)


class RecordingFaults:
    """No crash — we just want to observe A's attempt to append after being fenced."""

    def __init__(self) -> None:
        self.points: list[str] = []

    def at(self, point, **ctx):
        self.points.append(point)


@pytest.fixture
def schema_name(tmp_path):
    return f"n_{tmp_path.name.lower().replace('-', '_')}"[:60]


@pytest.fixture
def direct(schema_name):
    s = PostgresStore(PG_DSN, schema=schema_name, max_size=4)
    s.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    s.put_workflow(THREE)
    try:
        yield s
    finally:
        with s.connection() as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        s.close()


def test_lease_expiry_race_stale_worker_is_fenced_not_merged(toxiproxy, direct, schema_name,
                                                             caplog):
    caplog.set_level("WARNING", logger="agentos.worker")
    toxiproxy.proxy("pg", LISTEN, UPSTREAM)
    slow = PostgresStore(proxied_dsn(PG_DSN), schema=schema_name, max_size=2)

    # Shared "external ledger": both workers' executors count into the same list.
    on_s2 = threading.Event()
    exec_a = SlowStep(hold=1.5, on_s2=on_s2)
    exec_b = SlowStep(hold=0.0, on_s2=threading.Event())
    faults_a = RecordingFaults()
    engine_a = Engine(store=slow, blobs=slow, executors={"echo": exec_a}, faults=faults_a)
    worker_a = Worker(engine_a, slow, lease=slow, queue=slow, holder="A", lease_ttl=0.8,
                      faults=faults_a)
    engine_b = Engine(store=direct, blobs=direct, executors={"echo": exec_b})
    worker_b = Worker(engine_b, direct, lease=direct, queue=direct, holder="B", lease_ttl=30.0)

    run_id = engine_b.create_run("three", request_id="race")
    direct.push(run_id)

    a_result: dict = {}

    def run_a():
        try:
            worker_a.run_once(timeout=2.0)
            a_result["outcome"] = "returned"
        except ConflictError as exc:            # surfaced only if Worker re-raises; it logs
            a_result["outcome"] = f"conflict: {exc}"

    t = threading.Thread(target=run_a)
    t.start()
    assert on_s2.wait(10), "worker A never reached s2"

    # A is now inside s2 (1.5 s). Make A's link to Postgres slow: 2 s per response >
    # A's 0.8 s TTL, so A's heartbeat/renew and its commit both arrive late.
    toxiproxy.latency("pg", 2000)
    time.sleep(1.0)                              # let A's lease lapse

    # B takes over on the direct link and finishes the run.
    assert direct.acquire(run_id, "probe", 0.01) is not None, "A's lease should have expired"
    direct.release(direct.acquire(run_id, "probe", 0.01))
    worker_b.recover()
    assert worker_b.run_once(timeout=2.0) == run_id
    assert engine_b.get_run(run_id).status.value == "completed"

    toxiproxy.remove_toxic("pg", "lat")
    t.join(timeout=30)
    assert not t.is_alive(), "worker A hung"

    events = direct.read_events(run_id)
    completed = [e.step_id for e in events if isinstance(e, StepCompleted)]
    assert completed == ["s1", "s2", "s3"], completed           # exactly one each
    # A executed s1 and s2 (its s2 output was produced but must not have landed);
    # B executed whatever A had not committed. Either way the LOG has one completion
    # per step and A's stale write did not merge.
    assert exec_a.calls[:2] == ["s1", "s2"]
    assert "s3" not in exec_a.calls, "A must not have advanced past s2 after being fenced"
    # Every event after A's last committed one carries B's fence, i.e. was written by B.
    with direct.connection() as conn:
        (max_fence,) = conn.execute(
            "SELECT max_fence FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        (b_fence,) = conn.execute(
            "SELECT fence FROM leases WHERE run_id = %s", (run_id,)).fetchone()
    assert max_fence == b_fence and b_fence >= 2
    # And the REASON A was rejected must be the fence, not merely a seq mismatch: the
    # fence is the guarantee that holds even when seqs still happen to line up.
    rejections = [r.getMessage() for r in caplog.records if run_id in r.getMessage()]
    assert rejections, "worker A never reported a rejected write"
    assert any("fence" in m and "stale" in m for m in rejections), rejections
    slow.close()
