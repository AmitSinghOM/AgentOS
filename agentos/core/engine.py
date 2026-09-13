"""Workflow engine.

Every state change is an appended event; run state is only ever the fold of the log; a
step that already has `step.completed` is replayed from the log, never re-executed (C1).
`advance()` is re-entrant and is what the worker calls under a fenced lease (C6).

Phase 2 — the engine is the **scheduler** and the **governor**:
  schedule — the DAG is executed in *waves*: every node whose dependencies are complete
             runs concurrently (bounded by `max_parallelism`); appends are serialized.
  gate     — an agent whose DECLARED effects exceed the workflow budget is refused before
             dispatch (declare-then-do, A1);
  dispatch — the executor gets a StepRequest and a progress() callback that renews the
             lease (A5);
  verify   — reported effects ⊆ declared; cost and wall time within budget (A6);
             violations dead-letter the step (C11);
  retry    — an executor exception schedules the next attempt with exponential backoff
             (`step.failed` with `terminal=False, retry_at`); the last attempt dead-letters;
  record   — step.completed carries effects, cost and provenance; the run's rolling cost
             is checked against the run ceiling.

The engine depends only on the ports in `agentos.core`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from agentos.core import faults
from agentos.core.events import (
    Event,
    RunCompleted,
    RunFailed,
    RunStarted,
    StepCompleted,
    StepDeadLettered,
    StepFailed,
    StepProgress,
    StepRetryRequested,
    StepStarted,
)
from agentos.core.faults import FaultInjector, NoFaults
from agentos.core.fold import fold
from agentos.core.models import (
    Budget,
    Cost,
    EffectClass,
    Principal,
    StepRequest,
    StepResult,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowRun,
)
from agentos.core.ports import BlobStore, ConflictError, Executor, Store

TERMINAL = frozenset({"completed", "failed"})
PROGRESS_MIN_INTERVAL = 1.0   # seconds between step.progress events (rate limit)


class LeaseLost(Exception):
    """The heartbeat reported the lease is gone; stop advancing immediately."""


class RetryNotAllowed(Exception):
    """The step is not in a retryable state (not dead-lettered / failed)."""


class _Refused(Exception):
    """Internal: the governor refused a step. Carries the dead-letter cause."""

    def __init__(self, cause: str, *, effect_class: EffectClass | None = None,
                 cost: Cost | None = None) -> None:
        super().__init__(cause)
        self.cause, self.effect_class, self.cost = cause, effect_class, cost or Cost()


class _StepCrashed(Exception):
    """Internal: the executor raised. Carries the exception text and elapsed time."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _canonical(obj: dict) -> bytes:
    """Stable JSON bytes: sorted keys, no whitespace. Same inputs → same hash → same
    idempotency key across processes and Python versions."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def idempotency_key(run_id: str, step_id: str, inputs: dict) -> str:
    return f"{run_id}:{step_id}:{hashlib.sha256(_canonical(inputs)).hexdigest()}"


class _Log:
    """Serialized appender for one advance() call. Concurrent steps' progress events and
    the scheduler's own writes all go through here, so `expected_seq` is always right.
    Only the lease holder writes, so tracking last_seq locally is sound; a ConflictError
    still surfaces if that assumption is ever violated (fence, another writer)."""

    def __init__(self, store: Store, run_id: str, last_seq: int, fence: int | None) -> None:
        self._store, self.run_id, self.last_seq, self._fence = store, run_id, last_seq, fence
        self._lock = threading.Lock()
        self._closed = False

    def append(self, event: Event) -> None:
        with self._lock:
            if self._closed:
                return  # advance() has returned; a straggler thread's progress is dropped
            self._store.append_events(self.run_id, self.last_seq, [event], fence=self._fence)
            self.last_seq += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True


class Engine:
    def __init__(self, store: Store, blobs: BlobStore, executors: Mapping[str, Executor],
                 faults: FaultInjector | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        """`executors` maps an AgentType value (e.g. "echo") to the adapter that runs it.
        `clock` is monotonic (durations, rate limits); `wall` is the timestamp source for
        `retry_at` so tests can pin it."""
        self._store = store
        self._blobs = blobs
        self._executors = executors
        self._faults = faults or NoFaults()
        self._clock = clock
        self._wall = wall

    # ----------------------------------------------------------------- queries
    def get_run(self, run_id: str, *, hydrate: bool = True) -> WorkflowRun | None:
        events = self._store.read_events(run_id)
        if not events:
            return None
        run = fold(events)
        if hydrate:
            for step in run.steps:
                if step.output_ref is not None:
                    step.output = json.loads(self._blobs.get(step.output_ref))
        return run

    def is_terminal(self, run_id: str) -> bool:
        run = self.get_run(run_id, hydrate=False)
        return run is not None and run.status.value in TERMINAL

    def next_retry_delay(self, run: WorkflowRun) -> float | None:
        """Seconds until the earliest pending retry may start; None if none pending."""
        if not run.pending_retries:
            return None
        soonest = min(run.pending_retries.values())
        return max(0.0, (soonest - self._wall()).total_seconds())

    # ---------------------------------------------------------------- commands
    def create_run(self, wf_name: str, *, request_id: str | None = None,
                   principal: Principal | None = None) -> str:
        """Append run.started and return the run id WITHOUT executing anything. The
        worker picks it up from the queue. Idempotent on request_id (DESIGN §6)."""
        wf = self._store.get_workflow(wf_name)
        if wf is None:
            raise KeyError(f"unknown workflow {wf_name!r}")
        request_id = request_id or uuid4().hex
        existing = self._store.run_id_for_request(request_id)
        if existing is not None:
            return existing
        run_id = uuid4().hex
        self._store.append_events(run_id, 0, [RunStarted(
            run_id=run_id, workflow=wf.name, workflow_version=wf.version,
            request_id=request_id, principal=principal,
        )])
        return run_id

    def request_retry(self, run_id: str, step_id: str, *, principal: Principal | None = None,
                      reason: str = "") -> WorkflowRun:
        """Reopen a dead-lettered or terminally failed step (C11). Appends
        `step.retry_requested`; the caller then re-enqueues (or advances) the run."""
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if step_id not in run.dead_lettered and step_id not in run.failed_steps:
            raise RetryNotAllowed(f"step {step_id!r} of run {run_id!r} is not dead-lettered "
                                  f"or failed (status {run.status.value})")
        self._store.append_events(run_id, run.last_seq, [StepRetryRequested(
            run_id=run_id, step_id=step_id, principal=principal, reason=reason,
        )])
        return self.get_run(run_id)  # type: ignore[return-value]

    def start_run(self, wf_name: str, *, request_id: str | None = None,
                  principal: Principal | None = None, max_wait: float = 60.0) -> WorkflowRun:
        """create_run + advance in-process, sleeping through retry backoffs (bounded by
        `max_wait`). The synchronous path (tests, `?sync=true`)."""
        run_id = self.create_run(wf_name, request_id=request_id, principal=principal)
        return self.advance_until_terminal(run_id, max_wait=max_wait)

    def advance_until_terminal(self, run_id: str, *, max_wait: float = 60.0) -> WorkflowRun:
        deadline = time.monotonic() + max_wait
        while True:
            run = self.advance(run_id)
            delay = self.next_retry_delay(run)
            if run.status.value in TERMINAL or delay is None:
                return run
            if time.monotonic() + delay > deadline:
                return run
            time.sleep(delay)

    def advance(self, run_id: str, *, fence: int | None = None,
                heartbeat: Callable[[], bool] | None = None) -> WorkflowRun:
        """Drive a run from its current log position as far as it can go right now:
        to a terminal state, or until a step is waiting on a retry backoff (the caller
        re-enqueues with `next_retry_delay`).

        Re-entrant by construction: it re-derives everything from the log, skips steps
        that already completed, and appends with optimistic concurrency + the caller's
        lease fence, so calling it twice — or from two processes — cannot double-execute
        a step. `heartbeat()` is called before each wave and on every progress() call;
        returning False means the lease is lost and advancing stops (LeaseLost)."""
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if run.status.value in TERMINAL:
            return self.get_run(run_id)  # type: ignore[return-value]

        log = _Log(self._store, run_id, run.last_seq, fence)
        wf = self._store.get_workflow(run.workflow)
        if wf is None:
            return self._fail(log, f"workflow {run.workflow!r} no longer exists")
        if wf.version != run.workflow_version:
            # C3: never resume a run against a definition it did not start with.
            return self._fail(log, f"workflow {wf.name!r} is v{wf.version} but run "
                                   f"pinned v{run.workflow_version}")

        by_id = {n.id: n for n in wf.nodes}
        done: set[str] = {s.node_id for s in run.steps}
        outputs: dict[str, dict] = {
            s.node_id: json.loads(self._blobs.get(s.output_ref))
            for s in run.steps if s.output_ref is not None
        }
        attempts = dict(run.attempts)
        pending = dict(run.pending_retries)
        total = Decimal(run.total_cost)
        budget = wf.budget
        pool = ThreadPoolExecutor(max_workers=max(1, wf.max_parallelism),
                                  thread_name_prefix=f"agentos-{run_id[:8]}")
        try:
            while len(done) < len(by_id):
                if heartbeat is not None and not heartbeat():
                    raise LeaseLost(f"run {run_id!r}: lease lost")

                ready = [n for n in wf.nodes
                         if n.id not in done and all(d in done for d in n.depends_on)]
                now = self._wall()
                waiting = [n for n in ready if pending.get(n.id, now) > now]
                ready = [n for n in ready if n not in waiting]
                if not ready:
                    # Everything runnable is waiting on a backoff. Hand control back;
                    # the caller re-enqueues with next_retry_delay().
                    return self.get_run(run_id)  # type: ignore[return-value]

                # ---- one wave: start all, execute concurrently, settle sequentially.
                started: list[tuple[WorkflowNode, StepRequest, Executor]] = []
                for node in ready:
                    prepared = self._prepare(log, run_id, node, attempts, outputs, budget)
                    if isinstance(prepared, str):          # unrecoverable definition error
                        return self._fail(log, prepared, step_id=node.id)
                    started.append(prepared)

                futures = {
                    req.step_id: pool.submit(self._execute, log, req, executor, heartbeat)
                    for _, req, executor in started
                }
                # Settle EVERY step of the wave before deciding the run's fate, so a
                # completed sibling of a dead-lettered step is recorded, never lost (C4).
                dead: list[tuple[str, str]] = []
                for node, req, executor in started:
                    outcome = futures[req.step_id].result()   # re-raises Crash/LeaseLost
                    kind, payload = self._settle(log, node, req, executor, outcome, budget)
                    if kind == "retry":
                        pending[req.step_id] = payload
                    elif kind == "dead":
                        dead.append((req.step_id, payload))
                    else:  # "done"
                        result: StepResult = payload
                        outputs[req.step_id] = result.output
                        done.add(req.step_id)
                        total += result.cost.decimal()
                if dead:
                    step_id, cause = dead[0]
                    return self._fail(log, f"step {step_id!r} dead-lettered: {cause}",
                                      step_id=step_id)
                if budget.max_run_cost is not None and total > Decimal(budget.max_run_cost):
                    return self._fail(log, f"run cost {total} exceeds max_run_cost "
                                           f"{budget.max_run_cost}")
            log.append(RunCompleted(run_id=run_id))
            return self.get_run(run_id)  # type: ignore[return-value]
        finally:
            log.close()
            pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- one step
    def _prepare(self, log: _Log, run_id: str, node: WorkflowNode, attempts: dict[str, int],
                 outputs: dict[str, dict], budget: Budget):
        """Resolve agent + executor, append step.started. Returns (node, req, executor)
        or an error string for definition problems that no retry can fix."""
        agent = self._store.get_agent(node.agent)
        if agent is None:
            return f"node {node.id!r} references unknown agent {node.agent!r}"
        executor = self._executors.get(agent.type.value)
        if executor is None:
            return f"no executor registered for agent type {agent.type.value!r}"

        upstream = {dep: outputs[dep] for dep in node.depends_on}
        key = idempotency_key(run_id, node.id, upstream)
        attempt = attempts.get(node.id, 0) + 1
        attempts[node.id] = attempt
        declared = frozenset(agent.declared_effects)
        log.append(StepStarted(
            run_id=run_id, step_id=node.id, attempt=attempt, agent=agent.name,
            idempotency_key=key, declared_effects=sorted(declared),
        ))
        deadline = (self._wall() + timedelta(seconds=budget.max_step_wall_seconds)
                    if budget.max_step_wall_seconds else None)
        req = StepRequest(
            run_id=run_id, step_id=node.id, attempt=attempt, idempotency_key=key,
            agent=agent, inputs=upstream, inputs_ref=self._blobs.put(_canonical(upstream)),
            declared_effects=declared, budget=budget, deadline=deadline,
        )
        return node, req, executor

    def _execute(self, log: _Log, req: StepRequest, executor: Executor,
                 heartbeat: Callable[[], bool] | None):
        """Runs in the pool. Gate, then dispatch. Returns one of:
        ("ok", result, elapsed) | ("refused", _Refused) | ("crashed", _StepCrashed)."""
        over = req.declared_effects - req.budget.allowed_effect_classes
        if over:
            ec = min(over)
            return ("refused", _Refused(f"agent {req.agent.name!r} declares effect {ec.value!r} "
                                        f"not allowed by the workflow budget", effect_class=ec))
        progress = self._progress_fn(log, req, heartbeat)
        t0 = self._clock()
        try:
            result = executor.execute(req, progress)
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 — step boundary; recorded, not raised
            return ("crashed", _StepCrashed(str(exc)))
        return ("ok", result, self._clock() - t0)

    def _settle(self, log: _Log, node: WorkflowNode, req: StepRequest, executor: Executor,
                outcome, budget: Budget):
        """Sequential: verify + record. Returns ("done", result) | ("retry", retry_at) |
        None when the step was dead-lettered and the run failed."""
        run_id, step_id, attempt = req.run_id, req.step_id, req.attempt
        try:
            if outcome[0] == "crashed":
                err: _StepCrashed = outcome[1]
                if attempt < node.retry.max_attempts:
                    retry_at = self._wall() + timedelta(seconds=node.retry.delay_before(attempt + 1))
                    log.append(StepFailed(run_id=run_id, step_id=step_id, attempt=attempt,
                                          error=err.error, terminal=False, retry_at=retry_at))
                    return ("retry", retry_at)
                log.append(StepFailed(run_id=run_id, step_id=step_id, attempt=attempt,
                                      error=err.error, terminal=True))
                raise _Refused(f"failed after {attempt} attempt(s): {err.error}")
            if outcome[0] == "refused":
                raise outcome[1]

            _, result, elapsed = outcome
            if not isinstance(result, StepResult):
                raise _Refused(f"executor {executor.name!r} returned "
                               f"{type(result).__name__}, not StepResult")
            undeclared = {e.effect_class for e in result.effects} - req.declared_effects
            if undeclared:
                ec = min(undeclared)
                raise _Refused(f"step reported undeclared effect {ec.value!r}",
                               effect_class=ec, cost=result.cost)
            if budget.max_step_cost is not None and \
                    result.cost.decimal() > Decimal(budget.max_step_cost):
                raise _Refused(f"step cost {result.cost.amount} {result.cost.currency} "
                               f"exceeds max_step_cost {budget.max_step_cost}", cost=result.cost)
            if budget.max_step_wall_seconds is not None and elapsed > budget.max_step_wall_seconds:
                raise _Refused(f"step took {elapsed:.2f}s, over max_step_wall_seconds "
                               f"{budget.max_step_wall_seconds}", cost=result.cost)
        except _Refused as r:
            log.append(StepDeadLettered(run_id=run_id, step_id=step_id, attempt=attempt,
                                        cause=r.cause, effect_class=r.effect_class, cost=r.cost))
            return ("dead", r.cause)

        ref = self._blobs.put(_canonical(result.output))
        self._faults.at(faults.BEFORE_EFFECT_COMMIT, run_id=run_id, step_id=step_id)
        log.append(StepCompleted(
            run_id=run_id, step_id=step_id, attempt=attempt, idempotency_key=req.idempotency_key,
            output_ref=ref, effects=result.effects, cost=result.cost, provenance=result.provenance,
        ))
        self._faults.at(faults.AFTER_EFFECT_COMMIT, run_id=run_id, step_id=step_id)
        return ("done", result)

    # ---------------------------------------------------------------- helpers
    def _progress_fn(self, log: _Log, req: StepRequest, heartbeat: Callable[[], bool] | None):
        """progress(fraction, note): renew the lease every call; append step.progress at
        most once per PROGRESS_MIN_INTERVAL so a chatty executor cannot flood the log."""
        last_emit = [-1e9]

        def progress(fraction: float, note: str = "") -> None:
            if heartbeat is not None and not heartbeat():
                raise LeaseLost(f"run {req.run_id!r}: lease lost during step {req.step_id!r}")
            now = self._clock()
            if now - last_emit[0] >= PROGRESS_MIN_INTERVAL:
                last_emit[0] = now
                log.append(StepProgress(
                    run_id=req.run_id, step_id=req.step_id, attempt=req.attempt,
                    fraction=max(0.0, min(1.0, float(fraction))), note=note[:200],
                ))
        return progress

    def _fail(self, log: _Log, error: str, *, step_id: str | None = None) -> WorkflowRun:
        try:
            log.append(RunFailed(run_id=log.run_id, error=error, step_id=step_id))
        except ConflictError:
            pass  # already failed by a concurrent settle; the log is the truth
        return self.get_run(log.run_id)  # type: ignore[return-value]


__all__ = ["Budget", "Engine", "LeaseLost", "RetryNotAllowed", "WorkflowDefinition",
           "idempotency_key"]
