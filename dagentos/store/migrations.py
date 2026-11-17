"""Versioned schema migrations for the SQL adapters (C10, #10).

Numbered, forward-only, raw SQL per dialect, recorded in `schema_migrations`. Chosen over
Alembic deliberately: the two adapters keep hand-written dialect DDL (SQLite `WITHOUT
ROWID`, Postgres `JSONB`/`TIMESTAMPTZ`), so an Alembic environment per dialect would
wrap forty lines of SQL in two hundred of scaffolding and a SQLAlchemy dependency the
project does not otherwise use. A 2033 reader can understand this file in one sitting.

Rules: a migration is never edited once released — add the next number. Every statement
must be idempotent (`IF NOT EXISTS`) so a database created before the ledger existed
(v0.2–v0.6) adopts it by simply running everything; nothing is dropped or rewritten. The one
statement SQLite cannot make idempotent is `ALTER TABLE ... ADD COLUMN` (migration 4), so the
SQLite adapter commits each migration together with its ledger row: it runs once or not at all.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

_log = logging.getLogger("agentos.store")

# (version, description, {dialect: sql})
MIGRATIONS: list[tuple[int, str, dict[str, str]]] = [
    (1, "baseline: agents, workflows, runs, leases, queue, run_events, blobs", {
        # Filled in by each adapter from its SCHEMA constant (kept next to the code that
        # queries it); see SqliteStore/PostgresStore.
    }),
    (2, "run_snapshots: bounded replay (C15)", {
        "sqlite": """
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id     TEXT    PRIMARY KEY,
    seq        INTEGER NOT NULL,
    last_hash  TEXT,
    state      TEXT    NOT NULL,
    taken_at   TEXT    NOT NULL
) WITHOUT ROWID;
""",
        "postgres": """
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id     TEXT    PRIMARY KEY REFERENCES runs(run_id),
    seq        INTEGER NOT NULL,
    last_hash  TEXT,
    state      JSONB   NOT NULL,
    taken_at   TIMESTAMPTZ NOT NULL
);
""",
    }),
    (3, "scheduler_state: global pause history (A12 / E-03)", {
        # Append-only; the newest row is the current state. Not a run log: replay is per run.
        "sqlite": """
CREATE TABLE IF NOT EXISTS scheduler_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paused     INTEGER NOT NULL,
    principal  TEXT,
    reason     TEXT    NOT NULL DEFAULT '',
    changed_at TEXT    NOT NULL
);
""",
        "postgres": """
CREATE TABLE IF NOT EXISTS scheduler_state (
    id         BIGSERIAL PRIMARY KEY,
    paused     BOOLEAN NOT NULL,
    principal  JSONB,
    reason     TEXT    NOT NULL DEFAULT '',
    changed_at TIMESTAMPTZ NOT NULL
);
""",
    }),
    (4, "runs index: status, pending_approvals, indexed_seq, updated_at (E-07)", {
        # A rebuildable index of each run's log (dagentos.core.run_index), written in the
        # append transaction. NULL until indexed: the adapter's migrate() indexes every run whose
        # row is behind its log, which on this upgrade is every run (the backfill). `workflow`
        # and `created_at` were already columns. SQLite has no ADD COLUMN IF NOT EXISTS, so its
        # adapter runs each migration and its ledger row in one transaction instead.
        "sqlite": """
ALTER TABLE runs ADD COLUMN status TEXT;
ALTER TABLE runs ADD COLUMN pending_approvals TEXT NOT NULL DEFAULT '[]';
ALTER TABLE runs ADD COLUMN indexed_seq INTEGER;
ALTER TABLE runs ADD COLUMN updated_at TEXT;
CREATE INDEX IF NOT EXISTS runs_by_status ON runs (status, created_at, run_id);
CREATE INDEX IF NOT EXISTS runs_by_created ON runs (created_at, run_id);
""",
        "postgres": """
ALTER TABLE runs ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS pending_approvals JSONB NOT NULL DEFAULT '[]';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS indexed_seq INTEGER;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS runs_by_status ON runs (status, created_at, run_id);
CREATE INDEX IF NOT EXISTS runs_by_created ON runs (created_at, run_id);
""",
    }),
]

LEDGER = {
    "sqlite": """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
""",
    "postgres": """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL
);
""",
}

CURRENT_VERSION = MIGRATIONS[-1][0]


def apply(dialect: str, baseline_sql: str, execute_script: Callable[[str], None],
          applied_versions: Callable[[], set[int]],
          record: Callable[[int, str, str], None]) -> list[int]:
    """Run every migration not yet in the ledger, in order, and record it. Adapters pass
    three small callables so this module owns the *policy* and knows no driver. Returns
    the versions applied in this call."""
    execute_script(LEDGER[dialect])
    done = applied_versions()
    applied: list[int] = []
    for version, description, by_dialect in MIGRATIONS:
        if version in done:
            continue
        sql = baseline_sql if version == 1 else by_dialect[dialect]
        execute_script(sql)
        record(version, description, datetime.now(UTC).isoformat(timespec="seconds"))
        applied.append(version)
        # Visible once per upgrade, so an operator moving a v0.6 database forward can see
        # what changed (review finding F6); silent on an already-current database.
        _log.info("%s schema migration %d applied: %s", dialect, version, description)
    return applied
