"""Worker: pulls run ids from the queue and advances them under a fenced lease.

Exactly-once advancement comes from three things working together, none of which is the
queue: the append-only log (a completed step is never re-executed), the lease (one holder
per run at a time), and the fence (a stale holder cannot write). The queue may deliver a
run id twice, late, or to two workers — all of those are safe.

Crash recovery is a *sweep*, not queue magic: on start (and every `sweep_interval`) the
worker folds every non-terminal run and re-enqueues it. A worker that was SIGKILLed
mid-run leaves a lease that expires; the next sweep or delivery finds the run
non-terminal, takes a new lease with a higher fence, and `advance()` continues from the
last committed step.
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
from dagentos.core.ports import ConflictError, Store

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

    # ------------------------------------------------------------------ sweep
    def recover(self) -> list[str]:
        """Re-enqueue every run that needs a worker (not terminal, not paused, not
        suspended). Also expires timed-out approvals (C7). Idempotent."""
        expired = self._engine.expire_approvals()
        if expired:
            log.info("expired %d approval(s)", len(expired))
        found = []
        for run_id in self._store.list_run_ids():
            if self._engine.needs_worker(run_id):
                self._queue.push(run_id)
                found.append(run_id)
        if found:
            log.info("recovery sweep re-enqueued %d run(s)", len(found))
        return found

    # ------------------------------------------------------------------- loop
    def run_once(self, timeout: float = 1.0) -> str | None:
        """Pull one run id and process it. Returns the run id handled, or None."""
        run_id = self._queue.pull(timeout)
        if run_id is None:
            return None
        try:
            self._process(run_id)
        except Exception as exc:
            exc.add_note(f"run {run_id}")      # so the loop's log line names the run
            raise
        return run_id

    def run_forever(self, *, stop: Callable[[], bool] = lambda: False,
                    sweep_interval: float = 60.0) -> None:
        """The worker process. A failure while handling one delivery — a transient store
        error, an executor bug that escaped settle — is logged with the run id and the loop
        goes on after a bounded back-off: one bad run or one database blip must not stall
        every other run until an operator restarts the process (FAIL_MODES: Worker loop).
        The delivery stays un-acked, so the queue redelivers it after its visibility timeout.
        `run_once` deliberately keeps raising."""
        self.recover()
        next_sweep = self._clock() + sweep_interval
        while not stop():
            try:
                self.run_once(timeout=1.0)
            except Exception as exc:  # the loop is the boundary; see docstring
                self.errors += 1
                notes = " ".join(getattr(exc, "__notes__", ()) or ())
                log.exception("worker loop: %s %s (continuing after %.1fs)",
                              notes, type(exc).__name__, self.error_backoff_seconds)
                if self.error_backoff_seconds > 0:
                    time.sleep(self.error_backoff_seconds)
            if self._clock() >= next_sweep:
                self.recover()
                next_sweep = self._clock() + sweep_interval

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
            if delay is not None and run.status.value == "running":
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
