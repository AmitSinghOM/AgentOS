"""Layer 2, scenario 5: `restart_after_partial_wave` (the Phase 1 kill -9 demo under network
lag, on a fan-out wave; C1/C4/C6; audit E-12).

A REAL worker process (`python -m dagentos.worker --once`) runs the Phase 2 demo DAG
n1 → [n2, n3, n4] → n5 against Postgres THROUGH Toxiproxy with a latency toxic already in
place, so every round trip to the database lags. `AGENTOS_FAULT=after_effect_commit:n2`
makes it `os._exit(137)` the instant n2's `step.completed` is committed — in the middle of the
fan-out wave: n3 and n4 have started (their `step.started` is in the log), their outputs
exist in that process's memory, and neither is committed. Nothing is flushed or released;
the lease stays live until its TTL.

A second real process on the DIRECT link resumes from the log alone. What must be true
(tests/chaos/test_kill9_real_process.py, extended to a partial wave):
  - the killer exits 137 and leaves exactly [n1, n2] completed, n3/n4 started-not-completed,
    the run `running`, the dead worker's lease still live;
  - the second worker completes the run: exactly one `step.completed` per step;
  - n2 — committed — is NEVER started again (attempts == 1): the log is replayed, not the code;
  - n3 and n4 — produced but uncommitted — are re-attempted exactly once each (attempt 2), the
    `before_effect_commit` row of C1 played out by a real SIGKILL under lag;
  - n5 ran once and saw all three branch outputs (C4: a partial wave loses nothing committed);
  - the chaos-plan invariants hold on the final log.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter

import pytest

from dagentos.core.events import StepCompleted, StepStarted
from dagentos.core.fold import fold
from dagentos.core.models import Agent, AgentType, WorkflowDefinition

from ..conftest import assert_invariants
from .conftest import LISTEN, PG_DSN, UPSTREAM, proxied_dsn, wait_until

pytestmark = pytest.mark.network

LATENCY_MS = 150
# Lease TTL of the worker that gets killed. It must outlive the longest gap between two lease
# renewals UNDER LAG: the fan-out wave appends three `step.started` (each ~5 lagged round
# trips inside one transaction) before any step's progress() renews. First CI run of this
# test (PR #152) used 1.0 s: the lease lapsed during that gap, the heartbeat raised LeaseLost
# and the worker exited 0 before ever reaching n2's commit — the A5 lease-loss path working,
# which is scenario 1's subject, not this one's. 8 s leaves ~3x margin over the ~2.5 s gap.
KILLER_TTL = 8.0
FAN = WorkflowDefinition(name="fan", version=1, max_parallelism=3, nodes=[
    {"id": "n1", "agent": "g"},
    {"id": "n2", "agent": "g", "depends_on": ["n1"]},
    {"id": "n3", "agent": "g", "depends_on": ["n1"]},
    {"id": "n4", "agent": "g", "depends_on": ["n1"]},
    {"id": "n5", "agent": "g", "depends_on": ["n2", "n3", "n4"]},
])


def _worker(dsn: str, schema: str, *, fault: str | None, holder: str, ttl: float):
    env = os.environ | {
        "AGENTOS_STORE": "postgres",
        "AGENTOS_PG_DSN": dsn,
        "AGENTOS_PG_SCHEMA": schema,
        "AGENTOS_LOG": "WARNING",
        "AGENTOS_WORKER_METRICS_PORT": "0",      # two workers on one host: no port to clash on
    }
    if fault:
        env["AGENTOS_FAULT"] = fault
    else:
        env.pop("AGENTOS_FAULT", None)
    return subprocess.run(
        [sys.executable, "-m", "dagentos.worker", "--once", "--holder", holder,
         "--lease-ttl", str(ttl)],
        env=env, capture_output=True, text=True, timeout=180, check=False,  # we assert rc
    )


def test_kill_9_mid_fanout_under_lag_then_restart_replays_committed_steps_only(
        toxiproxy, pg_direct, pg_schema):
    direct = pg_direct
    direct.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    direct.put_workflow(FAN)
    toxiproxy.proxy("pg", LISTEN, UPSTREAM)
    toxiproxy.latency("pg", LATENCY_MS)                    # the wire is lagging before it starts

    from dagentos.core.engine import Engine
    run_id = Engine(store=direct, blobs=direct, executors={}).create_run("fan", request_id="pw")
    direct.push(run_id)

    first = _worker(proxied_dsn(PG_DSN), pg_schema, fault="after_effect_commit:n2",
                    holder="worker-1", ttl=KILLER_TTL)
    assert first.returncode == 137, (first.returncode, first.stderr[-800:])
    toxiproxy.remove_toxic("pg", "lat")

    mid = direct.read_events(run_id)
    completed_mid = [e.step_id for e in mid if isinstance(e, StepCompleted)]
    started_mid = Counter(e.step_id for e in mid if isinstance(e, StepStarted))
    assert completed_mid == ["n1", "n2"], completed_mid
    assert started_mid == {"n1": 1, "n2": 1, "n3": 1, "n4": 1}, started_mid   # a partial wave
    assert fold(mid).status.value == "running"
    assert direct.acquire(run_id, "probe", 0.01) is None, "dead worker's lease should still be live"

    # worker-1's lease expires by TTL (database clock), as it would in production.
    wait_until(lambda: direct.acquire(run_id, "probe", 0.01) is not None,
               timeout=KILLER_TTL + 10, what="the dead worker's lease to expire")
    direct.release(direct.acquire(run_id, "probe", 0.01))
    second = _worker(PG_DSN, pg_schema, fault=None, holder="worker-2", ttl=30.0)
    assert second.returncode == 0, second.stderr[-800:]
    assert second.stdout.strip() == run_id              # --once prints the run it handled

    events = direct.read_events(run_id)
    run = fold(events)
    assert run.status.value == "completed", run.error
    assert Counter(e.step_id for e in events if isinstance(e, StepCompleted)) == \
        {"n1": 1, "n2": 1, "n3": 1, "n4": 1, "n5": 1}                        # one each
    attempts = Counter(e.step_id for e in events if isinstance(e, StepStarted))
    assert attempts == {"n1": 1, "n2": 1, "n3": 2, "n4": 2, "n5": 1}, attempts
    # n5 saw every branch: its recorded inputs name all three, with n2's FIRST (only) output.
    n5_started = next(e for e in events if isinstance(e, StepStarted) and e.step_id == "n5")
    assert n5_started.attempt == 1
    final = Engine(store=direct, blobs=direct, executors={}).get_run(run_id)
    by_node = {s.node_id: s for s in final.steps}
    assert by_node["n5"].output["received"] == {n: by_node[n].output for n in ("n2", "n3", "n4")}
    assert by_node["n2"].attempt == 1 and by_node["n3"].attempt == 2 and by_node["n4"].attempt == 2
    # The second process wrote under a higher fence than the dead one.
    with direct.connection() as conn:
        (max_fence,) = conn.execute(
            "SELECT max_fence FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    assert max_fence >= 2
    assert_invariants(direct, run_id)
