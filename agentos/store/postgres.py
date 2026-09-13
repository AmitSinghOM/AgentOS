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
from collections.abc import Sequence

import psycopg
from psycopg import errors
from psycopg_pool import ConnectionPool

from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import Agent, BlobRef, WorkflowDefinition
from agentos.core.ports import ConflictError

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    body JSONB NOT NULL
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
    last_seq   INTEGER NOT NULL DEFAULT 0
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

    # definitions
    def put_agent(self, agent: Agent) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO agents(name, body) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body",
                (agent.name, agent.model_dump_json()),
            )

    def get_agent(self, name: str) -> Agent | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT body FROM agents WHERE name = %s", (name,)).fetchone()
        return Agent.model_validate(row[0]) if row else None

    def list_agents(self) -> list[Agent]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT body FROM agents ORDER BY name").fetchall()
        return [Agent.model_validate(r[0]) for r in rows]

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
                      events: Sequence[Event]) -> list[Event]:
        with self._pool.connection() as conn, conn.transaction():
            # Serialize appenders on the run row. A brand-new run has no row yet, so
            # concurrent creators race on runs.request_id / PRIMARY KEY instead.
            row = conn.execute(
                "SELECT last_seq FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            current = row[0] if row else 0
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
            conn.execute("UPDATE runs SET last_seq = %s WHERE run_id = %s",
                         (expected_seq + len(out), run_id))
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

    # lifecycle
    def connection(self):
        """Raw pooled connection — used by the lease adapter, never by the core."""
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()
