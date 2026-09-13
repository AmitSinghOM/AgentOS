"""`python -m agentos.worker [--once]` — composition root for the worker process.

Shares store configuration with the API (AGENTOS_STORE / AGENTOS_SQLITE_PATH /
AGENTOS_PG_DSN). AGENTOS_FAULT=<point>[:<step_id>] installs a hard-exit fault injector;
this is how the real kill -9 chaos test crashes a worker at a chosen boundary.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.faults import from_env
from agentos.core.models import AgentType
from agentos.worker import Worker


def build_store():
    kind = os.environ.get("AGENTOS_STORE", "sqlite").lower()
    if kind == "memory":
        raise RuntimeError("the worker cannot use AGENTOS_STORE=memory: nothing would be "
                           "shared with the API process")
    if kind == "sqlite":
        from agentos.store.sqlite import SqliteStore
        return SqliteStore(os.environ.get("AGENTOS_SQLITE_PATH", "agentos.db"))
    if kind == "postgres":
        from agentos.store.postgres import PostgresStore
        return PostgresStore(os.environ["AGENTOS_PG_DSN"],
                             schema=os.environ.get("AGENTOS_PG_SCHEMA"))
    raise RuntimeError(f"unknown AGENTOS_STORE {kind!r} (sqlite | postgres)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentos.worker")
    parser.add_argument("--once", action="store_true",
                        help="recover, process at most one run, exit")
    parser.add_argument("--holder", default=None)
    parser.add_argument("--lease-ttl", type=float, default=30.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("AGENTOS_LOG", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    store = build_store()
    injector = from_env()
    engine = Engine(store=store, blobs=store, executors={AgentType.echo.value: EchoExecutor()},
                    faults=injector)
    worker = Worker(engine, store, lease=store, queue=store, holder=args.holder,
                    lease_ttl=args.lease_ttl, faults=injector)
    if args.once:
        worker.recover()
        handled = worker.run_once(timeout=2.0)
        print(handled or "", end="")
        return 0
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
