"""Layer 2, scenario 2: `coordination_partition_mid_run` (ROADMAP → Phase 2, Chaos network
class; audit E-12).

Worker A talks to Postgres through Toxiproxy. While A is inside step s2 the proxy is
DISABLED — every connection dropped, new ones refused — at one of two engine boundaries,
placed deterministically with `CutLinkAt` (the network counterpart of `CrashAt`):

  before_effect_commit  s2's output exists, `step.completed` is NOT yet appended. A's commit
                        hits a dead socket. C1: the ledger never saw s2, so s2 RE-RUNS on
                        recovery as attempt 2 (exactly the Layer 1 `before_effect_commit`
                        row) — and still has exactly one completion.
  after_effect_commit   `step.completed` for s2 IS committed; A's next read fails. C1: s2 is
                        replayed from the log, never executed again.

What must be true afterwards (docs/REPLAY.md, DESIGN C1/C6; the five steady-state
invariants of the chaos plan):
  - A's run_once ends in a transport error — never a ConflictError (it was not fenced; the
    wire went away) and never a half-written log;
  - once A's lease lapses, worker B on the direct link takes over via the recovery sweep
    and completes the run: exactly one `step.completed` per step; s1 and s3 executed once;
    s2 executed once (after) or twice (before) and never a third time;
  - A appends nothing after the link returns: its lease was superseded (`max_fence` is B's);
  - when the link returns, the log read THROUGH the proxy is byte-identical to the direct
    read — there is one log.
"""
from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from dagentos.agents.echo import EchoExecutor
from dagentos.core import faults
from dagentos.core.engine import Engine
from dagentos.core.events import StepCompleted, StepStarted
from dagentos.core.models import Agent, AgentType, WorkflowDefinition
from dagentos.core.ports import ConflictError
from dagentos.worker import Worker

from ..conftest import assert_invariants
from .conftest import LISTEN, UPSTREAM, CutLinkAt, proxied_store, wait_until

pytestmark = pytest.mark.network

THREE = WorkflowDefinition(name="three", version=1, nodes=[
    {"id": "s1", "agent": "g"},
    {"id": "s2", "agent": "g", "depends_on": ["s1"]},
    {"id": "s3", "agent": "g", "depends_on": ["s2"]},
])


class CountingEcho(EchoExecutor):
    """The effect ledger: one entry per execution, shared by nothing — each worker has its own."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, req, progress):
        self.calls.append(req.step_id)
        return super().execute(req, progress)


@pytest.fixture
def definitions(pg_direct):
    pg_direct.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    pg_direct.put_workflow(THREE)
    return pg_direct


@pytest.mark.parametrize("cut_at,s2_runs", [
    (faults.BEFORE_EFFECT_COMMIT, 2),      # output produced, commit lost → re-run, one completion
    (faults.AFTER_EFFECT_COMMIT, 1),       # committed → replayed from the log, not re-executed
], ids=["cut-before-s2-commit", "cut-after-s2-commit"])
def test_partition_mid_step_recovers_from_the_log_with_one_completion_per_step(
        toxiproxy, definitions, pg_schema, cut_at, s2_runs):
    direct = definitions
    toxiproxy.proxy("pg", LISTEN, UPSTREAM)
    slow = proxied_store(pg_schema)

    exec_a, exec_b = CountingEcho(), CountingEcho()
    cut = CutLinkAt(toxiproxy, "pg", cut_at, "s2")
    engine_a = Engine(store=slow, blobs=slow, executors={"echo": exec_a}, faults=cut)
    worker_a = Worker(engine_a, slow, lease=slow, queue=slow, holder="A", lease_ttl=1.0,
                      faults=cut)
    engine_b = Engine(store=direct, blobs=direct, executors={"echo": exec_b})
    worker_b = Worker(engine_b, direct, lease=direct, queue=direct, holder="B", lease_ttl=30.0)

    run_id = engine_b.create_run("three", request_id="partition")
    direct.push(run_id)

    outcome: dict = {}

    def run_a():
        try:
            worker_a.run_once(timeout=2.0)
            outcome["result"] = "returned"
        except ConflictError as exc:
            outcome["result"] = f"conflict: {exc}"
        except Exception as exc:  # noqa: BLE001 — the transport error is the expected outcome
            outcome["result"] = f"transport: {type(exc).__name__}"

    t = threading.Thread(target=run_a, name="worker-A")
    t.start()
    wait_until(lambda: cut.fired, timeout=30, what="the partition at s2")

    # A is now partitioned. Its lease (1 s) lapses; B's sweep finds the run non-terminal.
    time.sleep(1.3)
    wait_until(lambda: direct.acquire(run_id, "probe", 0.01) is not None, timeout=10,
               what="A's lease to expire")
    direct.release(direct.acquire(run_id, "probe", 0.01))
    assert run_id in worker_b.recover()
    assert worker_b.run_once(timeout=2.0) == run_id
    assert engine_b.get_run(run_id).status.value == "completed"

    # Heal the link. A's pool reconnects, A unwinds — and must not write anything.
    toxiproxy.enable("pg")
    t.join(timeout=60)
    assert not t.is_alive(), "worker A hung after the partition"
    assert outcome["result"].startswith("transport:"), outcome

    events = direct.read_events(run_id)
    completed = [e.step_id for e in events if isinstance(e, StepCompleted)]
    assert completed == ["s1", "s2", "s3"], completed                       # exactly one each
    attempts = Counter(e.step_id for e in events if isinstance(e, StepStarted))
    assert attempts == {"s1": 1, "s2": s2_runs, "s3": 1}, attempts
    assert exec_a.calls == ["s1", "s2"]
    assert exec_b.calls == (["s2", "s3"] if s2_runs == 2 else ["s3"]), exec_b.calls
    assert Counter(exec_a.calls + exec_b.calls)["s2"] == s2_runs
    with direct.connection() as conn:
        (max_fence,) = conn.execute(
            "SELECT max_fence FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        (b_fence,) = conn.execute(
            "SELECT fence FROM leases WHERE run_id = %s", (run_id,)).fetchone()
    assert max_fence == b_fence and b_fence >= 2
    assert_invariants(direct, run_id)

    # One log: the view through the healed proxy is the direct view.
    wait_until(lambda: len(slow.read_events(run_id)) == len(events), timeout=30,
               what="the proxied store to reconnect")
    assert [e.to_record() for e in slow.read_events(run_id)] == [e.to_record() for e in events]
    slow.close()
