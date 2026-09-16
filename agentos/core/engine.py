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
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from agentos.core import faults
from agentos.core.coordination import Lease
from agentos.core.events import (
    CONTROL_REQUEST_TYPES,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    Event,
    ExecutorSubstituted,
    RunCancelled,
    RunCancelRequested,
    RunCompleted,
    RunFailed,
    RunPaused,
    RunPauseRequested,
    RunResumed,
    RunStarted,
    RunSuspended,
    StepCancelled,
    StepCompleted,
    StepDeadLettered,
    StepFailed,
    StepProgress,
    StepRetryRequested,
    StepStarted,
)
from agentos.core.faults import FaultInjector, NoFaults
from agentos.core.fold import FoldError, fold, fold_from
from agentos.core.integrity import chain
from agentos.core.models import (
    HUMAN_ONLY_EFFECTS,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Budget,
    Cost,
    EffectClass,
    Principal,
    PrincipalKind,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowRun,
)
from agentos.core.ports import BlobStore, ConflictError, Executor, Observer, Store

TERMINAL = frozenset({"completed", "failed", "cancelled"})
PROGRESS_MIN_INTERVAL = 1.0   # seconds between step.progress events (rate limit)
_log = logging.getLogger("agentos.engine")


class LeaseLost(Exception):
    """The heartbeat reported the lease is gone; stop advancing immediately."""


class Cancelled(Exception):
    """Raised inside progress() when a cancel has been requested: the cooperative
    cancellation token. Executors that heartbeat are interrupted at their next call;
    executors that do not finish their step, and it is recorded before the run cancels."""


class RetryNotAllowed(Exception):
    """The step is not in a retryable state (not dead-lettered / failed)."""


class ControlNotAllowed(Exception):
    """cancel/pause/resume is not valid for the run's current state."""


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


RUN_INPUTS_KEY = "run"   # every step sees the run's inputs under this key (reserved node id)


def _agent_of(step, wf) -> str:
    for n in wf.nodes:
        if n.id == step.node_id:
            return n.agent
    return step.node_id


def resolve_model(executor: Executor, req: StepRequest) -> str | None:
    """§11 A3 hook. An executor MAY expose `resolve(req) -> str | None`: the concrete
    model id it would use for this request, with capability aliases already applied.
    Executors without it (echo, tool) resolve to nothing and never substitute. A
    resolver that raises is treated as "cannot resolve now" — the step still dispatches
    and the executor reports the real error at its own boundary."""
    fn = getattr(executor, "resolve", None)
    if fn is None:
        return None
    try:
        return fn(req)
    except Exception as exc:  # noqa: BLE001 — never let a plugin hook stop the engine
        _log.warning("executor %r resolve() failed: %s", executor.name, exc)
        return None


def idempotency_key(run_id: str, step_id: str, inputs: dict) -> str:
    return f"{run_id}:{step_id}:{hashlib.sha256(_canonical(inputs)).hexdigest()}"


def _notify(observers: Sequence[Observer], event: Event, run: WorkflowRun | None = None) -> None:
    """Fan a committed event out to observers. An observer must never break the engine:
    exceptions are logged and swallowed (telemetry is derived from the log and can be
    rebuilt)."""
    for obs in observers:
        try:
            obs.observe(event, run)
        except Exception:  # noqa: BLE001 — telemetry must not affect correctness
            _log.exception("observer %s failed on %s seq=%s", type(obs).__name__,
                           type(event).event_type, event.seq)


class _Log:
    """Serialized appender for one advance() call. Concurrent steps' progress events and
    the scheduler's own writes all go through here, so `expected_seq` is always right.

    Single-writer with one exception: the API may append a *control request*
    (run.cancel_requested / run.pause_requested) while we hold the lease. On a seq
    conflict we re-read; if every foreign event is a control request we adopt the new
    seq, remember the request, and retry once. Anything else is a real conflict."""

    def __init__(self, store: Store, run_id: str, last_seq: int, fence: int | None,
                 observers: Sequence[Observer] = (), last_hash: str | None = None) -> None:
        self._store, self.run_id, self.last_seq, self._fence = store, run_id, last_seq, fence
        self._observers = observers
        self.last_hash = last_hash            # tail of the tamper-evident chain (C12)
        self._lock = threading.Lock()
        self._closed = False
        self.cancel_requested = False
        self.pause_requested = False

    def append(self, event: Event) -> None:
        with self._lock:
            if self._closed:
                return  # advance() has returned; a straggler thread's progress is dropped
            try:
                committed = self._store.append_events(
                    self.run_id, self.last_seq, chain([event], self.last_seq, self.last_hash),
                    fence=self._fence)
            except ConflictError:
                if not self._adopt_control_events():
                    raise
                committed = self._store.append_events(
                    self.run_id, self.last_seq, chain([event], self.last_seq, self.last_hash),
                    fence=self._fence)
            self.last_seq += 1
            self.last_hash = committed[0].hash
        _notify(self._observers, committed[0])

    def poll_control(self) -> None:
        """Pick up control requests appended since our last write (no conflict needed)."""
        with self._lock:
            self._adopt_control_events()

    def _adopt_control_events(self) -> bool:
        fresh = self._store.read_events(self.run_id, after_seq=self.last_seq)
        if not fresh:
            return False
        if not all(isinstance(e, CONTROL_REQUEST_TYPES) for e in fresh):
            return False
        for e in fresh:
            if isinstance(e, RunCancelRequested):
                self.cancel_requested = True
            elif isinstance(e, RunPauseRequested):
                self.pause_requested = True
        self.last_seq = fresh[-1].seq
        self.last_hash = fresh[-1].hash
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True


class Engine:
    def __init__(self, store: Store, blobs: BlobStore, executors: Mapping[str, Executor],
                 faults: FaultInjector | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(UTC),
                 lease: Lease | None = None,
                 observers: Sequence[Observer] = (),
                 snapshot_every: int = 200) -> None:
        """`executors` maps an AgentType value (e.g. "echo") to the adapter that runs it.
        `clock` is monotonic (durations, rate limits); `wall` is the timestamp source for
        `retry_at` so tests can pin it. `lease` lets control requests be finalized
        immediately when no worker holds the run (idle/paused runs). `observers` receive
        every committed event (telemetry adapters). `snapshot_every` bounds replay cost
        (C15): once a run's log has grown that many events past its last snapshot, the
        folded state is stored and later reads fold only the tail; 0 disables."""
        self._store = store
        self._blobs = blobs
        self._executors = executors
        self._faults = faults or NoFaults()
        self._clock = clock
        self._wall = wall
        self._lease = lease
        self._observers = tuple(observers)
        self._snapshot_every = snapshot_every

    # ----------------------------------------------------------------- queries
    def get_run(self, run_id: str, *, hydrate: bool = True) -> WorkflowRun | None:
        run = self._fold_run(run_id)
        if run is None:
            return None
        if hydrate:
            for step in run.steps:
                if step.output_ref is not None:
                    step.output = json.loads(self._blobs.get(step.output_ref))
            if run.inputs_ref is not None:
                run.inputs = json.loads(self._blobs.get(run.inputs_ref))
        return run

    def _fold_run(self, run_id: str) -> WorkflowRun | None:
        """State = fold(log). With a snapshot (C15): state = fold_from(snapshot, tail),
        reading only the events after it. A snapshot that fails to chain to the tail, or
        whose state does not parse, is IGNORED and the whole log is folded instead — the
        log is the source of truth and a bad cache must never make a run unreadable."""
        snap = self._store.get_snapshot(run_id) if self._snapshot_every else None
        if snap is not None:
            seq, last_hash, state = snap
            try:
                base = WorkflowRun.model_validate(state)
                if base.last_seq == seq and base.last_hash == last_hash:
                    tail = self._store.read_events(run_id, after_seq=seq)
                    return fold_from(base, tail)
            except (FoldError, ValueError) as exc:
                _log.warning("run %s: snapshot at seq %s unusable (%s); folding the full log",
                             run_id, seq, exc)
        events = self._store.read_events(run_id)
        if not events:
            return None
        return fold(events)

    def _maybe_snapshot(self, run_id: str, last_seq: int) -> None:
        """Called when an advance() returns: if the log grew `snapshot_every` events past
        the last snapshot, store the folded (unhydrated) state at the current tail.
        Derived work: like observers, it must never fail an advance whose events are
        already committed, nor replace the exception that ended the advance."""
        if not self._snapshot_every:
            return
        try:
            snap = self._store.get_snapshot(run_id)
            if last_seq - (snap[0] if snap else 0) < self._snapshot_every:
                return
            run = self._fold_run(run_id)          # cheap: folds from the previous snapshot
            if run is not None:
                self._store.put_snapshot(run_id, run.last_seq, run.last_hash,
                                         run.model_dump(mode="json", exclude={"inputs"}))
        except Exception as exc:  # noqa: BLE001 — a cache write must not break the worker
            _log.warning("run %s: snapshot skipped (%s); the log is intact", run_id, exc)

    def is_terminal(self, run_id: str) -> bool:
        run = self.get_run(run_id, hydrate=False)
        return run is not None and run.status.value in TERMINAL

    def needs_worker(self, run_id: str) -> bool:
        """True when a worker should be advancing this run: not terminal, not paused,
        not suspended awaiting approval."""
        run = self.get_run(run_id, hydrate=False)
        return run is not None and run.status.value not in TERMINAL and \
            run.status not in (RunStatus.paused, RunStatus.suspended)

    def next_retry_delay(self, run: WorkflowRun) -> float | None:
        """Seconds until the earliest pending retry may start; None if none pending."""
        if not run.pending_retries:
            return None
        soonest = min(run.pending_retries.values())
        return max(0.0, (soonest - self._wall()).total_seconds())

    # ---------------------------------------------------------------- commands
    def create_run(self, wf_name: str, *, request_id: str | None = None,
                   principal: Principal | None = None, inputs: dict | None = None) -> str:
        """Append run.started and return the run id WITHOUT executing anything. The
        worker picks it up from the queue. Idempotent on request_id (DESIGN §6).
        `inputs` are stored by hash and handed to every step under the reserved key
        `run` (so a prompt template can say `{run.topic}`)."""
        wf = self._store.get_workflow(wf_name)
        if wf is None:
            raise KeyError(f"unknown workflow {wf_name!r}")
        if inputs and any(n.id == RUN_INPUTS_KEY for n in wf.nodes):
            raise ValueError(f"node id {RUN_INPUTS_KEY!r} is reserved for run inputs")
        request_id = request_id or uuid4().hex
        existing = self._store.run_id_for_request(request_id)
        if existing is not None:
            return existing
        run_id = uuid4().hex
        # Pin the current version of every agent the workflow references (DESIGN §5).
        # Missing agents are pinned as 0 so the failure surfaces at dispatch, in the log.
        pins: dict[str, int] = {}
        for node in wf.nodes:
            if node.agent not in pins:
                agent = self._store.get_agent(node.agent)
                pins[node.agent] = agent.version if agent is not None else 0
        inputs_ref = self._blobs.put(_canonical(inputs)) if inputs else None
        self._emit(run_id, 0, [RunStarted(
            run_id=run_id, workflow=wf.name, workflow_version=wf.version,
            request_id=request_id, principal=principal, agent_versions=pins,
            inputs_ref=inputs_ref,
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
        self._emit(run_id, run.last_seq, [StepRetryRequested(
            run_id=run_id, step_id=step_id, principal=principal, reason=reason,
        )])
        return self.get_run(run_id)  # type: ignore[return-value]

    # ------------------------------------------------------ operator control (C5)
    def request_cancel(self, run_id: str, *, principal: Principal | None = None,
                       reason: str = "") -> WorkflowRun:
        """Persist the intent. If no worker holds the run, finalize immediately; otherwise
        the worker finalizes at its next boundary (the in-flight step is interrupted at
        its next progress() call, or recorded if it finishes first — C4)."""
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if run.status.value in TERMINAL:
            raise ControlNotAllowed(f"run {run_id!r} is already {run.status.value}")
        if not run.cancel_requested:
            self._emit(run_id, run.last_seq, [RunCancelRequested(
                run_id=run_id, principal=principal, reason=reason)])
        self._finalize_if_idle(run_id)
        return self.get_run(run_id)  # type: ignore[return-value]

    def request_pause(self, run_id: str, *, principal: Principal | None = None,
                      reason: str = "") -> WorkflowRun:
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if run.status.value in TERMINAL or run.status is RunStatus.paused:
            raise ControlNotAllowed(f"run {run_id!r} is {run.status.value}")
        if not run.pause_requested:
            self._emit(run_id, run.last_seq, [RunPauseRequested(
                run_id=run_id, principal=principal, reason=reason)])
        self._finalize_if_idle(run_id)
        return self.get_run(run_id)  # type: ignore[return-value]

    def resume(self, run_id: str, *, principal: Principal | None = None,
               reason: str = "") -> WorkflowRun:
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if run.status is not RunStatus.paused:
            raise ControlNotAllowed(f"run {run_id!r} is {run.status.value}, not paused")
        self._emit(run_id, run.last_seq, [RunResumed(
            run_id=run_id, principal=principal, reason=reason)])
        return self.get_run(run_id)  # type: ignore[return-value]

    # ---------------------------------------------------- human-in-the-loop (C7)
    def approve(self, run_id: str, approval_id: str, *, principal: Principal,
                reason: str = "") -> WorkflowRun:
        """Grant a pending approval. The decision needs a principal; `spend` and
        `write_external` need a human unless the workflow allows agent approval (A2).
        When no approvals remain pending the run is running again; the caller re-enqueues.
        The gated step has never started, so it runs exactly once, after this event."""
        run, approval = self._pending_approval(run_id, approval_id)
        budget = self._budget_for(run)
        human_only = (set(approval.effect_classes) & HUMAN_ONLY_EFFECTS) or \
            approval.kind is ApprovalKind.cost           # money is human-only too
        if principal.kind is not PrincipalKind.human and human_only \
                and not budget.allow_agent_approval:
            what = ("a cost ceiling" if approval.kind is ApprovalKind.cost else
                    str(sorted(c.value for c in set(approval.effect_classes) & HUMAN_ONLY_EFFECTS)))
            raise ControlNotAllowed(
                f"approval {approval_id!r} covers {what} and requires a human principal; "
                f"got {principal.kind.value!r}")
        self._emit(run_id, run.last_seq, [ApprovalGranted(
            run_id=run_id, approval_id=approval_id, step_id=approval.step_id,
            principal=principal, reason=reason)])
        return self.get_run(run_id)  # type: ignore[return-value]

    def reject(self, run_id: str, approval_id: str, *, principal: Principal,
               reason: str = "") -> WorkflowRun:
        """Reject: the step is dead-lettered with the decider named and the run fails.
        `POST …/steps/{step}/retry` reopens it and the step re-requests approval."""
        run, approval = self._pending_approval(run_id, approval_id)
        who = f"{principal.kind.value}:{principal.id}"
        cause = f"approval rejected by {who}" + (f": {reason}" if reason else "")
        rejected = ApprovalRejected(run_id=run_id, approval_id=approval_id,
                                    step_id=approval.step_id, principal=principal, reason=reason)
        if approval.kind is ApprovalKind.cost:
            # The money is already spent and recorded; nothing to dead-letter or reopen.
            self._emit(run_id, run.last_seq, [rejected, RunFailed(
                run_id=run_id, error=f"cost ceiling {cause}", step_id=approval.step_id or None)])
        else:
            self._emit(run_id, run.last_seq, [
                rejected,
                StepDeadLettered(run_id=run_id, step_id=approval.step_id,
                                 attempt=run.attempts.get(approval.step_id, 0), cause=cause),
                RunFailed(run_id=run_id, error=f"step {approval.step_id!r} dead-lettered: {cause}",
                          step_id=approval.step_id),
            ])
        return self.get_run(run_id)  # type: ignore[return-value]

    def expire_approvals(self, run_ids: list[str] | None = None) -> list[tuple[str, str]]:
        """Reject every pending approval whose `expires_at` has passed, as the `system`
        principal. Called by the worker's sweep. Returns (run_id, approval_id) pairs."""
        now = self._wall()
        expired: list[tuple[str, str]] = []
        for run_id in run_ids if run_ids is not None else self._store.list_run_ids():
            run = self.get_run(run_id, hydrate=False)
            if run is None or run.status is not RunStatus.suspended:
                continue
            for a in list(run.approvals.values()):
                if a.status is ApprovalStatus.pending and a.expires_at and a.expires_at <= now:
                    self.reject(run_id, a.approval_id,
                                principal=Principal(kind=PrincipalKind.system, id="expiry"),
                                reason=f"expired at {a.expires_at.isoformat()}")
                    expired.append((run_id, a.approval_id))
                    break                                  # run is failed now
        return expired

    def _emit(self, run_id: str, expected_seq: int, events: list[Event]) -> None:
        """Command-path append: chain, commit, then fan out to observers."""
        prev = None
        if expected_seq > 0:
            tail = self._store.read_events(run_id, after_seq=expected_seq - 1)
            prev = tail[0].hash if tail else None
        committed = self._store.append_events(run_id, expected_seq,
                                              chain(events, expected_seq, prev))
        for ev in committed:
            _notify(self._observers, ev)

    def _pending_approval(self, run_id: str, approval_id: str):
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        approval = run.approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"unknown approval {approval_id!r} for run {run_id!r}")
        if run.status is not RunStatus.suspended or approval.status is not ApprovalStatus.pending:
            raise ControlNotAllowed(f"approval {approval_id!r} is {approval.status.value}; "
                                    f"run is {run.status.value}")
        return run, approval

    def _budget_for(self, run: WorkflowRun) -> Budget:
        wf = self._store.get_workflow(run.workflow)
        return wf.budget if wf is not None else Budget()

    def _finalize_if_idle(self, run_id: str) -> None:
        """Take the lease briefly; if we get it, nobody is executing, so apply the pending
        control request now. If we don't, a worker holds it and will apply it."""
        if self._lease is None:
            return
        token = self._lease.acquire(run_id, "control", 5.0)
        if token is None:
            return
        try:
            run = self.get_run(run_id, hydrate=False)
            if run is None or run.status.value in TERMINAL:
                return
            log = _Log(self._store, run_id, run.last_seq, token.fence, self._observers,
                       last_hash=run.last_hash)
            log.cancel_requested, log.pause_requested = run.cancel_requested, run.pause_requested
            self._apply_control(log, run.status)
        finally:
            self._lease.release(token)

    def _apply_control(self, log: _Log, status: RunStatus) -> bool:
        """Append the finalizing control event if one is pending. Returns True if the run
        should stop advancing."""
        if log.cancel_requested:
            log.append(RunCancelled(run_id=log.run_id))
            return True
        if log.pause_requested and status is not RunStatus.paused:
            log.append(RunPaused(run_id=log.run_id))
            return True
        return status is RunStatus.paused

    def start_run(self, wf_name: str, *, request_id: str | None = None,
                  principal: Principal | None = None, max_wait: float = 60.0,
                  inputs: dict | None = None) -> WorkflowRun:
        """create_run + advance in-process, sleeping through retry backoffs (bounded by
        `max_wait`). The synchronous path (tests, `?sync=true`)."""
        run_id = self.create_run(wf_name, request_id=request_id, principal=principal,
                                 inputs=inputs)
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

        log = _Log(self._store, run_id, run.last_seq, fence, self._observers,
                   last_hash=run.last_hash)
        log.cancel_requested, log.pause_requested = run.cancel_requested, run.pause_requested
        if run.status is RunStatus.paused and not run.cancel_requested:
            return run                       # nothing to do until run.resumed
        if run.status is RunStatus.suspended and not run.cancel_requested:
            return run                       # nothing to do until every approval is decided
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
        run_inputs = json.loads(self._blobs.get(run.inputs_ref)) if run.inputs_ref else None
        # §11 A3: the concrete model each agent last ran on in this run (substitutions
        # included), so a changed alias resolution is recorded before the step runs.
        models: dict[str, str] = {}
        for s in run.steps:
            if s.provenance is not None and s.provenance.model_id:
                models[_agent_of(s, wf)] = s.provenance.model_id
        for sub in run.substitutions:
            models[sub.agent] = sub.to_model
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
                # Control requests appended by the API since our last write (C5).
                log.poll_control()
                if self._apply_control(log, run.status):
                    return self.get_run(run_id)  # type: ignore[return-value]

                ready = [n for n in wf.nodes
                         if n.id not in done and all(d in done for d in n.depends_on)]
                now = self._wall()
                waiting = [n for n in ready if pending.get(n.id, now) > now]
                ready = [n for n in ready if n not in waiting]

                # ---- declare-then-do, tier 2 (C7): steps whose declared effects need a
                # decision are suspended BEFORE dispatch — no step.started, no attempt.
                runnable: list[tuple[WorkflowNode, str | None]] = []
                requested_now = False
                for node in ready:
                    gate = self._approval_gate(node, run, budget)
                    if gate == "run":
                        runnable.append((node, None))
                    elif gate == "pending":
                        pass                              # already asked; keep waiting
                    elif gate.startswith("granted:"):
                        runnable.append((node, gate.split(":", 1)[1]))
                    else:                                 # "request:<classes>"
                        classes = [EffectClass(c) for c in gate.split(":", 1)[1].split(",")]
                        approval_id = uuid4().hex
                        expires = (now + timedelta(seconds=budget.approval_timeout_seconds)
                                   if budget.approval_timeout_seconds else None)
                        log.append(ApprovalRequested(
                            run_id=run_id, approval_id=approval_id, step_id=node.id,
                            effect_classes=classes, expires_at=expires,
                            reason=f"step {node.id!r} declares "
                                   f"{', '.join(c.value for c in classes)}",
                        ))
                        run.approvals[approval_id] = Approval(
                            approval_id=approval_id, step_id=node.id, effect_classes=classes,
                            requested_at=now, expires_at=expires)
                        requested_now = True

                if not runnable:
                    if requested_now or any(a.status is ApprovalStatus.pending
                                            for a in run.approvals.values()):
                        log.append(RunSuspended(run_id=run_id))
                        return self.get_run(run_id)  # type: ignore[return-value]
                    # Everything runnable is waiting on a backoff. Hand control back;
                    # the caller re-enqueues with next_retry_delay().
                    return self.get_run(run_id)  # type: ignore[return-value]

                # ---- one wave: start all, execute concurrently, settle sequentially.
                started: list[tuple[WorkflowNode, StepRequest, Executor]] = []
                for node, approval_id in runnable:
                    prepared = self._prepare(log, run_id, node, attempts, outputs, budget,
                                             run.agent_versions, approval_id,
                                             run_inputs=run_inputs, models=models)
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
                interrupted = False
                for node, req, executor in started:
                    outcome = futures[req.step_id].result()   # re-raises Crash/LeaseLost
                    if outcome[0] == "cancelled":
                        log.append(StepCancelled(run_id=run_id, step_id=req.step_id,
                                                 attempt=req.attempt))
                        interrupted = True
                        continue
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
                # A cancel that arrived mid-wave: every finished sibling is now recorded
                # (C4); interrupted ones are step.cancelled; finalize.
                log.poll_control()
                if interrupted or log.cancel_requested:
                    log.cancel_requested = True
                    self._apply_control(log, run.status)
                    return self.get_run(run_id)  # type: ignore[return-value]
                if requested_now:
                    # Gated siblings were asked for while this wave ran; the wave is
                    # recorded, now suspend until they are decided.
                    log.append(RunSuspended(run_id=run_id))
                    return self.get_run(run_id)  # type: ignore[return-value]
                ceiling = Decimal(run.cost_ceiling) if run.cost_ceiling else (
                    Decimal(budget.max_run_cost) if budget.max_run_cost is not None else None)
                if ceiling is not None and total > ceiling:
                    # Budget guardrail (DESIGN §8): the tripping step is already recorded,
                    # so the charge is in the log; now SUSPEND for a cost approval whose
                    # grant raises the ceiling by one more budget's worth (C7 / A6).
                    tripping = started[-1][1].step_id if started else ""
                    approval_id = uuid4().hex
                    proposed = total + Decimal(budget.max_run_cost or "0")
                    expires = (now + timedelta(seconds=budget.approval_timeout_seconds)
                               if budget.approval_timeout_seconds else None)
                    log.append(ApprovalRequested(
                        run_id=run_id, approval_id=approval_id, step_id=tripping,
                        effect_classes=[], kind=ApprovalKind.cost,
                        cost_at_request=str(total), proposed_ceiling=str(proposed),
                        expires_at=expires,
                        reason=f"run cost {total} exceeds ceiling {ceiling}; approving raises "
                               f"the ceiling to {proposed}",
                    ))
                    log.append(RunSuspended(run_id=run_id))
                    return self.get_run(run_id)  # type: ignore[return-value]
                # No refold per wave: everything the next wave needs (attempts, outputs,
                # models, approvals) is tracked locally; a per-wave fold made a 1000-step
                # run O(n²) (found by the C15 acceptance test).
            log.append(RunCompleted(run_id=run_id))
            return self.get_run(run_id)  # type: ignore[return-value]
        finally:
            log.close()
            pool.shutdown(wait=False, cancel_futures=True)
            self._maybe_snapshot(run_id, log.last_seq)

    # ---------------------------------------------------------------- one step
    def _approval_gate(self, node: WorkflowNode, run: WorkflowRun, budget: Budget) -> str:
        """Tier-2 gate. Returns "run" (no approval needed), "pending" (asked, undecided),
        "granted:<approval_id>", or "request:<c1,c2>" (needs asking now). Steps with
        classes outside both tiers are left to the tier-3 refusal in _execute."""
        pinned = run.agent_versions.get(node.agent)
        agent = self._store.get_agent(node.agent, version=pinned) if pinned else \
            self._store.get_agent(node.agent)
        if agent is None:
            return "run"                                   # _prepare reports the error
        declared = set(agent.declared_effects)
        if declared - budget.allowed_effect_classes - budget.approval_required_for:
            return "run"                                   # tier 3: _execute refuses it
        needs = (declared - budget.allowed_effect_classes) & budget.approval_required_for
        if not needs:
            return "run"
        mine = [a for a in run.approvals.values()
                if a.step_id == node.id and a.kind is ApprovalKind.effect]
        for a in mine:
            if a.status is ApprovalStatus.granted:
                return f"granted:{a.approval_id}"
        if any(a.status is ApprovalStatus.pending for a in mine):
            return "pending"
        return "request:" + ",".join(sorted(c.value for c in needs))

    def _prepare(self, log: _Log, run_id: str, node: WorkflowNode, attempts: dict[str, int],
                 outputs: dict[str, dict], budget: Budget, pins: dict[str, int],
                 approval_id: str | None = None, *, run_inputs: dict | None = None,
                 models: dict[str, str] | None = None):
        """Resolve the PINNED agent version + executor, append step.started. Returns
        (node, req, executor) or an error string for definition problems that no retry
        can fix. Runs that predate pinning (empty pins) resolve the latest version."""
        pinned = pins.get(node.agent)
        agent = self._store.get_agent(node.agent, version=pinned) if pinned else \
            self._store.get_agent(node.agent)
        if agent is None:
            which = f" v{pinned}" if pinned else ""
            return f"node {node.id!r} references unknown agent {node.agent!r}{which}"
        exec_name = agent.executor or agent.type.value
        executor = self._executors.get(exec_name)
        if executor is None:
            have = ", ".join(sorted(self._executors)) or "none"
            hint = (" — install the provider distribution (e.g. `pip install "
                    "agentos-provider-openai-compat`) and restart the API and worker"
                    if agent.executor else "")
            return (f"agent {agent.name!r} needs executor {exec_name!r} but only "
                    f"[{have}] are registered{hint}")

        upstream = {dep: outputs[dep] for dep in node.depends_on}
        if run_inputs:
            upstream[RUN_INPUTS_KEY] = run_inputs
        key = idempotency_key(run_id, node.id, upstream)
        attempt = attempts.get(node.id, 0) + 1
        attempts[node.id] = attempt
        declared = frozenset(agent.declared_effects)
        deadline = (self._wall() + timedelta(seconds=budget.max_step_wall_seconds)
                    if budget.max_step_wall_seconds else None)
        req = StepRequest(
            run_id=run_id, step_id=node.id, attempt=attempt, idempotency_key=key,
            agent=agent, inputs=upstream, inputs_ref=self._blobs.put(_canonical(upstream)),
            declared_effects=declared, budget=budget, deadline=deadline,
            approval_id=approval_id,
        )
        # §11 A3: record a model substitution BEFORE the step starts, never silently.
        now_model = resolve_model(executor, req)
        if models is not None and now_model is not None:
            before = models.get(agent.name)
            if before is not None and before != now_model:
                log.append(ExecutorSubstituted(
                    run_id=run_id, step_id=node.id, agent=agent.name, executor=executor.name,
                    from_model=before, to_model=now_model,
                    reason=f"executor {executor.name!r} now resolves agent {agent.name!r} "
                           f"to {now_model!r} (was {before!r})",
                    principal=Principal(kind=PrincipalKind.system, id=executor.name),
                ))
            models[agent.name] = now_model
        log.append(StepStarted(
            run_id=run_id, step_id=node.id, attempt=attempt, agent=agent.name,
            idempotency_key=key, declared_effects=sorted(declared), agent_version=agent.version,
        ))
        return node, req, executor

    def _execute(self, log: _Log, req: StepRequest, executor: Executor,
                 heartbeat: Callable[[], bool] | None):
        """Runs in the pool. Gate, then dispatch. Returns one of:
        ("ok", result, elapsed) | ("refused", _Refused) | ("crashed", _StepCrashed)."""
        over = req.declared_effects - req.budget.allowed_effect_classes
        if req.approval_id is not None:
            over -= req.budget.approval_required_for      # granted: tier 2 is now allowed
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
        except Cancelled:
            return ("cancelled",)
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
        most once per PROGRESS_MIN_INTERVAL so a chatty executor cannot flood the log.
        Also the cooperative cancellation token: raises Cancelled once a cancel request
        has been seen (checked on the same rate limit)."""
        last_emit = [-1e9]

        def progress(fraction: float, note: str = "") -> None:
            if heartbeat is not None and not heartbeat():
                raise LeaseLost(f"run {req.run_id!r}: lease lost during step {req.step_id!r}")
            if log.cancel_requested:
                raise Cancelled(f"run {req.run_id!r}: cancel requested")
            now = self._clock()
            if now - last_emit[0] >= PROGRESS_MIN_INTERVAL:
                last_emit[0] = now
                log.poll_control()
                if log.cancel_requested:
                    raise Cancelled(f"run {req.run_id!r}: cancel requested")
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


__all__ = ["Budget", "Cancelled", "ControlNotAllowed", "Engine", "LeaseLost", "RetryNotAllowed",
           "WorkflowDefinition",
           "idempotency_key"]
