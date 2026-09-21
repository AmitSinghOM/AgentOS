"""Store contract: every adapter passes exactly this suite. Adding an adapter means adding
one fixture below, nothing else (docs/DEVELOPMENT_STRUCTURE.md §5.2)."""
from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest

from dagentos.core.events import RunCompleted, RunStarted, StepCompleted, StepStarted
from dagentos.core.models import Agent, AgentType, RunStatus, WorkflowDefinition
from dagentos.core.ports import ConflictError
from dagentos.store.memory import MemoryStore
from dagentos.store.sqlite import SqliteStore

PG_DSN = os.environ.get("AGENTOS_TEST_PG_DSN")  # e.g. postgresql://localhost/agentos_test

ADAPTERS = ["memory", "sqlite-file", "sqlite-memory"] + (["postgres"] if PG_DSN else [])


def _postgres_store(schema: str):
    from dagentos.store.postgres import PostgresStore
    return PostgresStore(PG_DSN, schema=schema, max_size=4)


@pytest.fixture(params=ADAPTERS)
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "sqlite-file":
        s = SqliteStore(tmp_path / "agentos.db")
        yield s
        s.close()
    elif request.param == "sqlite-memory":
        s = SqliteStore(":memory:")
        yield s
        s.close()
    else:
        # One throwaway schema per test = isolation without a DB per test.
        schema = f"t_{tmp_path.name.lower().replace('-', '_')}"[:60]
        s = _postgres_store(schema)
        try:
            yield s
        finally:
            with s.connection() as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            s.close()


def test_postgres_adapter_is_exercised_when_dsn_is_set():
    """Guard against the Postgres leg silently vanishing from CI."""
    if not PG_DSN:
        pytest.skip("AGENTOS_TEST_PG_DSN not set — Postgres leg skipped (CI sets it)")
    assert "postgres" in ADAPTERS


def _started(run_id="r1", request_id="req-1"):
    return RunStarted(run_id=run_id, workflow="wf", workflow_version=1, request_id=request_id)


# ---------------------------------------------------------------- definitions

def test_agent_and_workflow_round_trip_with_enums(store):
    store.put_agent(Agent(name="a", type=AgentType.echo, config={"k": 1}))
    got = store.get_agent("a")
    assert got is not None and got.type is AgentType.echo  # enum, not str (agno #8454)
    assert [a.name for a in store.list_agents()] == ["a"]
    assert store.get_agent("missing") is None

    wf = WorkflowDefinition(name="wf", version=3, nodes=[{"id": "x", "agent": "a"}])
    store.put_workflow(wf)
    assert store.get_workflow("wf") == wf
    store.put_workflow(wf.model_copy(update={"version": 4}))
    assert store.get_workflow("wf").version == 4  # upsert


def test_agent_versions_are_immutable_and_latest_wins(store):
    v1 = Agent(name="a", version=1, type=AgentType.echo, config={"m": "one"})
    store.put_agent(v1)
    store.put_agent(v1)                                            # identical: no-op
    with pytest.raises(ConflictError, match="bump the version"):
        store.put_agent(v1.model_copy(update={"config": {"m": "changed"}}))
    assert store.get_agent("a").config == {"m": "one"}              # unchanged

    v2 = Agent(name="a", version=2, type=AgentType.echo, config={"m": "two"})
    store.put_agent(v2)
    assert store.get_agent("a") == v2                               # latest by default
    assert store.get_agent("a", version=1) == v1                    # pinned lookup
    assert store.get_agent("a", version=9) is None
    assert store.list_agent_versions("a") == [1, 2]
    assert store.list_agent_versions("nope") == []
    store.put_agent(Agent(name="b", version=1, type=AgentType.echo))
    assert [(x.name, x.version) for x in store.list_agents()] == [("a", 2), ("b", 1)]


# ------------------------------------------------------------------ run log

def test_append_assigns_dense_monotonic_seq(store):
    out = store.append_events("r1", 0, [_started(), StepStarted(
        run_id="r1", step_id="s", attempt=1, agent="a", idempotency_key="k")])
    assert [e.seq for e in out] == [1, 2]
    out2 = store.append_events("r1", 2, [RunCompleted(run_id="r1")])
    assert out2[0].seq == 3
    assert [e.seq for e in store.read_events("r1")] == [1, 2, 3]
    assert [e.seq for e in store.read_events("r1", after_seq=2)] == [3]
    assert store.read_events("nope") == []


def test_append_with_stale_expected_seq_conflicts_and_writes_nothing(store):
    store.append_events("r1", 0, [_started()])
    with pytest.raises(ConflictError):
        store.append_events("r1", 0, [RunCompleted(run_id="r1")])  # stale
    with pytest.raises(ConflictError):
        store.append_events("r1", 5, [RunCompleted(run_id="r1")])  # ahead
    assert [e.seq for e in store.read_events("r1")] == [1]


def test_batch_is_atomic_on_conflict(store):
    store.append_events("r1", 0, [_started()])
    # Second RunStarted reuses request_id → must conflict, and the batch's first
    # (valid) event must NOT have been written.
    with pytest.raises(ConflictError):
        store.append_events("r1", 1, [RunCompleted(run_id="r1"), _started(run_id="r1")])
    assert [e.seq for e in store.read_events("r1")] == [1]


def test_read_returns_typed_events_with_enum_status_after_round_trip(store):
    store.append_events("r1", 0, [_started()])
    (ev,) = store.read_events("r1")
    assert isinstance(ev, RunStarted)
    assert ev.request_id == "req-1" and ev.schema_version == 1
    # Exercise fold end to end through the adapter.
    from dagentos.core.fold import fold
    run = fold(store.read_events("r1"))
    assert run.status is RunStatus.running


def test_request_id_makes_run_start_idempotent(store):
    store.append_events("r1", 0, [_started(run_id="r1", request_id="dup")])
    assert store.run_id_for_request("dup") == "r1"
    assert store.run_id_for_request("other") is None
    with pytest.raises(ConflictError):
        store.append_events("r2", 0, [_started(run_id="r2", request_id="dup")])
    assert store.read_events("r2") == []
    assert store.list_run_ids() == ["r1"]


def test_concurrent_appenders_never_both_win(store):
    """Two writers with the same expected_seq: exactly one succeeds (C6 groundwork)."""
    store.append_events("r1", 0, [_started()])
    results: list[str] = []
    barrier = threading.Barrier(2)

    def writer(tag: str):
        barrier.wait()
        try:
            store.append_events("r1", 1, [StepStarted(
                run_id="r1", step_id=tag, attempt=1, agent="a", idempotency_key=tag)])
            results.append(f"ok:{tag}")
        except ConflictError:
            results.append(f"conflict:{tag}")

    ts = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(r.split(":")[0] for r in results) == ["conflict", "ok"]
    assert [e.seq for e in store.read_events("r1")] == [1, 2]


# -------------------------------------------------------------------- blobs

def test_blob_store_is_content_addressed_and_idempotent(store):
    ref = store.put(b'{"a":1}')
    again = store.put(b'{"a":1}')
    assert ref == again and ref.size == 7
    assert store.exists(ref) and store.get(ref) == b'{"a":1}'
    other = store.put(b"png-bytes", media_type="image/png")
    assert other.media_type == "image/png" and other.sha256 != ref.sha256
    from dagentos.core.models import BlobRef
    with pytest.raises(KeyError):
        store.get(BlobRef(sha256="0" * 64, size=1))


def test_step_completed_carries_blob_ref_not_bytes(store):
    ref = store.put(b'{"out":true}')
    store.append_events("r1", 0, [_started(), StepCompleted(
        run_id="r1", step_id="s", attempt=1, idempotency_key="k", output_ref=ref)])
    (_, done) = store.read_events("r1")
    assert isinstance(done, StepCompleted) and done.output_ref == ref
    assert b'"out"' not in str(done.to_record()).encode()  # payload never in the log


def test_event_chain_survives_the_adapter_round_trip(store):
    """C12: the hash is computed over the record BEFORE append; the adapter's serialization
    (datetimes, enums, decimals, nested models) must reproduce those exact bytes on read."""
    from dagentos.agents.echo import EchoExecutor
    from dagentos.core.engine import Engine
    from dagentos.core.integrity import event_hash, verify

    store.put_agent(Agent(name="g", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()},
                 lease=store if hasattr(store, "acquire") else None)
    run = eng.start_run("w", inputs={"topic": "round trip"})
    events = store.read_events(run.id)
    assert all(e.hash and event_hash(e) == e.hash for e in events)
    assert verify(events) == len(events) == run.integrity_verified >= 7


def test_ping_is_a_bounded_round_trip_and_returns_quickly(store):
    """Production pass 2 self-review (R1): `GET /ready` must answer inside a probe window.
    `ping` performs one real round-trip and honours its timeout — on PostgreSQL the pool's
    acquire wait, which otherwise defaults to 30 s (psycopg_pool `timeout`)."""
    started = time.monotonic()
    store.ping(timeout=2.0)                          # healthy: returns None, raises nothing
    assert time.monotonic() - started < 2.0


def test_postgres_ping_passes_the_timeout_to_the_pool_acquire():
    """The bound is the point: a store that cannot hand out a connection must fail the probe
    within `timeout`, not after the pool's 30 s default. Exercised against the pool API the
    adapter really calls (a live outage cannot be staged in CI)."""
    from dagentos.store.postgres import PostgresStore

    class FakePool:
        def __init__(self):
            self.timeouts: list[float | None] = []

        @contextlib.contextmanager
        def connection(self, timeout=None):
            self.timeouts.append(timeout)
            raise TimeoutError("pool exhausted (simulated)")

    store = PostgresStore.__new__(PostgresStore)
    store._pool = FakePool()
    with pytest.raises(TimeoutError):
        store.ping(timeout=1.5)
    assert store._pool.timeouts == [1.5]
