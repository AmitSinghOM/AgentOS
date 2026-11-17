"""Worker: pulls run ids from the queue and advances them under a fenced lease.

Exactly-once advancement comes from three things working together, none of which is the
queue: the append-only log (a completed step is never re-executed), the lease (one holder
per run at a time), and the fence (a stale holder cannot write). The queue may deliver a
run id twice, late, or to two workers — all of those are safe.

Crash recovery is a *sweep*, not queue magic: on start (and every `sweep_interval`) the
worker folds every run the run index lists as pending or running and re-enqueues the ones
the fold says need a worker. A worker that was SIGKILLed mid-run leaves a lease that
expires; the next sweep or delivery finds the run non-terminal, takes a new lease with a
higher fence, and `advance()` continues from the last committed step.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable

from dagentos.core import faults
from dagentos.core.coordination import Lease, LeaseToken, Queue
from dagentos.core.engine import Engine, LeaseLost
from dagentos.core.faults import FaultInjector, NoFaults
from dagentos.core.models import RunStatus
from dagentos.core.ports import ConflictError, Store
from dagentos.core.run_index import NEEDS_WORKER

log = logging.getLogger("agentos.worker")


class Worker:
    def __init__(self, engine: Engine, store: Store, lease: Lease, queue: Queue, *,
                 holder: str | None = None, lease_ttl: float = 30.0,
                 faults: FaultInjector | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._engine = engine
        self._store = store
        self._lease = lease
        self._queue = queue
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_ttl = lease_ttl
        self._faults = faults or NoFaults()
        self._clock = clock
        self.processed = 0
        self.errors = 0
        self.error_backoff_seconds = 1.0
        self._current: str | None = None
        self._pause_seen: str | None = None    # last logged scheduler state (A12)

    # --------------------------------------------------------- global pause (A12)
    def _dispatch_blocked(self) -> bool:
        """True while the operator's global pause is set — or while it cannot be read, which
        fails CLOSED (docs/FAIL_MODES.md). Logs once per transition, not once per tick."""
        try:
            state = self._store.get_scheduler_state()
        except Exception as exc:  # noqa: BLE001 — fail closed, see docstring
            if self._pause_seen != "unreadable":
                log.error("scheduler state unreadable (%s: %s); treating as paused: nothing is "
                          "pulled or dispatched until it reads again", type(exc).__name__, exc)
                self._pause_seen = "unreadable"
            return True
        if state.paused:
            if self._pause_seen != "paused":
                log.warning("scheduler paused by %s%s; finishing in-flight steps, dispatching "
                            "nothing new", state.who(), f": {state.reason}" if state.reason else "")
                self._pause_seen = "paused"
            return True
        if self._pause_seen not in (None, "running"):
            log.warning("scheduler resumed by %s; dispatch continues", state.who())
        self._pause_seen = "running"
        return False

    # ------------------------------------------------------------------ sweep
    def recover(self) -> list[str]:
        """Re-enqueue every run that needs a worker (not terminal, not paused, not
        suspended). Also expires timed-out approvals (C7). Idempotent. Does nothing
        while the scheduler is paused (A12): the deployment is frozen, not drained.
        The run index names the candidates (E-07) — suspended runs for expiry, pending or
        running ones for re-enqueue — so a sweep folds those, not every run ever recorded;
        each candidate is still re-checked against its fold before anything acts on it."""
        if self._dispatch_blocked():
            return []
        expired = self._engine.expire_approvals(self._run_ids(RunStatus.suspended))
        if expired:
            log.info("expired %d approval(s)", len(expired))
        found = []
        for run_id in self._run_ids(*NEEDS_WORKER):
            if self._engine.needs_worker(run_id):
                self._queue.push(run_id)
                found.append(run_id)
        if found:
            log.info("recovery sweep re-enqueued %d run(s)", len(found))
        return found

    def _run_ids(self, *status: RunStatus) -> list[str]:
        """Indexed runs in `status`, oldest first (the order the sweep always enqueued in)."""
        return [r.run_id for r in reversed(self._store.list_runs(status=status, limit=None).runs)]

    # ------------------------------------------------------------------- loop
    def run_once(self, timeout: float = 1.0) -> str | None:
        """Pull one run id and process it. Returns the run id handled, or None. While the
        scheduler is paused the queue is not touched: sleep the poll interval instead."""
        if self._dispatch_blocked():
            time.sleep(timeout)
            return None
        run_id = self._queue.pull(timeout)
        if run_id is None:
            return None
        self._current = run_id                 # so the loop's log line can name the run
        self._process(run_id)
        return run_id

    def run_forever(self, *, stop: Callable[[], bool] = lambda: False,
                    sweep_interval: float = 60.0) -> None:
        """The worker process. A failure while handling one delivery — a transient store
        error, an executor bug that escaped settle — is logged with the run id and the loop
        goes on after a bounded back-off: one bad run or one database blip must not stall
        every other run until an operator restarts the process (FAIL_MODES: Worker loop).
        The delivery stays un-acked, so the queue redelivers it after its visibility timeout.
        `run_once` deliberately keeps raising, and so does the boot sweep: a store that is
        down when the process starts must be loud, not retried quietly forever."""
        self.recover()
        next_sweep = self._clock() + sweep_interval
        while not stop():
            try:
                self._current = None
                self.run_once(timeout=1.0)
                if self._clock() >= next_sweep:
                    next_sweep = self._clock() + sweep_interval
                    self._current = "<sweep>"
                    self.recover()
            except Exception as exc:  # the loop is the boundary; see docstring
                self.errors += 1
                log.exception("worker loop: run %s: %s (continuing after %.1fs)",
                              self._current, type(exc).__name__, self.error_backoff_seconds)
                if self.error_backoff_seconds > 0:
                    time.sleep(self.error_backoff_seconds)

    # ---------------------------------------------------------------- process
    def _process(self, run_id: str) -> None:
        if not self._engine.needs_worker(run_id):
            self._queue.ack(run_id)          # terminal or paused; nothing to do
            return
        token = self._lease.acquire(run_id, self.holder, self.lease_ttl)
        if token is None:
            log.debug("run %s leased elsewhere; leaving on queue", run_id)
            return                            # redelivered after visibility timeout
        self._faults.at(faults.AFTER_LOCK_ACQUIRE, run_id=run_id, holder=self.holder)
        try:
            run = self._engine.advance(run_id, fence=token.fence,
                                       heartbeat=lambda: self._heartbeat(token))
            self._faults.at(faults.AFTER_RUN_COMMIT_BEFORE_ACK, run_id=run_id)
            delay = self._engine.next_retry_delay(run)
            if run.status.value == "running" and delay is None:
                # Stopped at a wave boundary by the global pause (A12): still running, nothing
                # pending — the only way advance() returns such a run. Hand it straight back so
                # the first tick after `resume --all` continues it, not the next sweep.
                self._queue.push(run_id)
                log.info("run %s: parked after its in-flight wave; scheduler paused", run_id)
            elif delay is not None and run.status.value == "running":
                # Waiting on a backoff: hand the run back to the queue, not before then.
                self._queue.push(run_id, delay_seconds=delay)
                log.info("run %s: retry scheduled in %.2fs", run_id, delay)
            else:
                self._queue.ack(run_id)      # terminal, or paused until run.resumed
                self.processed += 1
        except LeaseLost:
            log.warning("run %s: lease lost mid-run; another worker will continue", run_id)
        except ConflictError as exc:
            # Someone else advanced this run (or we were fenced). The log is the truth;
            # leave the delivery to be retried and the next advance() will replay.
            log.warning("run %s: %s", run_id, exc)
        finally:
            self._lease.release(token)

    def _heartbeat(self, token: LeaseToken) -> bool:
        return self._lease.renew(token, self.lease_ttl)
