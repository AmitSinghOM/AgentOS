"""One place that turns `AGENTOS_STORE*` into a store, shared by the API, the worker and the
CLI so the three cannot drift on variable names.

  AGENTOS_STORE         memory | sqlite | postgres   (default sqlite)
  AGENTOS_SQLITE_PATH   file path                    (default ./agentos.db)
  AGENTOS_PG_DSN        postgresql://…               (postgres)
  AGENTOS_PG_SCHEMA     optional schema              (postgres)
"""
from __future__ import annotations

import os


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
        return PostgresStore(dsn, schema=os.environ.get("AGENTOS_PG_SCHEMA"))
    raise RuntimeError(f"unknown AGENTOS_STORE {kind!r} (memory | sqlite | postgres)")
