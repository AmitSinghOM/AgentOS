"""Fold: events → run state. Pure, deterministic, no I/O.

`fold(events)` is the single definition of "what state is this run in". The API, the
worker, the chaos suite and the golden corpus all use it; nothing else derives status.
Outputs are referenced by BlobRef only — hydrating bytes is the caller's job — so the
fold never needs a blob store and replay cost is independent of payload size.
"""
from __future__ import annotations

from collections.abc import Iterable

from agentos.core.events import (
    Event,
    RunCompleted,
    RunFailed,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepStarted,
)
from agentos.core.models import RunStatus, StepResult, WorkflowRun


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
        status=RunStatus.running,
        started_at=first.occurred_at,
        parent_run_id=first.parent_run_id,
        last_seq=first.seq,
    )
    completed: dict[str, StepResult] = {}
    order: list[str] = []
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
        elif isinstance(ev, StepCompleted):
            if ev.step_id in completed:
                # Exactly-once invariant: a second completion for the same step is a bug
                # in the writer, never something the fold should paper over.
                raise FoldError(f"step {ev.step_id!r} completed twice")
            completed[ev.step_id] = StepResult(
                node_id=ev.step_id,
                attempt=ev.attempt,
                output_ref=ev.output_ref,
                finished_at=ev.occurred_at,
            )
            order.append(ev.step_id)
        elif isinstance(ev, StepFailed):
            if ev.terminal:
                run.error = f"step {ev.step_id!r}: {ev.error}"
        elif isinstance(ev, RunCompleted):
            run.status = RunStatus.completed
            run.ended_at = ev.occurred_at
        elif isinstance(ev, RunFailed):
            run.status = RunStatus.failed
            run.error = ev.error
            run.ended_at = ev.occurred_at
        elif isinstance(ev, RunStarted):
            raise FoldError("run.started appears twice")

    run.steps = [completed[s] for s in order]
    return run
