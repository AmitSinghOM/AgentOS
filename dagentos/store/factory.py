"""One place that turns `AGENTOS_STORE*` into a store, shared by the API, the worker and the
CLI so the three cannot drift on variable names.

  AGENTOS_STORE         memory | sqlite | postgres   (default sqlite)
  AGENTOS_SQLITE_PATH   file path                    (default ./agentos.db)
  AGENTOS_PG_DSN        postgresql://…               (postgres)
  AGENTOS_PG_SCHEMA     optional schema              (postgres; ^[a-z_][a-z0-9_]{0,62}$)
"""
from __future__ import annotations

import os
import re

# The schema name is interpolated into `CREATE SCHEMA IF NOT EXISTS "{s}"` and `search_path`
# (dagentos/store/postgres.py), so only a plain lowercase identifier is accepted (audit I-18).
PG_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_pg_schema(schema: str | None) -> str | None:
    """Return the schema name, None when unset or empty; raise naming the variable and rule."""
    if not schema:
        return None
    if not PG_SCHEMA_RE.fullmatch(schema):
        raise RuntimeError(f"AGENTOS_PG_SCHEMA must match {PG_SCHEMA_RE.pattern} "
                           f"(a plain lowercase Postgres identifier), got {schema!r}")
    return schema


def store_from_env(*, allow_memory: bool = True):
    kind = os.environ.get("AGENTOS_STORE", "sqlite").lower()
    if kind == "memory":
        if not allow_memory:
            raise RuntimeError("AGENTOS_STORE=memory is process-local: nothing would be shared "
                               "with the API process (use sqlite or postgres)")
        from dagentos.store.memory import MemoryStore
        return MemoryStore()
    if kind == "sqlite":
        from dagentos.store.sqlite import SqliteStore
        return SqliteStore(os.environ.get("AGENTOS_SQLITE_PATH", "agentos.db"))
    if kind == "postgres":
        from dagentos.store.postgres import PostgresStore
        dsn = os.environ.get("AGENTOS_PG_DSN")
        if not dsn:
            raise RuntimeError("AGENTOS_STORE=postgres requires AGENTOS_PG_DSN")
        schema = validate_pg_schema(os.environ.get("AGENTOS_PG_SCHEMA"))
        return PostgresStore(dsn, schema=schema)
    raise RuntimeError(f"unknown AGENTOS_STORE {kind!r} (memory | sqlite | postgres)")
