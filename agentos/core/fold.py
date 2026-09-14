"""Fold: events → run state. Pure, deterministic, no I/O.

`fold(events)` is the single definition of "what state is this run in". The API, the
worker, the chaos suite and the golden corpus all use it; nothing else derives status.
Outputs are referenced by BlobRef only — hydrating bytes is the caller's job — so the
fold never needs a blob store and replay cost is independent of payload size.
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from agentos.core.events import (
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    Event,
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
from agentos.core.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    RunStatus,
    StepRecord,
    WorkflowRun,
)


class FoldError(ValueError):
    """The log is not a valid run history (gap in seq, missing run.started, …)."""


def fold(events: Iterable[Event]) -> WorkflowRun:
    events = list(events)
    if not events:
        raise FoldError("empty log")
    first = events[0]
    if not isinstance(first, RunStarted):
        raise FoldError(f"log must begin with run.started, got {type(first).event_type}")

    run = WorkflowRun(
        id=first.run_id,
        workflow=first.workflow,
        workflow_version=first.workflow_version,
        request_id=first.request_id,
        agent_versions=dict(first.agent_versions),
        status=RunStatus.running,
        started_at=first.occurred_at,
        parent_run_id=first.parent_run_id,
        last_seq=first.seq,
    )
    completed: dict[str, StepRecord] = {}
    order: list[str] = []
    total = Decimal(0)
    expected_seq = first.seq

    for ev in events[1:]:
        expected_seq += 1
        if ev.seq != expected_seq:
            raise FoldError(f"seq gap: expected {expected_seq}, got {ev.seq}")
        if ev.run_id != run.id:
            raise FoldError(f"event for run {ev.run_id!r} in log of {run.id!r}")
        run.last_seq = ev.seq

        if isinstance(ev, StepStarted):
            run.attempts[ev.step_id] = ev.attempt
            run.pending_retries.pop(ev.step_id, None)
        elif isinstance(ev, StepProgress):
            run.progress[ev.step_id] = ev.fraction
        elif isinstance(ev, StepCompleted):
            if ev.step_id in completed:
                # Exactly-once invariant: a second completion for the same step is a bug
                # in the writer, never something the fold should paper over.
                raise FoldError(f"step {ev.step_id!r} completed twice")
            completed[ev.step_id] = StepRecord(
                node_id=ev.step_id,
                attempt=ev.attempt,
                output_ref=ev.output_ref,
                effects=ev.effects,
                cost=ev.cost,
                provenance=ev.provenance,
                finished_at=ev.occurred_at,
            )
            order.append(ev.step_id)
            run.progress[ev.step_id] = 1.0
            total += ev.cost.decimal()
        elif isinstance(ev, StepDeadLettered):
            run.dead_lettered[ev.step_id] = ev.cause
            total += ev.cost.decimal()
        elif isinstance(ev, StepFailed):
            if ev.terminal:
                run.failed_steps[ev.step_id] = ev.error
                run.error = f"step {ev.step_id!r}: {ev.error}"
            elif ev.retry_at is not None:
                run.pending_retries[ev.step_id] = ev.retry_at
        elif isinstance(ev, StepRetryRequested):
            # Reopen: the run is running again and the step may be attempted once more.
            run.status = RunStatus.running
            run.error = None
            run.ended_at = None
            run.dead_lettered.pop(ev.step_id, None)
            run.failed_steps.pop(ev.step_id, None)
            run.pending_retries.pop(ev.step_id, None)
        elif isinstance(ev, RunCompleted):
            run.status = RunStatus.completed
            run.ended_at = ev.occurred_at
        elif isinstance(ev, RunFailed):
            run.status = RunStatus.failed
            run.error = ev.error
            run.ended_at = ev.occurred_at
        elif isinstance(ev, RunCancelRequested):
            run.cancel_requested = True
        elif isinstance(ev, StepCancelled):
            run.cancelled_steps.append(ev.step_id)
            run.pending_retries.pop(ev.step_id, None)
        elif isinstance(ev, RunCancelled):
            run.status = RunStatus.cancelled
            run.cancel_requested = False
            run.pause_requested = False
            run.ended_at = ev.occurred_at
        elif isinstance(ev, RunPauseRequested):
            run.pause_requested = True
        elif isinstance(ev, RunPaused):
            run.status = RunStatus.paused
            run.pause_requested = False
        elif isinstance(ev, RunResumed):
            run.status = RunStatus.running
        elif isinstance(ev, ApprovalRequested):
            run.approvals[ev.approval_id] = Approval(
                approval_id=ev.approval_id, step_id=ev.step_id,
                effect_classes=ev.effect_classes, reason=ev.reason,
                requested_at=ev.occurred_at, expires_at=ev.expires_at,
                kind=ev.kind, cost_at_request=ev.cost_at_request,
                proposed_ceiling=ev.proposed_ceiling,
            )
        elif isinstance(ev, RunSuspended):
            run.status = RunStatus.suspended
        elif isinstance(ev, ApprovalGranted):
            a = run.approvals[ev.approval_id]
            a.status, a.decided_by, a.decision_reason, a.decided_at = (
                ApprovalStatus.granted, ev.principal, ev.reason, ev.occurred_at)
            if a.kind is ApprovalKind.cost and a.proposed_ceiling is not None:
                run.cost_ceiling = a.proposed_ceiling      # the grant raises the ceiling
            if run.status is RunStatus.suspended and not any(
                    x.status is ApprovalStatus.pending for x in run.approvals.values()):
                run.status = RunStatus.running
        elif isinstance(ev, ApprovalRejected):
            a = run.approvals[ev.approval_id]
            a.status, a.decided_by, a.decision_reason, a.decided_at = (
                ApprovalStatus.rejected, ev.principal, ev.reason, ev.occurred_at)
        elif isinstance(ev, RunStarted):
            raise FoldError("run.started appears twice")

    run.steps = [completed[s] for s in order]
    run.total_cost = str(total)
    return run
