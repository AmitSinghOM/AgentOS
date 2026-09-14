"""SQLite Store + BlobStore on the standard library.

Why SQLite first (docs/DEVELOPMENT_STRUCTURE.md §7): `pip install agentos` must work with
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
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from agentos.core.coordination import LeaseToken
from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import Agent, BlobRef, WorkflowDefinition
from agentos.core.ports import ConflictError

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


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        # One connection, one lock: SQLite serializes writers anyway, and a single
        # connection keeps ":memory:" databases coherent across calls.
        self._conn = sqlite3.connect(self._path, check_same_thread=False,
                                     isolation_level=None)  # autocommit; explicit BEGIN
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

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
            row = self._conn.execute(
                "SELECT body FROM agent_versions WHERE name = ? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT body FROM agent_versions WHERE name = ? AND version = ?", (name, version),
            ).fetchone()
        return Agent.model_validate_json(row[0]) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute(
            "SELECT a.body FROM agent_versions a JOIN (SELECT name, MAX(version) v "
            "FROM agent_versions GROUP BY name) m ON a.name = m.name AND a.version = m.v "
            "ORDER BY a.name"
        ).fetchall()
        return [Agent.model_validate_json(r[0]) for r in rows]

    def list_agent_versions(self, name: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT version FROM agent_versions WHERE name = ? ORDER BY version", (name,)
        ).fetchall()
        return [r[0] for r in rows]

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflows(name, body) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET body = excluded.body",
                (wf.name, wf.model_dump_json()),
            )

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        row = self._conn.execute(
            "SELECT body FROM workflows WHERE name = ?", (name,)
        ).fetchone()
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
                if fence is not None:
                    self._conn.execute(
                        "UPDATE runs SET max_fence = ? WHERE run_id = ?", (fence, run_id)
                    )
                self._conn.execute("COMMIT")
                return out
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def read_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        rows = self._conn.execute(
            "SELECT record FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
        return [from_record(json.loads(r[0])) for r in rows]

    def list_run_ids(self) -> list[str]:
        rows = self._conn.execute("SELECT run_id FROM runs ORDER BY created_at").fetchall()
        return [r[0] for r in rows]

    def run_id_for_request(self, request_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE request_id = ?", (request_id,)
        ).fetchone()
        return row[0] if row else None

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
        row = self._conn.execute("SELECT data FROM blobs WHERE sha256 = ?", (ref.sha256,)).fetchone()
        if row is None:
            raise KeyError(f"blob {ref.sha256[:12]}… not found")
        return bytes(row[0])

    def exists(self, ref: BlobRef) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM blobs WHERE sha256 = ?", (ref.sha256,)
        ).fetchone() is not None

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
        (n,) = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return int(n)

    def close(self) -> None:
        self._conn.close()
