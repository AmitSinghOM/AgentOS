"""Store contract: every adapter passes exactly this suite. Adding an adapter means adding
one fixture below, nothing else (docs/DEVELOPMENT_STRUCTURE.md §5.2)."""
from __future__ import annotations

import os
import threading

import pytest

from agentos.core.events import RunCompleted, RunStarted, StepCompleted, StepStarted
from agentos.core.models import Agent, AgentType, RunStatus, WorkflowDefinition
from agentos.core.ports import ConflictError
from agentos.store.memory import MemoryStore
from agentos.store.sqlite import SqliteStore

PG_DSN = os.environ.get("AGENTOS_TEST_PG_DSN")  # e.g. postgresql://localhost/agentos_test

ADAPTERS = ["memory", "sqlite-file", "sqlite-memory"] + (["postgres"] if PG_DSN else [])


def _postgres_store(schema: str):
    from agentos.store.postgres import PostgresStore
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
    from agentos.core.fold import fold
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
    from agentos.core.models import BlobRef
    with pytest.raises(KeyError):
        store.get(BlobRef(sha256="0" * 64, size=1))


def test_step_completed_carries_blob_ref_not_bytes(store):
    ref = store.put(b'{"out":true}')
    store.append_events("r1", 0, [_started(), StepCompleted(
        run_id="r1", step_id="s", attempt=1, idempotency_key="k", output_ref=ref)])
    (_, done) = store.read_events("r1")
    assert isinstance(done, StepCompleted) and done.output_ref == ref
    assert b'"out"' not in str(done.to_record()).encode()  # payload never in the log
