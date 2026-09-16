"""Versioned schema migrations for the SQL adapters (C10, #10).

Numbered, forward-only, raw SQL per dialect, recorded in `schema_migrations`. Chosen over
Alembic deliberately: the two adapters keep hand-written dialect DDL (SQLite `WITHOUT
ROWID`, Postgres `JSONB`/`TIMESTAMPTZ`), so an Alembic environment per dialect would
wrap forty lines of SQL in two hundred of scaffolding and a SQLAlchemy dependency the
project does not otherwise use. A 2033 reader can understand this file in one sitting.

Rules: a migration is never edited once released — add the next number. Every statement
must be idempotent (`IF NOT EXISTS`) so a database created before the ledger existed
(v0.2–v0.6) adopts it by simply running everything; nothing is dropped or rewritten.
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
