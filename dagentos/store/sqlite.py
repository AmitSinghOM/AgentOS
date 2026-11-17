"""SQLite Store + BlobStore on the standard library.

Why SQLite first (docs/DEVELOPMENT_STRUCTURE.md §7): `pip install dagentos` must work with
no infrastructure. Postgres is the production adapter and passes the same contract suite.

Physical guarantees this adapter relies on:
- `PRIMARY KEY (run_id, seq)` on run_events — a duplicate append is rejected by the
  database, not by application logic (C2).
- Every `append_events` is one transaction: the expected_seq check and all inserts
  commit together or not at all.
- `runs.request_id UNIQUE` — run start is idempotent even under concurrent clients.
- WAL journal mode so readers never block the single writer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dagentos.core.coordination import LeaseToken
from dagentos.core.events import Event, RunStarted, SchedulerState, from_record
from dagentos.core.models import Agent, BlobRef, Principal, RunStatus, WorkflowDefinition
from dagentos.core.ports import ConflictError
from dagentos.core.run_index import (
    RunIndex,
    RunPage,
    RunSummary,
    decode_cursor,
    encode_cursor,
    index_events,
)
from dagentos.store import migrations

_log = logging.getLogger("agentos.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_versions (
    name    TEXT    NOT NULL,
    version INTEGER NOT NULL,
    body    TEXT    NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    workflow   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    max_fence  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leases (
    run_id     TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    fence      INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    run_id     TEXT PRIMARY KEY,
    visible_at REAL NOT NULL,
    deliveries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id         TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    event_type     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at    TEXT    NOT NULL,
    parent_run_id  TEXT,
    record         TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blobs (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    data       BLOB NOT NULL
);
"""


def _create_owner_only(path: str) -> None:
    """Create the database file 0600 before SQLite does it with the process umask (audit I-15):
    the file holds run inputs, tool outputs and stderr tails. Only a plain path that does not
    exist yet is touched — ':memory:' and 'file:' URIs are left to SQLite, and an existing file
    keeps the mode its operator gave it. SQLite copies the main file's mode onto the `-wal` and
    `-shm` sidecars it creates, so they inherit 0600 (os_unix.c findCreateFileMode)."""
    if path == ":memory:" or path.startswith("file:") or os.path.exists(path):
        return
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))


def _status(value: str | None) -> RunStatus | None:
    try:
        return RunStatus(value) if value is not None else None
    except ValueError:                     # a hand-edited row lists as unindexed, never a 500
        return None


def _decode_index(row: tuple) -> RunIndex | None:
    """(status, pending_approvals, indexed_seq, updated_at) → RunIndex, or None when the row is
    not indexed yet or unreadable — the caller then rebuilds it from the whole log."""
    status = _status(row[0])
    if status is None or row[2] is None:
        return None
    try:
        pending = json.loads(row[1])
        updated = datetime.fromisoformat(row[3]) if row[3] else None
    except (TypeError, ValueError):
        return None
    if not isinstance(pending, list) or not all(isinstance(p, str) for p in pending):
        return None
    return RunIndex(status=status, pending_approvals=tuple(pending), indexed_seq=int(row[2]),
                    updated_at=updated)


def _run_filter(status: Sequence[RunStatus] | None, workflow: str | None) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if status:
        where.append(f"status IN ({', '.join('?' for _ in status)})")
        params += [s.value for s in status]
    if workflow is not None:
        where.append("workflow = ?")
        params.append(workflow)
    return where, params


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        _create_owner_only(self._path)
        # One connection, one lock: SQLite serializes writers anyway, and a single
        # connection keeps ":memory:" databases coherent across calls.
        self._conn = sqlite3.connect(self._path, check_same_thread=False,
                                     isolation_level=None)  # autocommit; explicit BEGIN
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # The lock exists before the first migrate(): migrate() is public and idempotent, so it
        # can be re-run on a live store, and it must serialise with reads like every statement.
        self._lock = threading.RLock()
        self.migrate()

    # Every statement goes through the lock — reads too. `check_same_thread=False` lets the API's
    # thread pool share the connection, and sqlite3 connections are not safe for two overlapping
    # statements: a read racing another read raises InterfaceError (SQLITE_MISUSE) or hands one
    # caller the other's rows (seen as JSONDecodeError on a record). Writers always held the lock;
    # reads bypassed it until the v0.15.0 UI polish pass surfaced a 500 on the run page's first paint.
    def _one(self, sql: str, params: tuple = ()) -> Any:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[Any]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def migrate(self) -> list[int]:
        """Apply pending schema migrations (dagentos/store/migrations.py); idempotent.

        Held under the lock end to end: the version SELECT, each script and each version
        INSERT are statements on the shared connection, and a re-run overlapping a locked
        read otherwise gets that read's rows (four-seat review v0.16.0, A3).

        One IMMEDIATE transaction around the ledger read, every pending script and its ledger
        row: SQLite's `ALTER TABLE ... ADD COLUMN` (migration 4) has no IF NOT EXISTS, so DDL
        committed without its ledger row could never be re-run, and an API and a worker
        starting together on one file must not both apply it. Scripts run statement by
        statement because `executescript` commits the open transaction. Then every run whose
        index row is behind its log is caught up (the migration-4 backfill, see run_index).
        A database already at the latest version is only read: no write lock at open."""
        with self._lock:
            if not self._migrations_pending():
                applied: list[int] = []
            else:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    applied = migrations.apply(
                        "sqlite", SCHEMA, self._script,
                        lambda: {r[0] for r in self._conn.execute(
                            "SELECT version FROM schema_migrations").fetchall()},
                        lambda v, d, at: self._conn.execute(
                            "INSERT OR IGNORE INTO schema_migrations VALUES (?, ?, ?)", (v, d, at)))
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
        self._catch_up_index()
        return applied

    def _migrations_pending(self) -> bool:
        """Read-only pre-check; `apply` re-reads the ledger inside the write transaction."""
        try:
            done = {r[0] for r in self._conn.execute("SELECT version FROM schema_migrations")}
        except sqlite3.OperationalError:          # no ledger yet
            return True
        return any(v not in done for v, _d, _by in migrations.MIGRATIONS)

    def _script(self, sql: str) -> None:
        """Run a multi-statement script inside the caller's transaction."""
        stmt = ""
        for line in sql.splitlines(keepends=True):
            stmt += line
            if sqlite3.complete_statement(stmt):
                self._conn.execute(stmt)
                stmt = ""
        if stmt.strip():
            self._conn.execute(stmt)

    # run index (E-07): a rebuildable index of each run's log, see dagentos.core.run_index
    def _write_index(self, run_id: str, last_seq: int, new: Sequence[Event] = ()) -> None:
        """Inside the caller's transaction: bring the run's row from what it reflects up to
        `last_seq` (reading any events a pre-migration-4 process appended behind it), then
        through `new`. No row (events for a run that never started) is left alone."""
        row = self._conn.execute(
            "SELECT status, pending_approvals, indexed_seq, updated_at FROM runs WHERE run_id = ?",
            (run_id,)).fetchone()
        if row is None:
            return
        prev = _decode_index(row)
        seen = prev.indexed_seq if prev is not None else 0
        behind = [] if seen == last_seq else [from_record(json.loads(r[0])) for r in self._conn.execute(
            "SELECT record FROM run_events WHERE run_id = ? AND seq > ? AND seq <= ? ORDER BY seq",
            (run_id, seen, last_seq)).fetchall()]
        idx = index_events(prev, [*behind, *new])
        self._conn.execute(
            "UPDATE runs SET status = ?, pending_approvals = ?, indexed_seq = ?, updated_at = ? "
            "WHERE run_id = ?",
            (idx.status.value, json.dumps(list(idx.pending_approvals)), idx.indexed_seq,
             idx.updated_at.isoformat() if idx.updated_at else None, run_id))

    def _catch_up_index(self) -> None:
        stale = self._all(
            "SELECT r.run_id FROM runs r WHERE r.indexed_seq IS NULL OR r.indexed_seq < "
            "(SELECT COALESCE(MAX(seq), 0) FROM run_events e WHERE e.run_id = r.run_id)")
        for (run_id,) in stale:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    (last,) = self._conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    self._write_index(run_id, last)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
        if stale:
            _log.info("run index: caught up %d run(s) behind their log", len(stale))

    def list_runs(self, *, status: Sequence[RunStatus] | None = None,
                  workflow: str | None = None, cursor: str | None = None,
                  limit: int | None = 50) -> RunPage:
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1 or None")
        if status is not None and not status:
            return RunPage()
        where, params = _run_filter(status, workflow)
        if cursor is not None:
            key, after = decode_cursor(cursor)
            where.append("(created_at, run_id) < (?, ?)")
            params += [key, after]
        rows = self._all(
            "SELECT run_id, workflow, status, created_at, updated_at FROM runs"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY created_at DESC, run_id DESC LIMIT ?",
            (*params, -1 if limit is None else limit + 1))
        more = limit is not None and len(rows) > limit
        rows = rows[:limit] if more else rows
        return RunPage(
            runs=[RunSummary(run_id=r[0], workflow=r[1], status=_status(r[2]),
                             created_at=datetime.fromisoformat(r[3]),
                             updated_at=datetime.fromisoformat(r[4]) if r[4] else None)
                  for r in rows],
            next_cursor=encode_cursor(rows[-1][3], rows[-1][0]) if more else None)

    def run_status_counts(self, *, workflow: str | None = None) -> dict[str, int]:
        where, params = _run_filter(None, workflow)
        rows = self._all("SELECT status, COUNT(*) FROM runs WHERE status IS NOT NULL"
                         + "".join(f" AND {w}" for w in where) + " GROUP BY status", tuple(params))
        return {r[0]: int(r[1]) for r in rows}

    def schema_version(self) -> int:
        (v,) = self._one("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        return int(v)

    # snapshots (C15): a bounded optimization of the fold, never the source of truth.
    # Monotonic per run: a stale worker (lease lost) also reaches advance()'s finally and
    # snapshots from the store; an older seq must not replace the live worker's newer one.
    def put_snapshot(self, run_id: str, seq: int, last_hash: str | None, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET seq = excluded.seq, "
                "last_hash = excluded.last_hash, state = excluded.state, "
                "taken_at = excluded.taken_at WHERE excluded.seq > run_snapshots.seq",
                (run_id, seq, last_hash, json.dumps(state, sort_keys=True, default=str),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

    def get_snapshot(self, run_id: str) -> tuple[int, str | None, dict] | None:
        row = self._one("SELECT seq, last_hash, state FROM run_snapshots WHERE run_id = ?", (run_id,))
        return (int(row[0]), row[1], json.loads(row[2])) if row else None

    # scheduler-wide pause (A12): append-only history, newest row is the current state
    def get_scheduler_state(self) -> SchedulerState:
        row = self._one("SELECT paused, principal, reason, changed_at FROM scheduler_state "
                        "ORDER BY id DESC LIMIT 1")
        if row is None:
            return SchedulerState()
        return SchedulerState(paused=bool(row[0]),
                              principal=Principal.model_validate_json(row[1]) if row[1] else None,
                              reason=row[2], changed_at=row[3])

    def set_scheduler_state(self, state: SchedulerState) -> None:
        at = state.changed_at or datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                "INSERT INTO scheduler_state(paused, principal, reason, changed_at) "
                "VALUES (?, ?, ?, ?)",
                (int(state.paused), state.principal.model_dump_json() if state.principal else None,
                 state.reason, at.isoformat()))

    # definitions (agents immutable per (name, version))
    def put_agent(self, agent: Agent) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM agent_versions WHERE name = ? AND version = ?",
                (agent.name, agent.version),
            ).fetchone()
            if row is not None:
                if Agent.model_validate_json(row[0]) != agent:
                    raise ConflictError(f"agent {agent.name!r} v{agent.version} already exists "
                                        f"with a different definition; bump the version")
                return
            self._conn.execute(
                "INSERT INTO agent_versions(name, version, body) VALUES (?, ?, ?)",
                (agent.name, agent.version, agent.model_dump_json()),
            )

    def get_agent(self, name: str, version: int | None = None) -> Agent | None:
        if version is None:
            row = self._one(
                "SELECT body FROM agent_versions WHERE name = ? ORDER BY version DESC LIMIT 1",
                (name,),
            )
        else:
            row = self._one(
                "SELECT body FROM agent_versions WHERE name = ? AND version = ?", (name, version),
            )
        return Agent.model_validate_json(row[0]) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._all(
            "SELECT a.body FROM agent_versions a JOIN (SELECT name, MAX(version) v "
            "FROM agent_versions GROUP BY name) m ON a.name = m.name AND a.version = m.v "
            "ORDER BY a.name"
        )
        return [Agent.model_validate_json(r[0]) for r in rows]

    def list_agent_versions(self, name: str) -> list[int]:
        rows = self._all("SELECT version FROM agent_versions WHERE name = ? ORDER BY version", (name,))
        return [r[0] for r in rows]

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflows(name, body) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET body = excluded.body",
                (wf.name, wf.model_dump_json()),
            )

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        row = self._one("SELECT body FROM workflows WHERE name = ?", (name,))
        return WorkflowDefinition.model_validate_json(row[0]) if row else None

    # run log
    def append_events(self, run_id: str, expected_seq: int,
                      events: Sequence[Event], *, fence: int | None = None) -> list[Event]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if fence is not None:                          # fence first (see MemoryStore)
                    row = self._conn.execute(
                        "SELECT max_fence FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    seen = row[0] if row else 0
                    if fence < seen:
                        raise ConflictError(f"run {run_id!r}: fence {fence} is stale "
                                            f"(highest seen {seen})")
                (current,) = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
                ).fetchone()
                if current != expected_seq:
                    raise ConflictError(
                        f"run {run_id!r}: expected seq {expected_seq}, log is at {current}"
                    )
                out: list[Event] = []
                for i, ev in enumerate(events, start=expected_seq + 1):
                    stamped = ev.model_copy(update={"seq": i})
                    rec = stamped.to_record()
                    if isinstance(stamped, RunStarted):
                        try:
                            self._conn.execute(
                                "INSERT INTO runs(run_id, request_id, workflow, created_at) "
                                "VALUES (?, ?, ?, ?)",
                                (run_id, stamped.request_id, stamped.workflow,
                                 rec["occurred_at"]),
                            )
                        except sqlite3.IntegrityError as exc:
                            raise ConflictError(
                                f"request_id {stamped.request_id!r} already started"
                            ) from exc
                    try:
                        self._conn.execute(
                            "INSERT INTO run_events(run_id, seq, event_type, schema_version, "
                            "occurred_at, parent_run_id, record) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, i, rec["event_type"], rec["schema_version"],
                             rec["occurred_at"], rec.get("parent_run_id"), json.dumps(rec)),
                        )
                    except sqlite3.IntegrityError as exc:  # PRIMARY KEY(run_id, seq)
                        raise ConflictError(f"run {run_id!r}: seq {i} already exists") from exc
                    out.append(stamped)
                # The index row moves in this transaction, so it never disagrees with the fold
                # at a commit boundary (E-07); a conflict above rolls it back with the events.
                self._write_index(run_id, expected_seq, out)
                if fence is not None:
                    self._conn.execute(
                        "UPDATE runs SET max_fence = ? WHERE run_id = ?", (fence, run_id)
                    )
                self._conn.execute("COMMIT")
                return out
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def read_events(self, run_id: str, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        rows = self._all(
            "SELECT record FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after_seq, -1 if limit is None else limit),
        )
        return [from_record(json.loads(r[0])) for r in rows]

    def ping(self, timeout: float) -> None:
        """One statement against the file, behind the connection lock like every read. `timeout`
        is not separately enforceable here: the call waits for any in-flight statement on this
        connection, then a locked file fails after the connection's busy timeout (sqlite3's
        default 5 s) — the probe window must allow for both."""
        self._one("SELECT 1")

    def list_run_ids(self) -> list[str]:
        rows = self._all("SELECT run_id FROM runs ORDER BY created_at")
        return [r[0] for r in rows]

    def run_id_for_request(self, request_id: str) -> str | None:
        row = self._one("SELECT run_id FROM runs WHERE request_id = ?", (request_id,))
        return row[0] if row else None

    # archive (E-08): every row of one run in one transaction, see Store.purge_run
    def purge_run(self, run_id: str, *, expected_seq: int,
                  blobs: Sequence[str] = ()) -> list[str]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                (current,) = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
                ).fetchone()
                if current == 0 or current != expected_seq:
                    raise ConflictError(f"run {run_id!r}: expected seq {expected_seq}, log is "
                                        f"at {current}; not purged")
                for table in ("run_events", "run_snapshots", "queue", "leases", "runs"):
                    self._conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
                removed: list[str] = []
                for sha in dict.fromkeys(blobs):          # once each, caller's order
                    referenced = self._conn.execute(
                        "SELECT 1 FROM run_events WHERE instr(record, ?) > 0 LIMIT 1", (sha,)
                    ).fetchone()
                    if referenced is None and self._conn.execute(
                            "DELETE FROM blobs WHERE sha256 = ?", (sha,)).rowcount:
                        removed.append(sha)
                self._conn.execute("COMMIT")
                return removed
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # blobs
    def put(self, data: bytes, media_type: str = "application/json") -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO blobs(sha256, size, media_type, data) VALUES (?, ?, ?, ?)",
                (digest, len(data), media_type, sqlite3.Binary(data)),
            )
        return BlobRef(sha256=digest, size=len(data), media_type=media_type)

    def get(self, ref: BlobRef) -> bytes:
        row = self._one("SELECT data FROM blobs WHERE sha256 = ?", (ref.sha256,))
        if row is None:
            raise KeyError(f"blob {ref.sha256[:12]}… not found")
        return bytes(row[0])

    def exists(self, ref: BlobRef) -> bool:
        return self._one("SELECT 1 FROM blobs WHERE sha256 = ?", (ref.sha256,)) is not None

    # lease (wall clock, so a lease outlives the process that took it)
    def acquire(self, run_id: str, holder: str, ttl_seconds: float) -> LeaseToken | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = self._conn.execute(
                    "SELECT holder, fence, expires_at FROM leases WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is not None and row[2] > now and row[0] != holder:
                    self._conn.execute("COMMIT")
                    return None
                fence = (row[1] if row else 0) + 1
                self._conn.execute(
                    "INSERT INTO leases(run_id, holder, fence, expires_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET holder = excluded.holder, "
                    "fence = excluded.fence, expires_at = excluded.expires_at",
                    (run_id, holder, fence, now + ttl_seconds),
                )
                # Record the fence at acquire time (see MemoryStore.acquire). The run row
                # may not exist yet for a brand-new run; that is fine — the first append
                # creates it and carries the fence.
                self._conn.execute(
                    "UPDATE runs SET max_fence = MAX(max_fence, ?) WHERE run_id = ?",
                    (fence, run_id),
                )
                self._conn.execute("COMMIT")
                return LeaseToken(run_id=run_id, holder=holder, fence=fence)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def renew(self, token: LeaseToken, ttl_seconds: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE leases SET expires_at = ? WHERE run_id = ? AND holder = ? "
                "AND fence = ? AND expires_at > ?",
                (time.time() + ttl_seconds, token.run_id, token.holder, token.fence, time.time()),
            )
            return cur.rowcount == 1

    def release(self, token: LeaseToken) -> None:
        # Expire, never delete: the row carries the fence counter, which must stay
        # monotonic for the life of the run.
        with self._lock:
            self._conn.execute(
                "UPDATE leases SET expires_at = 0 WHERE run_id = ? AND holder = ? AND fence = ?",
                (token.run_id, token.holder, token.fence),
            )

    # queue (at-least-once; visibility timeout redelivers un-acked runs)
    visibility_seconds = 30.0

    def push(self, run_id: str, *, delay_seconds: float = 0.0) -> None:
        """Enqueue, or make an in-flight delivery visible again. Re-pushing a run that
        someone is currently processing is harmless: the lease refuses the second
        worker and the delivery is retried later. `delay_seconds` defers visibility."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO queue(run_id, visible_at) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET visible_at = excluded.visible_at",
                (run_id, time.time() + max(0.0, delay_seconds)),
            )

    def pull(self, timeout: float) -> str | None:
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    now = time.time()
                    row = self._conn.execute(
                        "SELECT run_id FROM queue WHERE visible_at <= ? "
                        "ORDER BY visible_at LIMIT 1", (now,)
                    ).fetchone()
                    if row is not None:
                        self._conn.execute(
                            "UPDATE queue SET visible_at = ?, deliveries = deliveries + 1 "
                            "WHERE run_id = ?", (now + self.visibility_seconds, row[0]),
                        )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            if row is not None:
                return row[0]
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def ack(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM queue WHERE run_id = ?", (run_id,))

    def queue_depth(self) -> int:
        (n,) = self._one("SELECT COUNT(*) FROM queue")
        return int(n)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
