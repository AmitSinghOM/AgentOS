"""Lease + Queue contract: every coordination adapter passes exactly this suite."""
from __future__ import annotations

import os
import threading
import time

import pytest

from dagentos.core.coordination import LeaseToken
from dagentos.core.events import RunStarted, StepStarted
from dagentos.core.ports import ConflictError
from dagentos.store.memory import MemoryStore
from dagentos.store.sqlite import SqliteStore

PG_DSN = os.environ.get("AGENTOS_TEST_PG_DSN")
ADAPTERS = ["memory", "sqlite"] + (["postgres"] if PG_DSN else [])


@pytest.fixture(params=ADAPTERS)
def coord(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "sqlite":
        s = SqliteStore(tmp_path / "c.db")
        yield s
        s.close()
    else:
        from dagentos.store.postgres import PostgresStore
        schema = f"c_{tmp_path.name.lower().replace('-', '_')}"[:60]
        s = PostgresStore(PG_DSN, schema=schema)
        try:
            yield s
        finally:
            with s.connection() as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            s.close()


# ------------------------------------------------------------------- lease

def test_one_holder_at_a_time_and_fences_increase(coord):
    a = coord.acquire("r", "A", 5.0)
    assert a is not None and a.fence == 1
    assert coord.acquire("r", "B", 5.0) is None            # held by A
    assert coord.renew(a, 5.0) is True
    coord.release(a)
    b = coord.acquire("r", "B", 5.0)
    assert b is not None and b.fence == 2                   # strictly increasing
    assert coord.renew(a, 5.0) is False                     # A's token is dead


def test_expired_lease_can_be_taken_and_old_token_cannot_renew(coord):
    a = coord.acquire("r", "A", 0.05)
    time.sleep(0.15)
    b = coord.acquire("r", "B", 5.0)
    assert b is not None and b.fence == a.fence + 1
    assert coord.renew(a, 5.0) is False
    assert coord.renew(b, 5.0) is True


def test_same_holder_may_reacquire_its_own_live_lease(coord):
    a = coord.acquire("r", "A", 5.0)
    a2 = coord.acquire("r", "A", 5.0)
    assert a2 is not None and a2.fence == a.fence + 1


def test_release_is_scoped_to_the_token(coord):
    a = coord.acquire("r", "A", 5.0)
    stale = LeaseToken(run_id="r", holder="A", fence=a.fence - 1)
    coord.release(stale)                                     # must not release A's lease
    assert coord.acquire("r", "B", 5.0) is None


def test_stale_fence_cannot_append(coord):
    """The property that turns lease expiry into a guarantee (C6)."""
    coord.append_events("r", 0, [RunStarted(run_id="r", workflow="w", workflow_version=1,
                                            request_id="q")], fence=1)
    coord.append_events("r", 1, [StepStarted(run_id="r", step_id="a", attempt=1, agent="e",
                                             idempotency_key="k")], fence=2)
    with pytest.raises(ConflictError, match="fence"):
        coord.append_events("r", 2, [StepStarted(run_id="r", step_id="b", attempt=1,
                                                 agent="e", idempotency_key="k2")], fence=1)
    assert len(coord.read_events("r")) == 2
    # Equal fence is fine (same holder, several appends); unfenced appends are allowed
    # (API writes run.started without a lease).
    coord.append_events("r", 2, [StepStarted(run_id="r", step_id="b", attempt=1, agent="e",
                                             idempotency_key="k2")], fence=2)
    coord.append_events("r", 3, [StepStarted(run_id="r", step_id="c", attempt=1, agent="e",
                                             idempotency_key="k3")])
    assert len(coord.read_events("r")) == 4


def test_fence_is_recorded_at_acquire_not_first_write(coord):
    """Closes the window between B taking the lease and B's first append: a stale A
    that writes in that window must already be rejected."""
    coord.append_events("r", 0, [RunStarted(run_id="r", workflow="w", workflow_version=1,
                                            request_id="q")])
    a = coord.acquire("r", "A", 0.05)
    time.sleep(0.15)                                          # A stalls past its TTL
    b = coord.acquire("r", "B", 5.0)                          # B takes over, writes nothing yet
    assert b.fence > a.fence
    with pytest.raises(ConflictError, match="fence"):
        coord.append_events("r", 1, [StepStarted(run_id="r", step_id="a", attempt=1, agent="e",
                                                 idempotency_key="k")], fence=a.fence)
    assert len(coord.read_events("r")) == 1
    coord.append_events("r", 1, [StepStarted(run_id="r", step_id="a", attempt=1, agent="e",
                                             idempotency_key="k")], fence=b.fence)


# ------------------------------------------------------------------- queue

def test_queue_delivers_once_then_redelivers_if_unacked(coord):
    coord.visibility_seconds = 0.1
    coord.push("r1")
    coord.push("r1")                                         # de-duplicated while waiting
    assert coord.pull(0.2) == "r1"
    assert coord.pull(0.05) is None                          # invisible while in flight
    time.sleep(0.15)
    assert coord.pull(0.2) == "r1"                           # redelivered (at-least-once)
    coord.ack("r1")
    time.sleep(0.15)
    assert coord.pull(0.05) is None


def test_push_of_inflight_run_makes_it_visible_now(coord):
    """The recovery sweep relies on this: a run whose worker died is re-pushed and must
    be deliverable immediately, not after the visibility timeout."""
    coord.visibility_seconds = 30.0
    coord.push("r1")
    assert coord.pull(0.2) == "r1"
    assert coord.pull(0.05) is None
    coord.push("r1")                                         # sweep
    assert coord.pull(0.2) == "r1"


def test_push_with_delay_defers_visibility(coord):
    """Retry backoff primitive: the run is not deliverable before now + delay."""
    coord.visibility_seconds = 30.0
    coord.push("r1", delay_seconds=0.3)
    assert coord.pull(0.1) is None                           # not yet
    time.sleep(0.25)
    assert coord.pull(0.3) == "r1"                           # now
    # A delayed re-push of an in-flight run replaces its visibility time.
    coord.push("r1", delay_seconds=5.0)
    assert coord.pull(0.1) is None


def test_queue_is_fifo_and_two_pullers_never_share_a_delivery(coord):
    coord.visibility_seconds = 5.0
    for r in ("a", "b", "c", "d"):
        coord.push(r)
    got: list[str] = []
    lock = threading.Lock()

    def puller():
        while (r := coord.pull(0.2)) is not None:
            with lock:
                got.append(r)

    ts = [threading.Thread(target=puller) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got) == ["a", "b", "c", "d"]              # each exactly once


def test_queue_depth_counts_waiting_and_inflight(coord):
    coord.visibility_seconds = 30.0
    assert coord.queue_depth() == 0
    coord.push("a")
    coord.push("b")
    assert coord.queue_depth() == 2
    assert coord.pull(0.2) in ("a", "b")
    assert coord.queue_depth() == 2                          # in flight still counts
    coord.ack("a")
    coord.ack("b")
    assert coord.queue_depth() == 0
