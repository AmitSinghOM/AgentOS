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
from agentos.agents.tool import ToolExecutor
from agentos.core.engine import Engine
from agentos.core.faults import from_env
from agentos.core.models import AgentType
from agentos.core.policy import policy_from_env
from agentos.observability import build_observers, store_resolver
from agentos.plugins import discover_executors, store_pricing_snapshots
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
    observers, prom = build_observers(resolve=store_resolver(store))
    if prom is not None and os.environ.get("AGENTOS_WORKER_METRICS_PORT", "8001") != "0":
        # The worker has its own Prometheus registry (step/run outcomes happen here, the
        # API only sees run.started); expose it so Prometheus scrapes both processes.
        # Telemetry never breaks the worker: a busy port is a warning, not a crash.
        from prometheus_client import start_http_server
        port = int(os.environ.get("AGENTOS_WORKER_METRICS_PORT", "8001"))
        try:
            start_http_server(port, registry=prom.registry)
            logging.getLogger("agentos.worker").info("worker metrics on :%d/metrics", port)
        except OSError as exc:
            logging.getLogger("agentos.worker").warning(
                "worker metrics not served: port %d unavailable (%s); set "
                "AGENTOS_WORKER_METRICS_PORT to another port or 0 to disable", port, exc)
    executors = {AgentType.echo.value: EchoExecutor(), AgentType.tool.value: ToolExecutor(),
                 **discover_executors()}
    store_pricing_snapshots(executors, store)
    # AGENTOS_SNAPSHOT_EVERY (default 200; 0 disables): kept in step with agentos/api/main.py,
    # not imported from it — importing the API module would build the API's own store.
    raw = os.environ.get("AGENTOS_SNAPSHOT_EVERY", "200")
    if not raw.isdigit():
        raise RuntimeError(f"AGENTOS_SNAPSHOT_EVERY must be an integer >= 0, got {raw!r}")
    engine = Engine(store=store, blobs=store, executors=executors,
                    faults=injector, lease=store, observers=observers,
                    snapshot_every=int(raw), policy=policy_from_env())
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
