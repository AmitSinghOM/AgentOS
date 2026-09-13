"""Workflow engine.

Every state change is an appended event; run state is only ever the fold of the log; a
step that already has `step.completed` is replayed from the log, never re-executed (C1).
`advance()` is re-entrant and is what the worker calls under a fenced lease (C6).

Phase 2: the engine is also the **governor** (docs/DEVELOPMENT_STRUCTURE.md §2.1, §11):
  gate     — an agent whose DECLARED effects exceed the workflow budget is refused before
             dispatch (declare-then-do, A1);
  dispatch — the executor gets a StepRequest and a progress() callback that renews the
             lease (A5);
  verify   — reported effects must be within the declaration; cost and wall time must be
             within budget (A6); violations dead-letter the step (C11);
  record   — step.completed carries effects, cost and provenance; the run's rolling cost
             is checked against the run ceiling.

The engine depends only on the ports in `agentos.core`.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
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
    WorkflowRun,
)
from agentos.core.ports import BlobStore, Executor, Store

TERMINAL = frozenset({"completed", "failed"})
PROGRESS_MIN_INTERVAL = 1.0   # seconds between step.progress events (rate limit)


class LeaseLost(Exception):
    """The heartbeat reported the lease is gone; stop advancing immediately."""


class _Refused(Exception):
    """Internal: the governor refused a step. Carries the dead-letter cause."""

    def __init__(self, cause: str, *, effect_class: EffectClass | None = None,
                 cost: Cost | None = None) -> None:
        super().__init__(cause)
        self.cause, self.effect_class, self.cost = cause, effect_class, cost or Cost()


def _canonical(obj: dict) -> bytes:
    """Stable JSON bytes: sorted keys, no whitespace. Same inputs → same hash → same
    idempotency key across processes and Python versions."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def idempotency_key(run_id: str, step_id: str, inputs: dict) -> str:
    return f"{run_id}:{step_id}:{hashlib.sha256(_canonical(inputs)).hexdigest()}"


class Engine:
    def __init__(self, store: Store, blobs: BlobStore, executors: Mapping[str, Executor],
                 faults: FaultInjector | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """`executors` maps an AgentType value (e.g. "echo") to the adapter that runs it."""
        self._store = store
        self._blobs = blobs
        self._executors = executors
        self._faults = faults or NoFaults()
        self._clock = clock

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

    def start_run(self, wf_name: str, *, request_id: str | None = None,
                  principal: Principal | None = None) -> WorkflowRun:
        """create_run + advance in-process. The synchronous path (tests, `?sync=true`)."""
        run_id = self.create_run(wf_name, request_id=request_id, principal=principal)
        return self.advance(run_id)

    def advance(self, run_id: str, *, fence: int | None = None,
                heartbeat: Callable[[], bool] | None = None) -> WorkflowRun:
        """Drive a run from its current log position to a terminal state.

        Re-entrant by construction: it re-derives everything from the log, skips steps
        that already completed, and appends with optimistic concurrency + the caller's
        lease fence, so calling it twice — or from two processes — cannot double-execute
        a step. `heartbeat()` is called before each step and on every progress() call;
        returning False means the lease is lost and advancing stops (LeaseLost)."""
        run = self.get_run(run_id, hydrate=False)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        if run.status.value in TERMINAL:
            return self.get_run(run_id)  # type: ignore[return-value]

        wf = self._store.get_workflow(run.workflow)
        if wf is None:
            return self._fail(run, fence, f"workflow {run.workflow!r} no longer exists")
        if wf.version != run.workflow_version:
            # C3: never resume a run against a definition it did not start with.
            return self._fail(run, fence, f"workflow {wf.name!r} is v{wf.version} but run "
                                          f"pinned v{run.workflow_version}")

        by_id = {n.id: n for n in wf.nodes}
        done = {s.node_id: s for s in run.steps}
        outputs: dict[str, dict] = {
            sid: json.loads(self._blobs.get(s.output_ref))
            for sid, s in done.items() if s.output_ref is not None
        }
        budget = wf.budget

        for step_id in wf.topological_order():
            if step_id in done:
                continue  # replayed from the log — the C1 invariant in one line
            if heartbeat is not None and not heartbeat():
                raise LeaseLost(f"run {run_id!r}: lease lost before step {step_id!r}")

            node = by_id[step_id]
            upstream = {dep: outputs[dep] for dep in node.depends_on}
            key = idempotency_key(run_id, step_id, upstream)
            attempt = run.attempts.get(step_id, 0) + 1

            agent = self._store.get_agent(node.agent)
            if agent is None:
                return self._fail(run, fence, f"node {step_id!r} references unknown agent "
                                              f"{node.agent!r}", step_id=step_id)
            executor = self._executors.get(agent.type.value)
            if executor is None:
                return self._fail(run, fence, f"no executor registered for agent type "
                                              f"{agent.type.value!r}", step_id=step_id)

            declared = frozenset(agent.declared_effects)
            run = self._append(run, fence, StepStarted(
                run_id=run_id, step_id=step_id, attempt=attempt, agent=agent.name,
                idempotency_key=key, declared_effects=sorted(declared),
            ))

            try:
                # ---- gate: declare-then-do. Refuse BEFORE the executor runs.
                over = declared - budget.allowed_effect_classes
                if over:
                    ec = min(over)
                    raise _Refused(f"agent {agent.name!r} declares effect {ec.value!r} "
                                   f"not allowed by the workflow budget", effect_class=ec)

                # ---- dispatch
                inputs_ref = self._blobs.put(_canonical(upstream))
                deadline = (datetime.now(UTC) + timedelta(seconds=budget.max_step_wall_seconds)
                            if budget.max_step_wall_seconds else None)
                req = StepRequest(
                    run_id=run_id, step_id=step_id, attempt=attempt, idempotency_key=key,
                    agent=agent, inputs=upstream, inputs_ref=inputs_ref,
                    declared_effects=declared, budget=budget, deadline=deadline,
                )
                progress = self._progress_fn(run_id, step_id, attempt, fence, heartbeat)
                t0 = self._clock()
                try:
                    result = executor.execute(req, progress)
                except LeaseLost:
                    raise
                except Exception as exc:  # noqa: BLE001 — step boundary; recorded, not raised
                    run = self.get_run(run_id, hydrate=False)  # type: ignore[assignment]
                    run = self._append(run, fence, StepFailed(
                        run_id=run_id, step_id=step_id, attempt=attempt, error=str(exc),
                    ))
                    return self._fail(run, fence, f"step {step_id!r} failed: {exc}",
                                      step_id=step_id)
                elapsed = self._clock() - t0
                # progress() may have appended step.progress events; re-read so our
                # expected_seq for the next append is current.
                run = self.get_run(run_id, hydrate=False)  # type: ignore[assignment]
                if not isinstance(result, StepResult):
                    raise _Refused(f"executor {executor.name!r} returned "
                                   f"{type(result).__name__}, not StepResult")

                # ---- verify: the core trusts nothing the executor says about limits.
                undeclared = {e.effect_class for e in result.effects} - declared
                if undeclared:
                    ec = min(undeclared)
                    raise _Refused(f"step reported undeclared effect {ec.value!r}",
                                   effect_class=ec, cost=result.cost)
                if budget.max_step_cost is not None and \
                        result.cost.decimal() > Decimal(budget.max_step_cost):
                    raise _Refused(f"step cost {result.cost.amount} {result.cost.currency} "
                                   f"exceeds max_step_cost {budget.max_step_cost}",
                                   cost=result.cost)
                if budget.max_step_wall_seconds is not None and \
                        elapsed > budget.max_step_wall_seconds:
                    raise _Refused(f"step took {elapsed:.2f}s, over max_step_wall_seconds "
                                   f"{budget.max_step_wall_seconds}", cost=result.cost)
            except _Refused as r:
                run = self._append(run, fence, StepDeadLettered(
                    run_id=run_id, step_id=step_id, attempt=attempt, cause=r.cause,
                    effect_class=r.effect_class, cost=r.cost,
                ))
                return self._fail(run, fence, f"step {step_id!r} dead-lettered: {r.cause}",
                                  step_id=step_id)

            # ---- record
            ref = self._blobs.put(_canonical(result.output))
            self._faults.at(faults.BEFORE_EFFECT_COMMIT, run_id=run_id, step_id=step_id)
            run = self._append(run, fence, StepCompleted(
                run_id=run_id, step_id=step_id, attempt=attempt, idempotency_key=key,
                output_ref=ref, effects=result.effects, cost=result.cost,
                provenance=result.provenance,
            ))
            self._faults.at(faults.AFTER_EFFECT_COMMIT, run_id=run_id, step_id=step_id)
            outputs[step_id] = result.output
            done[step_id] = run.steps[-1]

            # Run ceiling: checked after recording, so the cost that tripped it is in
            # the log. Phase 3 turns this into SUSPENDED-for-approval.
            if budget.max_run_cost is not None and \
                    Decimal(run.total_cost) > Decimal(budget.max_run_cost):
                return self._fail(run, fence, f"run cost {run.total_cost} exceeds "
                                              f"max_run_cost {budget.max_run_cost}",
                                  step_id=step_id)

        self._append(run, fence, RunCompleted(run_id=run_id))
        return self.get_run(run_id)  # type: ignore[return-value]

    # ---------------------------------------------------------------- helpers
    def _progress_fn(self, run_id: str, step_id: str, attempt: int, fence: int | None,
                     heartbeat: Callable[[], bool] | None):
        """progress(fraction, note): renew the lease every call; append step.progress at
        most once per PROGRESS_MIN_INTERVAL so a chatty executor cannot flood the log."""
        last_emit = [-1e9]

        def progress(fraction: float, note: str = "") -> None:
            if heartbeat is not None and not heartbeat():
                raise LeaseLost(f"run {run_id!r}: lease lost during step {step_id!r}")
            now = self._clock()
            if now - last_emit[0] >= PROGRESS_MIN_INTERVAL:
                last_emit[0] = now
                run = self.get_run(run_id, hydrate=False)
                if run is not None:
                    self._append(run, fence, StepProgress(
                        run_id=run_id, step_id=step_id, attempt=attempt,
                        fraction=max(0.0, min(1.0, float(fraction))), note=note[:200],
                    ))
        return progress

    def _append(self, run: WorkflowRun, fence: int | None, event: Event) -> WorkflowRun:
        self._store.append_events(run.id, run.last_seq, [event], fence=fence)
        return self.get_run(run.id, hydrate=False)  # type: ignore[return-value]

    def _fail(self, run: WorkflowRun, fence: int | None, error: str, *,
              step_id: str | None = None) -> WorkflowRun:
        self._append(run, fence, RunFailed(run_id=run.id, error=error, step_id=step_id))
        return self.get_run(run.id)  # type: ignore[return-value]


__all__ = ["Budget", "Engine", "LeaseLost", "WorkflowDefinition", "idempotency_key"]
