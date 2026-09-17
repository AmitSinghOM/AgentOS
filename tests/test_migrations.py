"""Schema migrations (C10, #10): the ledger's rules are enforced here, not just documented.

- A released migration is never edited: its SQL is pinned by hash per dialect. Editing one
  fails this test with the instruction to add the next number instead (review finding F7).
- A database created before the ledger existed (v0.2-v0.6: tables present, no
  `schema_migrations`) adopts it by running every migration; nothing is dropped
  (review finding F3).
- Applying a migration is logged once, so an upgrade is visible (review finding F6).
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agentos.store import migrations
from agentos.store.sqlite import SCHEMA as SQLITE_SCHEMA
from agentos.store.sqlite import SqliteStore

# version -> dialect -> sha256 of the SQL as RELEASED. Add a row when you add a migration.
# Never change an existing row: that is the whole point.
RELEASED = {
    2: {
        "sqlite": "d5a102234a1c05f54082ae5a22bc07675ec7d787fc26061267297ca6580a2f69",
        "postgres": "a08f3cbb7d8bd20f5072d408f435898ae709eac7ed288298f5f103c9570cbcce",
    },
}


def _sha(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def test_released_migrations_are_never_edited():
    by_version = {v: by for v, _d, by in migrations.MIGRATIONS}
    for version, dialects in RELEASED.items():
        assert version in by_version, f"migration {version} was removed; migrations are forward-only"
        for dialect, digest in dialects.items():
            actual = _sha(by_version[version][dialect])
            assert actual == digest, (
                f"migration {version} ({dialect}) was edited after release. Do not edit a "
                f"released migration: add migration {migrations.CURRENT_VERSION + 1} instead."
            )


def test_every_released_migration_is_pinned():
    # A new migration must be pinned in RELEASED before it ships (version 1 is the adapters'
    # baseline SCHEMA, pinned by the adapters' own contract tests rather than here).
    unpinned = [v for v, _d, by in migrations.MIGRATIONS if v != 1 and v not in RELEASED]
    assert not unpinned, f"pin migrations {unpinned} in RELEASED (sha256 per dialect)"
    assert migrations.CURRENT_VERSION == max(RELEASED)


def test_a_pre_ledger_database_adopts_the_ledger_without_losing_anything(tmp_path):
    # Simulate a v0.6 database: the baseline tables exist, there is no schema_migrations,
    # and it holds a row we must still find afterwards.
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(SQLITE_SCHEMA)
    raw.execute("INSERT INTO blobs (sha256, size, media_type, data) VALUES (?, ?, ?, ?)",
                ("ab" * 32, 7, "text/plain", b"payload"))
    raw.commit()
    assert not raw.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('schema_migrations', 'run_snapshots')"
    ).fetchall()
    raw.close()

    store = SqliteStore(path)                                    # runs migrate() in __init__
    try:
        assert store.schema_version() == migrations.CURRENT_VERSION
        names = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        assert {"schema_migrations", "run_snapshots"} <= names
        assert store._conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
        ledger = store._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [r[0] for r in ledger] == [v for v, _d, _by in migrations.MIGRATIONS]
        assert store.migrate() == []                              # idempotent
    finally:
        store.close()


def test_applying_a_migration_is_logged_once(tmp_path, caplog):
    with caplog.at_level("INFO", logger="agentos.store"):
        store = SqliteStore(tmp_path / "fresh.db")
        store.close()
    applied = [r.getMessage() for r in caplog.records if "schema migration" in r.getMessage()]
    assert len(applied) == len(migrations.MIGRATIONS)
    assert applied[-1].startswith(f"sqlite schema migration {migrations.CURRENT_VERSION} applied")
    caplog.clear()
    with caplog.at_level("INFO", logger="agentos.store"):
        store = SqliteStore(tmp_path / "fresh.db")                # already current: silent
        store.close()
    assert not [r for r in caplog.records if "schema migration" in r.getMessage()]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_every_migration_names_both_dialects(dialect):
    for v, _d, by in migrations.MIGRATIONS:
        if v == 1:
            continue
        assert dialect in by, f"migration {v} has no {dialect} SQL"
