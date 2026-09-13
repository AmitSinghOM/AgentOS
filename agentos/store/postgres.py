"""PostgreSQL Store + BlobStore on psycopg 3.

The production adapter. Same contract as SQLite (tests/contract/), same physical
guarantees, plus the primitive the worker slice needs: `pg_try_advisory_xact_lock` /
session advisory locks for per-run leases live in `agentos/store/pg_lease.py`.

- `PRIMARY KEY (run_id, seq)` rejects a duplicate append at the database (C2).
- Each `append_events` is one transaction: `SELECT ... FOR UPDATE` on the run row
  serializes concurrent appenders so exactly one sees the expected seq.
- `runs.request_id UNIQUE` makes run start idempotent under concurrency.

Schema management: `SqlStore.migrate()` creates tables idempotently. Alembic arrives with
the first schema change; for a brand-new schema, `CREATE TABLE IF NOT EXISTS` is honest.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence

import psycopg
from psycopg import errors
from psycopg_pool import ConnectionPool

from agentos.core.coordination import LeaseToken
from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import Agent, BlobRef, WorkflowDefinition
from agentos.core.ports import ConflictError

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

    def migrate(self) -> None:
        with self._pool.connection() as conn:
            if self._schema:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            conn.execute(SCHEMA)

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
                "SELECT last_seq, max_fence FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
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
            conn.execute(
                "UPDATE runs SET last_seq = %s, max_fence = GREATEST(max_fence, %s) "
                "WHERE run_id = %s",
                (expected_seq + len(out), fence if fence is not None else 0, run_id),
            )
            return out

    def read_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT record FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [from_record(r[0]) for r in rows]

    def list_run_ids(self) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT run_id FROM runs ORDER BY created_at, run_id").fetchall()
        return [r[0] for r in rows]

    def run_id_for_request(self, request_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE request_id = %s", (request_id,)
            ).fetchone()
        return row[0] if row else None

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

    # lifecycle
    def connection(self):
        """Raw pooled connection — used by the lease adapter, never by the core."""
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()
