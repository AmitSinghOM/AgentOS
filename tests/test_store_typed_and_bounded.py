"""C10 (#10) and C15 (#15) — the store as a typed, append-only, bounded-replay log.

C10: every event type round-trips through every adapter and comes back EQUAL, including
enums (cf. agno #8454), tz-aware datetimes (crewAI #7358), Decimal-as-string costs,
nested models and optionals; schema is versioned via migrations.
C15: `run_events` is append-only with a dense monotonic seq; per-step write size is O(1);
snapshots bound replay so a resume touches at most `snapshot_every` events.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from dagentos.agents.echo import EchoExecutor
from dagentos.core import events as E
from dagentos.core.engine import Engine
from dagentos.core.events import EVENT_TYPES, Event
from dagentos.core.fold import fold
from dagentos.core.integrity import chain
from dagentos.core.models import (
    Agent,
    AgentType,
    ApprovalKind,
    BlobRef,
    Cost,
    Effect,
    EffectClass,
    Meter,
    Principal,
    PrincipalKind,
    Provenance,
    RunStatus,
    WorkflowDefinition,
    WorkflowRun,
)
from dagentos.store.memory import MemoryStore
from dagentos.store.sqlite import SqliteStore

PG_DSN = os.environ.get("AGENTOS_TEST_PG_DSN")
ADAPTERS = ["memory", "sqlite-file", "sqlite-memory"] + (["postgres"] if PG_DSN else [])


@pytest.fixture(params=ADAPTERS)
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "sqlite-file":
        s = SqliteStore(tmp_path / "a.db")
        yield s
        s.close()
    elif request.param == "sqlite-memory":
        s = SqliteStore(":memory:")
        yield s
        s.close()
    else:
        from dagentos.store.postgres import PostgresStore
        schema = f"t_{tmp_path.name.lower().replace('-', '_')}"[:60]
        s = PostgresStore(PG_DSN, schema=schema, max_size=4)
        try:
            yield s
        finally:
            with s.connection() as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            s.close()


# ------------------------------------------------------------------ C10 round trip

T0 = datetime(2026, 9, 15, 11, 22, 33, 123456, tzinfo=UTC)
HUMAN = Principal(kind=PrincipalKind.human, id="amit")
SYSTEM = Principal(kind=PrincipalKind.system, id="expiry")
REF = BlobRef(sha256="ab" * 32, size=1234, media_type="application/json")
COST = Cost(units=[Meter(name="input_tokens", quantity=1234.0), Meter(name="requests", quantity=1)],
            amount="0.000450", currency="USD", pricing_snapshot_hash="cd" * 32)
PROV = Provenance(executor="openai-compat", executor_version="0.1.0", model_id="gpt-4o-mini",
                  model_alias="chat.fast", prompt_hash="ef" * 32)


def every_event_type(run_id: str) -> list[Event]:
    """One instance of EVERY registered event type with every optional field populated
    with a non-default, type-distinct value. Fails if a new event type is added without
    being listed here — that is the point."""
    at = T0
    evs: list[Event] = [
        E.RunStarted(run_id=run_id, workflow="w", workflow_version=3, request_id="req-1",
                     principal=HUMAN, agent_versions={"a": 2, "b": 7}, inputs_ref=REF,
                     parent_run_id="parent-1", occurred_at=at),
        E.StepStarted(run_id=run_id, step_id="s1", attempt=2, agent="a", idempotency_key="k",
                      declared_effects=[EffectClass.compute, EffectClass.spend], agent_version=2,
                      occurred_at=at + timedelta(seconds=1)),
        E.StepProgress(run_id=run_id, step_id="s1", attempt=2, fraction=0.5, note="half",
                       occurred_at=at + timedelta(seconds=2)),
        E.StepCompleted(run_id=run_id, step_id="s1", attempt=2, idempotency_key="k",
                        output_ref=REF,
                        effects=[Effect(effect_class=EffectClass.spend, description="pay",
                                        external_ref="pay_1")],
                        cost=COST, provenance=PROV, occurred_at=at + timedelta(seconds=3)),
        E.StepFailed(run_id=run_id, step_id="s2", attempt=1, error="boom", terminal=False,
                     retry_at=at + timedelta(minutes=5), occurred_at=at + timedelta(seconds=4)),
        E.StepDeadLettered(run_id=run_id, step_id="s3", attempt=3, cause="undeclared",
                           effect_class=EffectClass.write_external, cost=COST,
                           occurred_at=at + timedelta(seconds=5)),
        E.StepRetryRequested(run_id=run_id, step_id="s3", principal=HUMAN, reason="fixed",
                             occurred_at=at + timedelta(seconds=6)),
        E.StepCancelled(run_id=run_id, step_id="s2", attempt=1,
                        occurred_at=at + timedelta(seconds=7)),
        E.RunPauseRequested(run_id=run_id, principal=HUMAN, reason="lunch",
                            occurred_at=at + timedelta(seconds=8)),
        E.RunPaused(run_id=run_id, occurred_at=at + timedelta(seconds=9)),
        E.RunResumed(run_id=run_id, principal=HUMAN, occurred_at=at + timedelta(seconds=10)),
        E.ApprovalRequested(run_id=run_id, approval_id="ap1", step_id="s4",
                            effect_classes=[EffectClass.spend], reason="money",
                            expires_at=at + timedelta(hours=1), kind=ApprovalKind.cost,
                            cost_at_request="0.60", proposed_ceiling="1.10",
                            occurred_at=at + timedelta(seconds=11)),
        E.RunSuspended(run_id=run_id, occurred_at=at + timedelta(seconds=12)),
        E.ApprovalGranted(run_id=run_id, approval_id="ap1", step_id="s4", principal=HUMAN,
                          reason="ok", occurred_at=at + timedelta(seconds=13)),
        E.ApprovalRejected(run_id=run_id, approval_id="ap2", step_id="s5", principal=SYSTEM,
                           reason="expired", occurred_at=at + timedelta(seconds=14)),
        E.ExecutorSubstituted(run_id=run_id, step_id="s6", agent="a", executor="openai-compat",
                              from_model="m1", to_model="m2", reason="alias moved",
                              principal=SYSTEM, occurred_at=at + timedelta(seconds=15)),
        E.RunCancelRequested(run_id=run_id, principal=HUMAN, reason="stop",
                             occurred_at=at + timedelta(seconds=16)),
        E.RunCancelled(run_id=run_id, occurred_at=at + timedelta(seconds=17)),
        E.RunFailed(run_id=run_id, error="x", step_id="s9",
                    occurred_at=at + timedelta(seconds=18)),
        E.RunCompleted(run_id=run_id, occurred_at=at + timedelta(seconds=19)),
        E.PolicyApplied(run_id=run_id, policy_sha256="ab" * 32,
                        narrowed=["spend: allowed → approval_required (always_approve)"],
                        occurred_at=at + timedelta(seconds=20)),
        E.ChainSealed(run_id=run_id, sealed_seq=19, sealed_hash="cd" * 32, alg="hmac-sha256",
                      key_id="k1", signature="ef" * 32, occurred_at=at + timedelta(seconds=21)),
    ]
    assert {type(e).event_type for e in evs} == set(EVENT_TYPES), \
        set(EVENT_TYPES) - {type(e).event_type for e in evs}
    return evs


def test_every_event_type_round_trips_through_the_store_with_equality(store):
    run_id = "rt-run"
    store.put_workflow(WorkflowDefinition(name="w", version=3, nodes=[{"id": "s1", "agent": "a"}]))
    written = chain(every_event_type(run_id), 0, None)      # seq + hashes, like the engine
    store.append_events(run_id, 0, written)
    read = store.read_events(run_id)
    assert len(read) == len(written)
    for w, r in zip(written, read, strict=True):
        assert type(r) is type(w), (type(w), type(r))
        assert r == w, f"{type(w).event_type} did not round-trip"        # model equality
        assert r.to_record() == w.to_record()                            # record equality
    # The enum came back as the enum, not a string; datetimes tz-aware to the microsecond.
    started = next(e for e in read if isinstance(e, E.StepStarted))
    assert started.declared_effects[1] is EffectClass.spend
    assert started.occurred_at == T0 + timedelta(seconds=1) and started.occurred_at.tzinfo
    completed = next(e for e in read if isinstance(e, E.StepCompleted))
    assert completed.cost.decimal() == COST.decimal() and completed.provenance == PROV
    assert next(e for e in read if isinstance(e, E.StepFailed)).retry_at == T0 + timedelta(minutes=5)


def test_read_events_limit_pages_in_seq_order_on_every_adapter(store):
    """Production review A4: `read_events(limit=)` is honoured by every adapter, pages are in
    seq order, `after_seq` + `limit` walk the whole log exactly, and no limit means all."""
    run_id = "pg-run"
    store.put_workflow(WorkflowDefinition(name="w", version=3, nodes=[{"id": "s1", "agent": "a"}]))
    written = chain(every_event_type(run_id), 0, None)
    store.append_events(run_id, 0, written)
    n = len(written)
    assert n > 6
    first = store.read_events(run_id, limit=3)
    assert [e.seq for e in first] == [1, 2, 3]
    walked, after = [], 0
    while True:
        page = store.read_events(run_id, after_seq=after, limit=5)
        if not page:
            break
        assert len(page) <= 5 and [e.seq for e in page] == list(range(after + 1, after + 1 + len(page)))
        walked += page; after = page[-1].seq
    assert [e.seq for e in walked] == list(range(1, n + 1))
    assert store.read_events(run_id, limit=None) == store.read_events(run_id)
    assert store.read_events(run_id, after_seq=n, limit=5) == []


def test_definitions_and_snapshot_state_round_trip(store):
    agent = Agent(name="a", version=2, type=AgentType.llm, executor="openai-compat",
                  declared_effects=[EffectClass.read, EffectClass.spend],
                  config={"model": "chat.fast", "n": 1.5, "nested": {"k": [1, "x", None]}})
    store.put_agent(agent)
    assert store.get_agent("a", version=2) == agent
    assert store.get_agent("a").declared_effects[1] is EffectClass.spend
    wf = WorkflowDefinition(name="w", version=4, nodes=[{"id": "s", "agent": "a"}])
    store.put_workflow(wf)
    assert store.get_workflow("w") == wf
    # A folded run, snapshotted and read back, is the same run. (Postgres enforces that a
    # snapshot belongs to a run that exists — so start one.)
    store.append_events("snap-run", 0, chain([E.RunStarted(
        run_id="snap-run", workflow="w", workflow_version=4, request_id="snap-req")], 0, None))
    run = WorkflowRun(id="snap-run", workflow="w", status=RunStatus.suspended,
                      attempts={"s": 2}, total_cost="0.60", cost_ceiling="1.10",
                      started_at=T0, last_seq=9, last_hash="ff" * 32)
    store.put_snapshot(run.id, 9, run.last_hash, run.model_dump(mode="json"))
    seq, last_hash, state = store.get_snapshot(run.id)
    assert (seq, last_hash) == (9, run.last_hash)
    assert WorkflowRun.model_validate(state) == run
    assert store.get_snapshot("nope") is None


def test_put_snapshot_is_monotonic_per_run(store):
    # Review finding F5: a stale worker (lease lost) also reaches advance()'s finally and
    # snapshots from the store at an OLDER seq; it must not replace the newer snapshot.
    store.append_events("mono-run", 0, chain([E.RunStarted(
        run_id="mono-run", workflow="w", workflow_version=1, request_id="mono-req")], 0, None))
    store.put_snapshot("mono-run", 10, "aa" * 32, {"last_seq": 10})
    store.put_snapshot("mono-run", 5, "bb" * 32, {"last_seq": 5})        # older: ignored
    assert store.get_snapshot("mono-run")[:2] == (10, "aa" * 32)
    store.put_snapshot("mono-run", 10, "cc" * 32, {"last_seq": 10})      # equal: ignored
    assert store.get_snapshot("mono-run")[:2] == (10, "aa" * 32)
    store.put_snapshot("mono-run", 11, "dd" * 32, {"last_seq": 11})      # newer: replaces
    assert store.get_snapshot("mono-run") == (11, "dd" * 32, {"last_seq": 11})


def test_schema_is_versioned_and_migrations_are_idempotent(store):
    if not hasattr(store, "schema_version"):
        pytest.skip("memory adapter has no schema")
    from dagentos.store.migrations import CURRENT_VERSION
    assert store.schema_version() == CURRENT_VERSION >= 2
    assert store.migrate() == []                                          # nothing pending
    assert store.schema_version() == CURRENT_VERSION


# ------------------------------------------------------------------ C15 bounded replay

class CountingStore(MemoryStore):
    """MemoryStore that records how many events each read_events call returned."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[int] = []

    def read_events(self, run_id, after_seq=0, limit=None):
        out = super().read_events(run_id, after_seq, limit)
        self.reads.append(len(out))
        return out


class Tiny:
    """Constant small output, so per-step cost is the engine's, not the payload's."""

    name, version = "tiny", "t"

    def execute(self, req, progress):
        from dagentos.core.models import StepResult
        return StepResult(output={"i": req.step_id},
                          effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                          provenance=Provenance(executor="tiny", executor_version="t"))


def test_thousand_step_run_writes_o1_per_step_and_replays_from_the_snapshot():
    store = CountingStore()
    store.put_agent(Agent(name="g", type=AgentType.echo, executor="tiny"))
    n = 1000
    nodes = [{"id": f"s{i}", "agent": "g", "depends_on": [f"s{i - 1}"] if i else []}
             for i in range(n)]
    store.put_workflow(WorkflowDefinition(name="long", nodes=nodes, max_parallelism=1))
    every = 100
    eng = Engine(store=store, blobs=store, executors={"tiny": Tiny()}, lease=store,
                 snapshot_every=every)
    run = eng.start_run("long")
    assert run.status is RunStatus.completed and len(run.steps) == n

    # Append-only, dense, monotonic — and each step's write is O(1): the records for step
    # 999 are no bigger than for step 1 (the log is not rewritten per turn, cf. agno #8805).
    records = store._events[run.id]
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))
    sizes = [len(json.dumps(r)) for r in records if r["event_type"] == "step.completed"]
    assert len(sizes) == n and max(sizes) / min(sizes) < 1.5

    # A snapshot exists near the tail, and a fresh engine reads at most `every` events.
    snap = store.get_snapshot(run.id)
    assert snap is not None and run.last_seq - snap[0] < every
    store.reads.clear()
    fresh = Engine(store=store, blobs=store, executors={"tiny": Tiny()}, lease=store,
                   snapshot_every=every)
    again = fresh.get_run(run.id, hydrate=False)
    assert store.reads and max(store.reads) <= every                     # bounded replay
    # …and the snapshot is only a cache: the full fold of the log agrees with it exactly.
    assert fold(MemoryStore.read_events(store, run.id)).model_dump() == again.model_dump()
    assert again.status is RunStatus.completed and len(again.steps) == n


def test_a_snapshot_that_does_not_chain_to_the_log_is_ignored_not_trusted():
    store = MemoryStore()
    store.put_agent(Agent(name="g", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()}, lease=store,
                 snapshot_every=1)
    run = eng.start_run("w")
    snap_seq, _, state = store.get_snapshot(run.id)
    # Forge a snapshot claiming the run failed, with a last_hash that does not match the
    # log at that seq. The engine must fall back to the log, not believe the cache.
    state["status"] = "failed"
    state["last_hash"] = "00" * 32
    store.put_snapshot(run.id, snap_seq - 2, "00" * 32, state | {"last_seq": snap_seq - 2})
    assert eng.get_run(run.id).status is RunStatus.completed


class SnapshotWriteFails(MemoryStore):
    """A store whose snapshot cache is broken; the event log itself is fine."""

    def put_snapshot(self, run_id, seq, last_hash, state):
        raise RuntimeError("disk full (snapshots only)")


def test_a_failing_snapshot_write_never_fails_the_advance(caplog):
    # Review finding F8: `_maybe_snapshot` runs in advance()'s `finally`, after the run's
    # events are committed. A cache-write error must be logged, not raised — otherwise the
    # worker sees a failed advance for a run that progressed, and any exception that ended
    # the advance would be replaced by the snapshot's.
    store = SnapshotWriteFails()
    store.put_agent(Agent(name="g", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()}, lease=store,
                 snapshot_every=1)
    with caplog.at_level("WARNING", logger="agentos.engine"):
        run = eng.start_run("w")
    assert run.status is RunStatus.completed and len(run.steps) == 2
    assert store.get_snapshot(run.id) is None                     # nothing cached
    assert any("snapshot skipped" in r.getMessage() and "disk full" in r.getMessage()
               for r in caplog.records)
    assert eng.get_run(run.id).status is RunStatus.completed     # log still folds


def test_a_snapshot_beyond_the_log_is_ignored_even_with_no_tail():
    # Review finding F2: with nothing after the snapshot there was no chain link to check,
    # so a snapshot whose seq is past the log's tail (events restored from an older backup
    # than run_snapshots) was served as truth. The anchor event itself is now verified.
    store = CountingStore()
    store.put_agent(Agent(name="g", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()}, lease=store,
                 snapshot_every=1)
    run = eng.start_run("w")
    snap_seq, snap_hash, state = store.get_snapshot(run.id)
    assert snap_seq == run.last_seq                                  # taken at the tail

    # 1. A valid anchor with an empty tail IS used, and costs exactly one event read.
    store.reads.clear()
    assert eng.get_run(run.id, hydrate=False).status is RunStatus.completed
    assert store.reads == [1]

    # 2. Same state, claiming a seq beyond the log: nothing to chain to, so previously it
    #    was believed. Now the missing anchor event means the whole log is folded instead.
    forged = state | {"status": "failed", "last_seq": snap_seq + 5}
    store._snapshots[run.id] = (snap_seq + 5, snap_hash, json.dumps(forged))  # bypass monotonic guard on purpose
    store.reads.clear()
    got = eng.get_run(run.id, hydrate=False)
    assert got.status is RunStatus.completed and got.last_seq == run.last_seq
    assert store.reads[-1] == run.last_seq                          # fell back to the full log

    # 3. Right seq, wrong hash at that seq, empty tail: also a stranger.
    store._snapshots[run.id] = (snap_seq, "00" * 32, json.dumps(state | {"status": "failed",
                                                                          "last_hash": "00" * 32}))
    assert eng.get_run(run.id, hydrate=False).status is RunStatus.completed


def test_snapshot_every_zero_disables_snapshots_entirely():
    # Review finding F1: the knob is operator-facing (AGENTOS_SNAPSHOT_EVERY); 0 must mean
    # no snapshot is written AND none is consulted, so every read is the full fold.
    store = CountingStore()
    store.put_agent(Agent(name="g", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    store.put_snapshot("planted", 1, None, {})                    # never read below
    eng = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()}, lease=store,
                 snapshot_every=0)
    run = eng.start_run("w")
    assert run.status is RunStatus.completed
    assert store.get_snapshot(run.id) is None
    store.reads.clear()
    assert eng.get_run(run.id, hydrate=False).status is RunStatus.completed
    assert store.reads == [run.last_seq]                         # one full-log read


def test_snapshot_every_env_is_validated_and_names_the_variable(monkeypatch):
    from dagentos.api.main import snapshot_every_from_env
    monkeypatch.delenv("AGENTOS_SNAPSHOT_EVERY", raising=False)
    assert snapshot_every_from_env() == 200
    monkeypatch.setenv("AGENTOS_SNAPSHOT_EVERY", "0")
    assert snapshot_every_from_env() == 0
    for bad in ("-1", "many", ""):
        monkeypatch.setenv("AGENTOS_SNAPSHOT_EVERY", bad)
        with pytest.raises(RuntimeError, match="AGENTOS_SNAPSHOT_EVERY"):
            snapshot_every_from_env()


def test_concurrent_reads_on_one_store_never_raise(store):
    """UI polish pass (v0.15.0 baseline): the run page's first paint fires GET /runs/{id}, the
    stream's read_events and the badge's GET /approvals together, and the API runs sync store
    calls on a thread pool. The SQLite adapter shares ONE connection (check_same_thread=False)
    and serialises writers under its lock — reads must be serialised too, or two overlapping
    reads raise sqlite3.InterfaceError (SQLITE_MISUSE) and the page shows a 500. Every adapter
    must survive a burst of overlapping reads; only SQLite had the defect."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    run_id = "conc-run"
    store.put_workflow(WorkflowDefinition(name="w", version=3, nodes=[{"id": "s1", "agent": "a"}]))
    written = chain(every_event_type(run_id), 0, None)
    store.append_events(run_id, 0, written)
    n = len(written)
    barrier = threading.Barrier(8)

    def burst(i: int) -> int:
        barrier.wait(timeout=5)
        total = 0
        for _ in range(25):
            total += len(store.read_events(run_id))
            total += len(store.read_events(run_id, after_seq=2, limit=3))
            total += len(store.list_run_ids())
            store.ping(timeout=1.0)
            total += 1 if store.run_id_for_request("nope") is None else 0
        return total

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(burst, range(8)))     # any InterfaceError propagates here
    assert all(r == 25 * (n + 3 + 1 + 1) for r in results)
