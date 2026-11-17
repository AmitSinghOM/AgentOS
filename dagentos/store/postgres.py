"""PostgreSQL Store + BlobStore on psycopg 3.

The production adapter. Same contract as SQLite (tests/contract/), same physical
guarantees, plus the primitive the worker slice needs: `pg_try_advisory_xact_lock` /
session advisory locks for per-run leases live in `dagentos/store/pg_lease.py`.

- `PRIMARY KEY (run_id, seq)` rejects a duplicate append at the database (C2).
- Each `append_events` is one transaction: `SELECT ... FOR UPDATE` on the run row
  serializes concurrent appenders so exactly one sees the expected seq.
- `runs.request_id UNIQUE` makes run start idempotent under concurrency.

Schema management: numbered, forward-only, raw-SQL migrations in `dagentos/store/migrations.py`,
recorded in `schema_migrations` and applied on startup (`migrate()`), idempotent so a
database created before the ledger adopts it silently. Chosen over Alembic on purpose —
see that module's docstring.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import psycopg
from psycopg import errors
from psycopg_pool import ConnectionPool

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_versions (
    name    TEXT    NOT NULL,
    version INTEGER NOT NULL,
    body    JSONB   NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    body JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    workflow   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_seq   INTEGER NOT NULL DEFAULT 0,
    max_fence  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leases (
    run_id     TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    fence      INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    run_id     TEXT PRIMARY KEY,
    visible_at TIMESTAMPTZ NOT NULL,
    deliveries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id         TEXT    NOT NULL REFERENCES runs(run_id),
    seq            INTEGER NOT NULL,
    event_type     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    parent_run_id  TEXT,
    record         JSONB   NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS blobs (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    data       BYTEA NOT NULL
);
"""

_log = logging.getLogger("agentos.store")


def _status(value: str | None) -> RunStatus | None:
    try:
        return RunStatus(value) if value is not None else None
    except ValueError:                     # a hand-edited row lists as unindexed, never a 500
        return None


def _decode_index(row) -> RunIndex | None:
    """(status, pending_approvals, indexed_seq, updated_at) → RunIndex, or None when the row is
    not indexed yet or unreadable — the caller then rebuilds it from the whole log."""
    status, pending = _status(row[0]), row[1]
    if status is None or row[2] is None or not isinstance(pending, list) \
            or not all(isinstance(p, str) for p in pending):
        return None
    return RunIndex(status=status, pending_approvals=tuple(pending), indexed_seq=int(row[2]),
                    updated_at=row[3])


def _behind(conn, run_id: str, prev: RunIndex | None, upto: int) -> list[Event]:
    """Events a row has not seen yet, up to `upto` (written by a pre-migration-4 process)."""
    seen = prev.indexed_seq if prev is not None else 0
    if seen >= upto:
        return []
    rows = conn.execute("SELECT record FROM run_events WHERE run_id = %s AND seq > %s "
                        "AND seq <= %s ORDER BY seq", (run_id, seen, upto)).fetchall()
    return [from_record(r[0]) for r in rows]


def _run_filter(status: Sequence[RunStatus] | None, workflow: str | None) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if status:
        where.append("status = ANY(%s)")
        params.append([s.value for s in status])
    if workflow is not None:
        where.append("workflow = %s")
        params.append(workflow)
    return where, params


class PostgresStore:
    def __init__(self, dsn: str, *, schema: str | None = None, min_size: int = 1,
                 max_size: int = 4) -> None:
        """`schema` (optional) isolates AgentOS tables in their own Postgres schema —
        the configurability LangGraph users asked for in #7345."""
        self._schema = schema
        opts = {"options": f"-c search_path={schema}"} if schema else {}
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                    kwargs=opts, open=True)
        self.migrate()

    def migrate(self) -> list[int]:
        """Apply pending schema migrations (dagentos/store/migrations.py); idempotent. All of
        them run in this connection's one transaction. Then every run whose index row is behind
        its log is caught up (the migration-4 backfill, see run_index)."""
        with self._pool.connection() as conn:
            if self._schema:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            applied = migrations.apply(
                "postgres", SCHEMA, conn.execute,
                lambda: {r[0] for r in conn.execute(
                    "SELECT version FROM schema_migrations").fetchall()},
                lambda v, d, at: conn.execute(
                    "INSERT INTO schema_migrations VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (v, d, at)))
        self._catch_up_index()
        return applied

    def schema_version(self) -> int:
        with self._pool.connection() as conn:
            (v,) = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                                ).fetchone()
        return int(v)

    # snapshots (C15): a bounded optimization of the fold, never the source of truth.
    # Monotonic per run (see SqliteStore.put_snapshot).
    def put_snapshot(self, run_id: str, seq: int, last_hash: str | None, state: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO run_snapshots (run_id, seq, last_hash, state, taken_at) "
                "VALUES (%s, %s, %s, %s::jsonb, now()) ON CONFLICT (run_id) DO UPDATE SET "
                "seq = EXCLUDED.seq, last_hash = EXCLUDED.last_hash, state = EXCLUDED.state, "
                "taken_at = EXCLUDED.taken_at WHERE run_snapshots.seq < EXCLUDED.seq",
                (run_id, seq, last_hash, json.dumps(state, sort_keys=True, default=str)))

    def get_snapshot(self, run_id: str) -> tuple[int, str | None, dict] | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT seq, last_hash, state FROM run_snapshots WHERE run_id = %s",
                               (run_id,)).fetchone()
        return (int(row[0]), row[1], row[2]) if row else None

    # scheduler-wide pause (A12): append-only history, newest row is the current state
    def get_scheduler_state(self) -> SchedulerState:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT paused, principal, reason, changed_at FROM scheduler_state "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return SchedulerState()
        return SchedulerState(paused=row[0],
                              principal=Principal.model_validate(row[1]) if row[1] else None,
                              reason=row[2], changed_at=row[3])

    def set_scheduler_state(self, state: SchedulerState) -> None:
        at = state.changed_at or datetime.now(UTC)
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO scheduler_state(paused, principal, reason, changed_at) "
                "VALUES (%s, %s::jsonb, %s, %s)",
                (state.paused, state.principal.model_dump_json() if state.principal else None,
                 state.reason, at))

    # definitions (agents immutable per (name, version))
    def put_agent(self, agent: Agent) -> None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT body FROM agent_versions WHERE name = %s AND version = %s FOR UPDATE",
                (agent.name, agent.version),
            ).fetchone()
            if row is not None:
                if Agent.model_validate(row[0]) != agent:
                    raise ConflictError(f"agent {agent.name!r} v{agent.version} already exists "
                                        f"with a different definition; bump the version")
                return
            try:
                conn.execute(
                    "INSERT INTO agent_versions(name, version, body) VALUES (%s, %s, %s::jsonb)",
                    (agent.name, agent.version, agent.model_dump_json()),
                )
            except errors.UniqueViolation as exc:
                raise ConflictError(f"agent {agent.name!r} v{agent.version} raced another "
                                    f"writer") from exc

    def get_agent(self, name: str, version: int | None = None) -> Agent | None:
        with self._pool.connection() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT body FROM agent_versions WHERE name = %s "
                    "ORDER BY version DESC LIMIT 1", (name,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT body FROM agent_versions WHERE name = %s AND version = %s",
                    (name, version),
                ).fetchone()
        return Agent.model_validate(row[0]) if row else None

    def list_agents(self) -> list[Agent]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (name) body FROM agent_versions "
                "ORDER BY name, version DESC"
            ).fetchall()
        return [Agent.model_validate(r[0]) for r in rows]

    def list_agent_versions(self, name: str) -> list[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT version FROM agent_versions WHERE name = %s ORDER BY version", (name,)
            ).fetchall()
        return [r[0] for r in rows]

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO workflows(name, body) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body",
                (wf.name, wf.model_dump_json()),
            )

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT body FROM workflows WHERE name = %s", (name,)
            ).fetchone()
        return WorkflowDefinition.model_validate(row[0]) if row else None

    # run log
    def append_events(self, run_id: str, expected_seq: int,
                      events: Sequence[Event], *, fence: int | None = None) -> list[Event]:
        with self._pool.connection() as conn, conn.transaction():
            # Serialize appenders on the run row. A brand-new run has no row yet, so
            # concurrent creators race on runs.request_id / PRIMARY KEY instead.
            row = conn.execute(
                "SELECT last_seq, max_fence, status, pending_approvals, indexed_seq, updated_at "
                "FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            current, seen_fence = (row[0], row[1]) if row else (0, 0)
            if fence is not None and fence < seen_fence:   # fence first (see MemoryStore)
                raise ConflictError(f"run {run_id!r}: fence {fence} is stale "
                                    f"(highest seen {seen_fence})")
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
                        conn.execute(
                            "INSERT INTO runs(run_id, request_id, workflow, created_at) "
                            "VALUES (%s, %s, %s, %s)",
                            (run_id, stamped.request_id, stamped.workflow,
                             stamped.occurred_at),
                        )
                    except errors.UniqueViolation as exc:
                        raise ConflictError(
                            f"request_id {stamped.request_id!r} already started"
                        ) from exc
                try:
                    conn.execute(
                        "INSERT INTO run_events(run_id, seq, event_type, schema_version, "
                        "occurred_at, parent_run_id, record) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                        (run_id, i, rec["event_type"], rec["schema_version"],
                         stamped.occurred_at, rec.get("parent_run_id"), json.dumps(rec)),
                    )
                except errors.UniqueViolation as exc:  # PRIMARY KEY(run_id, seq)
                    raise ConflictError(f"run {run_id!r}: seq {i} already exists") from exc
                out.append(stamped)
            # The index row moves in this transaction, so it never disagrees with the fold at
            # a commit boundary (E-07); a conflict above rolls it back with the events.
            prev = _decode_index(row[2:]) if row else None
            idx = index_events(prev, [*_behind(conn, run_id, prev, expected_seq), *out])
            conn.execute(
                "UPDATE runs SET last_seq = %s, max_fence = GREATEST(max_fence, %s), "
                "status = %s, pending_approvals = %s::jsonb, indexed_seq = %s, updated_at = %s "
                "WHERE run_id = %s",
                (expected_seq + len(out), fence if fence is not None else 0, idx.status.value,
                 json.dumps(list(idx.pending_approvals)), idx.indexed_seq, idx.updated_at, run_id),
            )
            return out

    def read_events(self, run_id: str, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT record FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                (run_id, after_seq, limit),        # LIMIT NULL = no limit in PostgreSQL
            ).fetchall()
        return [from_record(r[0]) for r in rows]

    def ping(self, timeout: float) -> None:
        """Bounded acquire + `SELECT 1`. `pool.connection()` alone waits psycopg_pool's default
        30 s for a connection, longer than any readiness window; the probe passes its own."""
        with self._pool.connection(timeout=timeout) as conn:
            conn.execute("SELECT 1").fetchone()

    def list_run_ids(self) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT run_id FROM runs ORDER BY created_at, run_id").fetchall()
        return [r[0] for r in rows]

    # run index (E-07): a rebuildable index of each run's log, see dagentos.core.run_index
    def _catch_up_index(self) -> None:
        """Index every run whose row is behind its log: on the migration-4 upgrade that is
        every run (the backfill); afterwards only runs a pre-migration-4 process appended to.
        One short transaction per run, holding the run row like an append does."""
        with self._pool.connection() as conn:
            stale = conn.execute(
                "SELECT run_id FROM runs WHERE indexed_seq IS DISTINCT FROM last_seq").fetchall()
        for (run_id,) in stale:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "SELECT last_seq, status, pending_approvals, indexed_seq, updated_at "
                    "FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)).fetchone()
                if row is None:                    # runs rows are never deleted; defensive
                    continue
                prev = _decode_index(row[1:])
                idx = index_events(prev, _behind(conn, run_id, prev, row[0]))
                conn.execute(
                    "UPDATE runs SET status = %s, pending_approvals = %s::jsonb, "
                    "indexed_seq = %s, updated_at = %s WHERE run_id = %s",
                    (idx.status.value, json.dumps(list(idx.pending_approvals)), idx.indexed_seq,
                     idx.updated_at, run_id))
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
            where.append("(created_at, run_id) < (%s::timestamptz, %s)")
            params += [key, after]
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT run_id, workflow, status, created_at, updated_at FROM runs"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY created_at DESC, run_id DESC LIMIT %s",
                (*params, None if limit is None else limit + 1)).fetchall()
        more = limit is not None and len(rows) > limit
        rows = rows[:limit] if more else rows
        return RunPage(
            runs=[RunSummary(run_id=r[0], workflow=r[1], status=_status(r[2]), created_at=r[3],
                             updated_at=r[4]) for r in rows],
            next_cursor=encode_cursor(rows[-1][3].isoformat(), rows[-1][0]) if more else None)

    def run_status_counts(self, *, workflow: str | None = None) -> dict[str, int]:
        where, params = _run_filter(None, workflow)
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM runs WHERE status IS NOT NULL"
                + "".join(f" AND {w}" for w in where) + " GROUP BY status", tuple(params)
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def run_id_for_request(self, request_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE request_id = %s", (request_id,)
            ).fetchone()
        return row[0] if row else None

    # archive (E-08): every row of one run in one transaction, see Store.purge_run
    def purge_run(self, run_id: str, *, expected_seq: int,
                  blobs: Sequence[str] = ()) -> list[str]:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT last_seq FROM runs WHERE run_id = %s FOR UPDATE",
                               (run_id,)).fetchone()          # serializes with appenders
            current = row[0] if row else 0
            if row is None or current != expected_seq:
                raise ConflictError(f"run {run_id!r}: expected seq {expected_seq}, log is at "
                                    f"{current}; not purged")
            # children first: run_events and run_snapshots REFERENCE runs(run_id)
            for table in ("run_events", "run_snapshots", "queue", "leases", "runs"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,))
            removed: list[str] = []
            for sha in dict.fromkeys(blobs):              # once each, caller's order
                referenced = conn.execute(
                    "SELECT 1 FROM run_events WHERE strpos(record::text, %s) > 0 LIMIT 1", (sha,)
                ).fetchone()
                if referenced is None and conn.execute(
                        "DELETE FROM blobs WHERE sha256 = %s", (sha,)).rowcount:
                    removed.append(sha)
            return removed

    # blobs
    def put(self, data: bytes, media_type: str = "application/json") -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO blobs(sha256, size, media_type, data) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (sha256) DO NOTHING",
                (digest, len(data), media_type, psycopg.Binary(data)),
            )
        return BlobRef(sha256=digest, size=len(data), media_type=media_type)

    def get(self, ref: BlobRef) -> bytes:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM blobs WHERE sha256 = %s", (ref.sha256,)).fetchone()
        if row is None:
            raise KeyError(f"blob {ref.sha256[:12]}… not found")
        return bytes(row[0])

    def exists(self, ref: BlobRef) -> bool:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM blobs WHERE sha256 = %s", (ref.sha256,)
            ).fetchone() is not None

    # lease (database clock, so all workers agree on "now")
    def acquire(self, run_id: str, holder: str, ttl_seconds: float) -> LeaseToken | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT holder, fence, expires_at > now() FROM leases WHERE run_id = %s "
                "FOR UPDATE", (run_id,)
            ).fetchone()
            if row is not None and row[2] and row[0] != holder:
                return None
            fence = (row[1] if row else 0) + 1
            conn.execute(
                "INSERT INTO leases(run_id, holder, fence, expires_at) "
                "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (run_id) DO UPDATE SET holder = EXCLUDED.holder, "
                "fence = EXCLUDED.fence, expires_at = EXCLUDED.expires_at",
                (run_id, holder, fence, ttl_seconds),
            )
            # Record the fence at acquire time (see MemoryStore.acquire).
            conn.execute(
                "UPDATE runs SET max_fence = GREATEST(max_fence, %s) WHERE run_id = %s",
                (fence, run_id),
            )
            return LeaseToken(run_id=run_id, holder=holder, fence=fence)

    def renew(self, token: LeaseToken, ttl_seconds: float) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE leases SET expires_at = now() + make_interval(secs => %s) "
                "WHERE run_id = %s AND holder = %s AND fence = %s AND expires_at > now()",
                (ttl_seconds, token.run_id, token.holder, token.fence),
            )
            return cur.rowcount == 1

    def release(self, token: LeaseToken) -> None:
        # Expire, never delete: the row carries the fence counter, which must stay
        # monotonic for the life of the run.
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE leases SET expires_at = to_timestamp(0) "
                "WHERE run_id = %s AND holder = %s AND fence = %s",
                (token.run_id, token.holder, token.fence),
            )

    # queue (at-least-once; SKIP LOCKED lets N workers pull without contention)
    visibility_seconds = 30.0

    def push(self, run_id: str, *, delay_seconds: float = 0.0) -> None:
        """Enqueue, or make an in-flight delivery visible again (see SqliteStore.push)."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO queue(run_id, visible_at) "
                "VALUES (%s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (run_id) DO UPDATE SET visible_at = EXCLUDED.visible_at",
                (run_id, max(0.0, delay_seconds)),
            )

    def pull(self, timeout: float) -> str | None:
        deadline = time.time() + timeout
        while True:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "SELECT run_id FROM queue WHERE visible_at <= now() "
                    "ORDER BY visible_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE queue SET visible_at = now() + make_interval(secs => %s), "
                        "deliveries = deliveries + 1 WHERE run_id = %s",
                        (self.visibility_seconds, row[0]),
                    )
                    return row[0]
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def ack(self, run_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM queue WHERE run_id = %s", (run_id,))

    def queue_depth(self) -> int:
        with self._pool.connection() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return int(n)

    # lifecycle
    def connection(self):
        """Raw pooled connection — used by the lease adapter, never by the core."""
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()
