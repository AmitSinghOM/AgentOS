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
from collections.abc import Sequence
from pathlib import Path

from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import Agent, BlobRef, WorkflowDefinition
from agentos.core.ports import ConflictError

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    workflow   TEXT NOT NULL,
    created_at TEXT NOT NULL
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

    # definitions
    def put_agent(self, agent: Agent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO agents(name, body) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET body = excluded.body",
                (agent.name, agent.model_dump_json()),
            )

    def get_agent(self, name: str) -> Agent | None:
        row = self._conn.execute("SELECT body FROM agents WHERE name = ?", (name,)).fetchone()
        return Agent.model_validate_json(row[0]) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute("SELECT body FROM agents ORDER BY name").fetchall()
        return [Agent.model_validate_json(r[0]) for r in rows]

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
                      events: Sequence[Event]) -> list[Event]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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

    def close(self) -> None:
        self._conn.close()
