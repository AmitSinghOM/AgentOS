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


# ------------------------------------------------------- scheduler-wide pause (A12 / E-03)

def test_scheduler_state_defaults_to_running_and_the_newest_row_wins(store):
    from datetime import UTC, datetime

    from dagentos.core.events import SchedulerState
    from dagentos.core.models import Principal, PrincipalKind

    assert store.get_scheduler_state() == SchedulerState()           # fresh: never paused
    amit = Principal(kind=PrincipalKind.human, id="amit")
    t1 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    store.set_scheduler_state(SchedulerState(paused=True, principal=amit, reason="leaked key",
                                             changed_at=t1))
    got = store.get_scheduler_state()
    assert got.paused is True and got.reason == "leaked key"
    assert got.principal == amit and got.principal.kind is PrincipalKind.human   # typed back
    assert got.changed_at == t1 and got.changed_at.tzinfo is not None

    t2 = datetime(2026, 11, 16, 12, 30, tzinfo=UTC)
    store.set_scheduler_state(SchedulerState(paused=False, principal=None, reason="rotated",
                                             changed_at=t2))
    got = store.get_scheduler_state()
    assert got == SchedulerState(paused=False, principal=None, reason="rotated", changed_at=t2)

    # A row without a timestamp is stamped by the store, never left None.
    store.set_scheduler_state(SchedulerState(paused=True))
    assert store.get_scheduler_state().paused is True
    assert store.get_scheduler_state().changed_at is not None


# ----------------------------------------------------------- run index (audit E-07)

def _at(minute: int):
    from datetime import UTC, datetime
    return datetime(2026, 11, 17, 10, minute, 0, 123456, tzinfo=UTC)


def _start(store, run_id: str, minute: int, workflow: str = "wf", then=()) -> None:
    store.append_events(run_id, 0, [RunStarted(run_id=run_id, workflow=workflow,
                                               workflow_version=1, request_id=f"req-{run_id}",
                                               occurred_at=_at(minute)), *then])


def _ids(page) -> list[str]:
    return [r.run_id for r in page.runs]


def test_run_index_row_follows_every_append_and_a_conflict_changes_nothing(store):
    from dagentos.core.events import ApprovalGranted, ApprovalRequested, RunSuspended
    from dagentos.core.models import EffectClass

    def row():
        (r,) = store.list_runs().runs
        return r

    _start(store, "r1", 1)
    assert (row().status, row().workflow, row().updated_at) == (RunStatus.running, "wf", _at(1))
    assert row().created_at == _at(1)
    spend = [EffectClass.spend]
    store.append_events("r1", 1, [
        ApprovalRequested(run_id="r1", approval_id="a1", step_id="s1", effect_classes=spend,
                          occurred_at=_at(2)),
        ApprovalRequested(run_id="r1", approval_id="a2", step_id="s2", effect_classes=spend,
                          occurred_at=_at(2)),
        RunSuspended(run_id="r1", occurred_at=_at(3))])
    assert (row().status, row().updated_at) == (RunStatus.suspended, _at(3))
    store.append_events("r1", 4, [ApprovalGranted(run_id="r1", approval_id="a1", step_id="s1")])
    assert row().status is RunStatus.suspended                    # a2 still pending
    with pytest.raises(ConflictError):                            # stale: nothing written
        store.append_events("r1", 4, [RunCompleted(run_id="r1")])
    assert row().status is RunStatus.suspended
    store.append_events("r1", 5, [ApprovalGranted(run_id="r1", approval_id="a2", step_id="s2")])
    assert row().status is RunStatus.running
    store.append_events("r1", 6, [RunCompleted(run_id="r1", occurred_at=_at(9))])
    assert (row().status, row().updated_at) == (RunStatus.completed, _at(9))


def test_list_runs_filters_on_the_index_newest_created_first(store):
    from dagentos.core.events import RunFailed, RunSuspended
    _start(store, "a", 1, then=[RunCompleted(run_id="a")])
    _start(store, "b", 2, workflow="other", then=[RunSuspended(run_id="b")])
    _start(store, "c", 3, then=[RunFailed(run_id="c", error="x")])
    _start(store, "d", 4, then=[RunSuspended(run_id="d")])
    _start(store, "e", 5)

    assert _ids(store.list_runs()) == ["e", "d", "c", "b", "a"]
    assert _ids(store.list_runs(status=[RunStatus.suspended])) == ["d", "b"]
    assert _ids(store.list_runs(status=[RunStatus.running, RunStatus.failed])) == ["e", "c"]
    assert _ids(store.list_runs(workflow="other")) == ["b"]
    assert _ids(store.list_runs(status=[RunStatus.suspended], workflow="wf")) == ["d"]
    assert _ids(store.list_runs(status=[RunStatus.paused])) == []
    assert _ids(store.list_runs(limit=None)) == ["e", "d", "c", "b", "a"]
    assert store.list_runs(limit=None).next_cursor is None
    assert store.run_status_counts() == {"completed": 1, "suspended": 2, "failed": 1,
                                         "running": 1}
    assert store.run_status_counts(workflow="other") == {"suspended": 1}


def test_list_runs_cursor_returns_every_run_exactly_once_while_statuses_change(store):
    """The cursor is (created_at, run_id), which never moves: a run that changes status between
    two page requests is neither repeated nor skipped. (An updated_at cursor would skip it.)"""
    for i in range(7):
        _start(store, f"r{i}", i)
    seen: list[str] = []
    page = store.list_runs(limit=3)
    seen += _ids(page)
    assert seen == ["r6", "r5", "r4"] and page.next_cursor is not None
    store.append_events("r5", 1, [RunCompleted(run_id="r5", occurred_at=_at(30))])   # seen
    store.append_events("r1", 1, [RunCompleted(run_id="r1", occurred_at=_at(31))])   # unseen
    _start(store, "late", 40)                                      # newer than the cursor
    while page.next_cursor is not None:
        page = store.list_runs(limit=3, cursor=page.next_cursor)
        seen += _ids(page)
    assert seen == ["r6", "r5", "r4", "r3", "r2", "r1", "r0"]       # once each, no gap


def test_list_runs_cursor_pages_a_status_filter_too(store):
    from dagentos.core.events import RunSuspended
    for i in range(5):
        _start(store, f"s{i}", i, then=[RunSuspended(run_id=f"s{i}")])
        _start(store, f"x{i}", 10 + i)
    first = store.list_runs(status=[RunStatus.suspended], limit=2)
    second = store.list_runs(status=[RunStatus.suspended], limit=2, cursor=first.next_cursor)
    third = store.list_runs(status=[RunStatus.suspended], limit=2, cursor=second.next_cursor)
    assert _ids(first) + _ids(second) + _ids(third) == ["s4", "s3", "s2", "s1", "s0"]
    assert third.next_cursor is None


def test_list_runs_refuses_a_cursor_it_did_not_issue(store):
    _start(store, "r1", 1)
    with pytest.raises(ValueError, match="invalid cursor"):
        store.list_runs(cursor="not-a-cursor")


# ------------------------------------------------------------- purge_run (audit E-08)

def _two_runs_sharing_a_blob(store):
    """r1 and r2 both reference `shared` (a pricing table, say); only r1 references `own`."""
    shared = store.put(b'{"table": "prices"}')
    own = store.put(b'{"out": "r1 only"}')
    other = store.put(b'{"out": "r2 only"}')
    for rid, ref in (("r1", own), ("r2", other)):
        _start(store, rid, 1 if rid == "r1" else 2, then=[
            StepCompleted(run_id=rid, step_id="s", attempt=1, idempotency_key=f"k-{rid}",
                          output_ref=ref),
            StepCompleted(run_id=rid, step_id="t", attempt=1, idempotency_key=f"j-{rid}",
                          output_ref=shared),
            RunCompleted(run_id=rid, occurred_at=_at(5))])
    return shared, own, other


def test_purge_run_removes_every_row_of_one_run_only_at_the_expected_seq(store):
    """The archive's one write to the store: events, snapshot, queue and lease rows and the run
    row go in one transaction, nothing of any other run moves, and a log that grew since the
    caller read it (a retry on a failed run) is refused — the append-only guarantee for a live
    run is the caller's `expected_seq`, exactly as for `append_events`."""
    _two_runs_sharing_a_blob(store)
    store.put_snapshot("r1", 4, "h", {"last_seq": 4})
    store.push("r1")
    token = store.acquire("r1", "w", ttl_seconds=30)
    assert token is not None
    before_r2 = store.read_events("r2")

    with pytest.raises(ConflictError):                  # stale: the log is at 4, not 3
        store.purge_run("r1", expected_seq=3)
    assert len(store.read_events("r1")) == 4 and store.get_snapshot("r1") is not None
    assert store.queue_depth() == 1

    assert store.purge_run("r1", expected_seq=4) == []  # no blobs asked for → none removed
    assert store.read_events("r1") == []
    assert store.list_run_ids() == ["r2"]
    assert [r.run_id for r in store.list_runs(limit=None).runs] == ["r2"]
    assert store.run_status_counts() == {"completed": 1}
    assert store.run_id_for_request("req-r1") is None     # the idempotency key is released
    assert store.get_snapshot("r1") is None
    assert store.queue_depth() == 0
    assert store.read_events("r2") == before_r2            # the other run is untouched
    assert store.run_id_for_request("req-r2") == "r2"
    # a fresh run under the same id starts from seq 0 with a new fence counter
    store.append_events("r1", 0, [_started(run_id="r1", request_id="req-r1-again")])
    assert [e.seq for e in store.read_events("r1")] == [1]
    assert store.acquire("r1", "w2", ttl_seconds=30) is not None


def test_purge_run_removes_only_blobs_no_remaining_run_references(store):
    """The reference count is honest: a blob is kept while ANY remaining run's record names
    its sha256 (the shared pricing table), removed only when none does; a sha the caller
    passes that no run references at all is removed too (it was this run's, now orphaned)."""
    shared, own, other = _two_runs_sharing_a_blob(store)
    orphan = store.put(b"orphan")
    removed = store.purge_run("r1", expected_seq=4,
                              blobs=[shared.sha256, own.sha256, orphan.sha256, "0" * 64])
    assert sorted(removed) == sorted([own.sha256, orphan.sha256])   # not the unknown sha
    assert store.exists(shared) and store.exists(other)   # r2 still names shared
    assert not store.exists(own) and not store.exists(orphan)
    assert store.read_events("r2")[1].output_ref == other


def test_purge_run_of_an_unknown_run_is_a_conflict_and_writes_nothing(store):
    _two_runs_sharing_a_blob(store)
    with pytest.raises(ConflictError):
        store.purge_run("nope", expected_seq=4)
    assert store.list_run_ids() == ["r1", "r2"]
