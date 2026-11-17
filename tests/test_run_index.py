"""Run index (audit E-07): `runs.status` / `workflow` / `updated_at` are a rebuildable index of
the log, never a second source of state.

- The drift lock: `index_events` equals the fold's status (and pending approvals) at every cut
  point of every golden log, incrementally or in one pass, and on a seeded store the stored row
  equals `fold(run).status` for every run on every adapter.
- The append path keeps the row in step: create, suspend, approve one of two, approve the
  last, complete, reject, retry, cancel, pause, resume — the row follows each command.
- Migration 4 backfills a database whose runs predate it, is atomic (a failure before its
  ledger row leaves it re-runnable), and a row a pre-migration-4 process left behind its log
  is caught up on the next append and on the next start.
- `index_events` never raises: it runs inside every append.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from dagentos.core.engine import Engine
from dagentos.core.events import (
    ApprovalGranted,
    ApprovalRequested,
    RunCompleted,
    RunSuspended,
    StepProgress,
    from_record,
)
from dagentos.core.fold import fold
from dagentos.core.integrity import chain
from dagentos.core.models import (
    Agent,
    AgentType,
    ApprovalStatus,
    EffectClass,
    RunStatus,
    WorkflowDefinition,
)
from dagentos.core.run_index import RunIndex, decode_cursor, encode_cursor, index_events
from dagentos.store import migrations
from dagentos.store.memory import MemoryStore
from dagentos.store.sqlite import SCHEMA as SQLITE_SCHEMA
from dagentos.store.sqlite import SqliteStore
from tests.test_approvals import HUMAN, RecordingExecutor
from tests.test_store_typed_and_bounded import every_event_type

GOLDEN = sorted((Path(__file__).resolve().parent / "golden").glob("*.json"))


def _pending(run) -> tuple[str, ...]:
    return tuple(sorted(a.approval_id for a in run.approvals.values()
                        if a.status is ApprovalStatus.pending))


# ------------------------------------------------------------------ pure: the drift lock

@pytest.mark.parametrize("path", GOLDEN, ids=[p.stem for p in GOLDEN])
def test_index_equals_the_fold_at_every_cut_point_of_every_golden_log(path):
    events = [from_record(r) for r in json.loads(path.read_text())["events"]]
    for k in range(1, len(events) + 1):
        run, idx = fold(events[:k]), index_events(None, events[:k])
        assert idx.status is run.status, f"{path.stem} at seq {k}"
        assert idx.pending_approvals == _pending(run), f"{path.stem} at seq {k}"
        assert idx.indexed_seq == events[k - 1].seq
        assert idx.updated_at == events[k - 1].occurred_at
        half = k // 2                    # the append path continues from a stored row
        assert index_events(index_events(None, events[:half]), events[half:k]) == idx


def test_index_equals_the_fold_at_every_cut_point_of_every_seeded_log():
    """The golden corpus only holds completed runs; the seed reaches every status transition
    the index mirrors, so the same cut-point lock runs over its logs too."""
    mem = MemoryStore()
    seed(mem)
    logs = [mem.read_events(rid) for rid in mem.list_run_ids()]
    seen = {e.event_type for log in logs for e in log}
    assert {"run.completed", "run.failed", "run.cancelled", "run.paused", "run.resumed",
            "run.suspended", "approval.requested", "approval.granted", "approval.rejected",
            "step.retry_requested"} <= seen
    for events in logs:
        for k in range(1, len(events) + 1):
            run, idx = fold(events[:k]), index_events(None, events[:k])
            assert idx.status is run.status, f"{events[0].workflow} at seq {k}"
            assert idx.pending_approvals == _pending(run), f"{events[0].workflow} at seq {k}"
            for j in range(k):
                assert index_events(index_events(None, events[:j]), events[j:k]) == idx


def test_a_grant_resumes_a_suspended_run_only_when_it_was_the_last_pending_one():
    evs = chain([e for e in every_event_type("r") if e.event_type in (
        "run.started", "approval.requested", "run.suspended")], 0, None)
    two = evs[1].model_copy(update={"approval_id": "ap9", "seq": 4})
    idx = index_events(None, [*evs, two])
    assert idx.status is RunStatus.suspended and idx.pending_approvals == ("ap1", "ap9")
    grant = ApprovalGranted(run_id="r", approval_id="ap1", step_id="s4", principal=HUMAN, seq=5)
    assert index_events(idx, [grant]).status is RunStatus.suspended          # ap9 still waits
    last = grant.model_copy(update={"approval_id": "ap9", "seq": 6})
    assert index_events(index_events(idx, [grant]), [last]).status is RunStatus.running


def test_index_events_never_raises_whatever_the_order():
    """It runs inside every append; an exception would fail the write."""
    evs = every_event_type("r")
    rng = random.Random(7)
    for _ in range(200):
        rng.shuffle(evs)
        index_events(None, evs)
        index_events(RunIndex(status=RunStatus.suspended, pending_approvals=("x",)), evs)
    # a decision for an approval the row never saw changes nothing it should not
    stray = ApprovalGranted(run_id="r", approval_id="nope", step_id="s", principal=HUMAN, seq=2)
    assert index_events(RunIndex(status=RunStatus.completed), [stray]).status is RunStatus.completed


def test_cursor_round_trips_and_refuses_what_it_did_not_issue():
    c = encode_cursor("2026-11-17T10:00:00.000001Z", "abc")
    assert decode_cursor(c) == ("2026-11-17T10:00:00.000001Z", "abc")
    for bad in ("", "!!!", "e30", "WzEsMl0", "bm90IGpzb24"):   # '', junk, {}, [1,2], 'not json'
        with pytest.raises(ValueError, match="invalid cursor"):
            decode_cursor(bad)


# --------------------------------------------- seeded store: the append path keeps the row

@pytest.fixture(params=["memory", "sqlite-file"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
        return
    s = SqliteStore(tmp_path / "idx.db")
    yield s
    s.close()


def _engine(store):
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_agent(Agent(name="model", type=AgentType.llm))          # no executor: fails
    store.put_workflow(WorkflowDefinition(name="ok", nodes=[{"id": "n1", "agent": "calc"}]))
    store.put_workflow(WorkflowDefinition(name="gated", nodes=[
        {"id": "n1", "agent": "calc"},
        {"id": "pay1", "agent": "payer", "depends_on": ["n1"]},
        {"id": "pay2", "agent": "payer", "depends_on": ["n1"]},
        {"id": "n4", "agent": "calc", "depends_on": ["pay1", "pay2"]},
    ]))
    store.put_workflow(WorkflowDefinition(name="broken", nodes=[{"id": "x", "agent": "model"}]))
    return Engine(store=store, blobs=store, executors={"echo": RecordingExecutor()}, lease=store)


def _row(store, run_id):
    (row,) = [s for s in store.list_runs(limit=None).runs if s.run_id == run_id]
    return row


def _follows(store, eng, run_id, expected: RunStatus) -> None:
    run = eng.get_run(run_id, hydrate=False)
    assert run.status is expected
    row = _row(store, run_id)
    assert row.status is run.status, f"index says {row.status}, fold says {run.status}"
    assert row.updated_at == store.read_events(run_id)[-1].occurred_at


def _by_step(run, step):
    (a,) = [a for a in run.approvals.values()
            if a.step_id == step and a.status is ApprovalStatus.pending]
    return a.approval_id


def seed(store):
    """Every status the engine can reach, with the row checked after each command."""
    eng = _engine(store)
    ok = eng.create_run("ok")
    _follows(store, eng, ok, RunStatus.running)
    eng.advance(ok)
    _follows(store, eng, ok, RunStatus.completed)

    gated = eng.create_run("gated")
    eng.advance(gated)
    _follows(store, eng, gated, RunStatus.suspended)
    run = eng.get_run(gated, hydrate=False)
    assert len(_pending(run)) == 2
    eng.approve(gated, _by_step(run, "pay1"), principal=HUMAN)
    _follows(store, eng, gated, RunStatus.suspended)                  # pay2 still waits
    eng.approve(gated, _by_step(run, "pay2"), principal=HUMAN)
    _follows(store, eng, gated, RunStatus.running)
    eng.advance(gated)
    _follows(store, eng, gated, RunStatus.completed)

    rejected = eng.create_run("gated")
    eng.advance(rejected)
    run = eng.get_run(rejected, hydrate=False)
    eng.reject(rejected, _by_step(run, "pay1"), principal=HUMAN, reason="no")
    _follows(store, eng, rejected, RunStatus.failed)
    eng.request_retry(rejected, "pay1", principal=HUMAN)
    _follows(store, eng, rejected, RunStatus.running)
    eng.advance(rejected)
    _follows(store, eng, rejected, RunStatus.suspended)

    broken = eng.create_run("broken")
    eng.advance(broken)
    _follows(store, eng, broken, RunStatus.failed)

    cancelled = eng.create_run("ok")
    eng.request_cancel(cancelled, principal=HUMAN)
    _follows(store, eng, cancelled, RunStatus.cancelled)

    paused = eng.create_run("ok")
    eng.request_pause(paused, principal=HUMAN)
    _follows(store, eng, paused, RunStatus.paused)
    resumed = eng.create_run("ok")
    eng.request_pause(resumed, principal=HUMAN)
    eng.resume(resumed, principal=HUMAN)
    _follows(store, eng, resumed, RunStatus.running)
    return eng


def _drift(store) -> dict[str, tuple]:
    rows = {s.run_id: s.status for s in store.list_runs(limit=None).runs}
    folded = {rid: fold(store.read_events(rid)).status for rid in store.list_run_ids()}
    assert rows.keys() == folded.keys()
    return {rid: (rows[rid], folded[rid]) for rid in rows if rows[rid] is not folded[rid]}


def test_the_append_path_keeps_the_row_in_step_and_no_run_drifts(store):
    seed(store)
    assert _drift(store) == {}
    assert len(store.list_run_ids()) == 7


def test_the_row_survives_a_reopen(tmp_path):
    s = SqliteStore(tmp_path / "re.db")
    seed(s)
    before = {r.run_id: r for r in s.list_runs(limit=None).runs}
    s.close()
    s = SqliteStore(tmp_path / "re.db")
    try:
        assert {r.run_id: r for r in s.list_runs(limit=None).runs} == before
        assert _drift(s) == {}
    finally:
        s.close()


# --------------------------------------------------------------- migration 4 (SQLite)

def _v3_database(path: Path, logs: dict[str, list[dict]]) -> None:
    """A database as v0.17.1 left it: migrations 1-3 applied, runs with no index columns."""
    raw = sqlite3.connect(path)
    raw.executescript(SQLITE_SCHEMA)
    raw.executescript(migrations.LEDGER["sqlite"])
    for version, desc, by in migrations.MIGRATIONS:
        if version in (2, 3):
            raw.executescript(by["sqlite"])
        if version <= 3:
            raw.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, desc, "then"))
    for run_id, records in logs.items():
        first = records[0]
        raw.execute("INSERT INTO runs(run_id, request_id, workflow, created_at) VALUES (?,?,?,?)",
                    (run_id, first["request_id"], first["workflow"], first["occurred_at"]))
        for r in records:
            raw.execute("INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, r["seq"], r["event_type"], r["schema_version"],
                         r["occurred_at"], r.get("parent_run_id"), json.dumps(r)))
    raw.commit()
    raw.close()


def _logs_from_a_seeded_memory_store() -> dict[str, list[dict]]:
    mem = MemoryStore()
    seed(mem)
    return {rid: [e.to_record() for e in mem.read_events(rid)] for rid in mem.list_run_ids()}


def test_migration_4_backfills_runs_that_predate_it(tmp_path):
    logs = _logs_from_a_seeded_memory_store()
    _v3_database(tmp_path / "old.db", logs)
    s = SqliteStore(tmp_path / "old.db")
    try:
        assert s.schema_version() == migrations.CURRENT_VERSION
        rows = s.list_runs(limit=None).runs
        assert {r.run_id for r in rows} == set(logs)
        assert all(r.status is not None for r in rows)
        assert _drift(s) == {}
        assert s.run_status_counts() == {"completed": 2, "failed": 1, "suspended": 1,
                                         "cancelled": 1, "paused": 1, "running": 1}
    finally:
        s.close()


def test_migration_4_is_atomic_so_a_failure_before_its_ledger_row_can_be_rerun(tmp_path,
                                                                                monkeypatch):
    """SQLite has no ADD COLUMN IF NOT EXISTS: DDL committed without its ledger row would make
    every later start fail with 'duplicate column name'."""
    _v3_database(tmp_path / "old.db", _logs_from_a_seeded_memory_store())
    real = migrations.apply

    def crash_before_recording_4(dialect, baseline, execute_script, applied, record, **kw):
        def record_or_crash(v, d, at):
            if v == 4:
                raise RuntimeError("simulated crash before the ledger row")
            record(v, d, at)
        return real(dialect, baseline, execute_script, applied, record_or_crash, **kw)

    monkeypatch.setattr(migrations, "apply", crash_before_recording_4)
    with pytest.raises(RuntimeError, match="simulated crash"):
        SqliteStore(tmp_path / "old.db")
    monkeypatch.setattr(migrations, "apply", real)
    s = SqliteStore(tmp_path / "old.db")                    # no 'duplicate column name'
    try:
        assert s.schema_version() == 4
        assert _drift(s) == {}
    finally:
        s.close()


def test_a_row_left_behind_its_log_is_caught_up_on_append_and_on_start(tmp_path):
    """A process older than migration 4 (mixed-version upgrade) appends without touching the
    row. The next append through a current store, and the next start, catch it up."""
    def append_like_an_old_binary(s, rid, events):
        for ev in chain(events, len(s.read_events(rid)), s.read_events(rid)[-1].hash):
            rec = ev.to_record()
            s._conn.execute("INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (rid, rec["seq"], rec["event_type"], rec["schema_version"],
                             rec["occurred_at"], None, json.dumps(rec)))

    s = SqliteStore(tmp_path / "mix.db")
    eng = _engine(s)
    done = eng.create_run("ok")
    append_like_an_old_binary(s, done, [RunCompleted(run_id=done)])
    assert _row(s, done).status is RunStatus.running                 # behind its log
    s.close()
    s = SqliteStore(tmp_path / "mix.db")                             # the start catches it up
    try:
        assert _row(s, done).status is RunStatus.completed
        eng = _engine(s)
        held = eng.create_run("ok")
        append_like_an_old_binary(s, held, [
            ApprovalRequested(run_id=held, approval_id="ap", step_id="n1",
                              effect_classes=[EffectClass.spend]),
            RunSuspended(run_id=held)])
        n = len(s.read_events(held))
        s.append_events(held, n, chain([StepProgress(run_id=held, step_id="n1", attempt=1,
                                                     fraction=0.5)], n, s.read_events(held)[-1].hash))
        row = _row(s, held)                                          # the append caught up
        assert row.status is RunStatus.suspended
        assert row.updated_at == s.read_events(held)[-1].occurred_at
        assert _drift(s) == {}
    finally:
        s.close()


def test_nulling_indexed_seq_rebuilds_every_row_on_the_next_start(tmp_path):
    """The rebuild FAIL_MODES documents for a drifted index: `UPDATE runs SET indexed_seq =
    NULL`, then restart. Even a row whose status was hand-edited comes back equal to the fold."""
    s = SqliteStore(tmp_path / "rebuild.db")
    seed(s)
    s._conn.execute("UPDATE runs SET status = 'completed', pending_approvals = 'not json'")
    assert _drift(s) != {}
    s._conn.execute("UPDATE runs SET indexed_seq = NULL")
    s.close()
    s = SqliteStore(tmp_path / "rebuild.db")
    try:
        assert _drift(s) == {}
    finally:
        s.close()


def test_opening_a_current_database_takes_no_write_lock(tmp_path):
    """Migrations run in one write transaction, but a database already at the latest version
    is only read on open: an API starting while a worker is mid-append must not wait for the
    writer (or fail with 'database is locked' after the busy timeout)."""
    SqliteStore(tmp_path / "cur.db").close()
    writer = sqlite3.connect(tmp_path / "cur.db", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")                         # another process mid-append
    try:
        s = SqliteStore(tmp_path / "cur.db")
        assert s.schema_version() == migrations.CURRENT_VERSION
        s.close()
    finally:
        writer.execute("ROLLBACK")
        writer.close()
