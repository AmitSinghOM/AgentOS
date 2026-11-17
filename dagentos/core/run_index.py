"""Run index (audit E-07): the derived row each store keeps beside a run's log so that
"what waits on me / what failed" is one indexed query instead of one fold per run.

The log is the truth; this row is a rebuildable index of it. Nothing that decides anything
reads it: the fold never does (replay stays deterministic), and every API view, the
approvals inbox and the worker's sweep re-fold each run the index points them at and act on
the FOLD's status. The index only chooses which runs to fold.

`index_events` is the one place the row is computed. Every adapter calls it inside the
transaction that appends the events, so the row can never disagree with the fold at a
commit boundary, and the same function rebuilds a row from a whole log (migration 4's
backfill, or a row a pre-migration-4 process left behind). It mirrors exactly the status
transitions in `dagentos.core.fold._apply`; tests/test_run_index.py pins the two equal over
the golden corpus at every cut point and over a seeded store on every adapter, and
`agentos doctor` compares the stored rows with the fold on a live store.

It must never raise: it runs inside every append, so an exception here would fail the
append. Unknown or out-of-order events leave the row as it was.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from dagentos.core.events import (
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    Event,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunPaused,
    RunResumed,
    RunSuspended,
    StepRetryRequested,
)
from dagentos.core.models import RunStatus

#: Statuses a worker must advance (`Engine.needs_worker`): not terminal, not paused, not
#: suspended. The sweep asks the index for these and still re-checks each against the fold.
NEEDS_WORKER = (RunStatus.pending, RunStatus.running)


@dataclass(frozen=True)
class RunIndex:
    """The derived row. `pending_approvals` is carried only so an `approval.granted` can tell
    whether it was the last pending one (the fold's suspended → running rule); `indexed_seq`
    is the last log seq the row reflects, so a row behind its log is detectable and caught up."""

    status: RunStatus = RunStatus.running
    pending_approvals: tuple[str, ...] = ()
    indexed_seq: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RunSummary:
    """One run as the index sees it. `status` is None for a row not indexed yet."""

    run_id: str
    workflow: str
    status: RunStatus | None
    created_at: datetime
    updated_at: datetime | None


@dataclass(frozen=True)
class RunPage:
    """Newest-created first. `next_cursor` is None on the last page."""

    runs: list[RunSummary] = field(default_factory=list)
    next_cursor: str | None = None


def index_events(prev: RunIndex | None, events: Iterable[Event]) -> RunIndex:
    """Fold `events` (in seq order, after `prev.indexed_seq`) into the index row. `prev` None
    means the log starts here: a run is `running` from `run.started`, as in the fold."""
    status = prev.status if prev is not None else RunStatus.running
    pending = list(prev.pending_approvals) if prev is not None else []
    seq = prev.indexed_seq if prev is not None else 0
    updated = prev.updated_at if prev is not None else None
    for ev in events:
        seq, updated = ev.seq, ev.occurred_at
        if isinstance(ev, (StepRetryRequested, RunResumed)):
            status = RunStatus.running
        elif isinstance(ev, RunCompleted):
            status = RunStatus.completed
        elif isinstance(ev, RunFailed):
            status = RunStatus.failed
        elif isinstance(ev, RunCancelled):
            status = RunStatus.cancelled
        elif isinstance(ev, RunPaused):
            status = RunStatus.paused
        elif isinstance(ev, RunSuspended):
            status = RunStatus.suspended
        elif isinstance(ev, ApprovalRequested):
            if ev.approval_id not in pending:
                pending.append(ev.approval_id)
        elif isinstance(ev, (ApprovalGranted, ApprovalRejected)):
            if ev.approval_id in pending:
                pending.remove(ev.approval_id)
            if isinstance(ev, ApprovalGranted) and status is RunStatus.suspended and not pending:
                status = RunStatus.running
    return RunIndex(status=status, pending_approvals=tuple(sorted(pending)), indexed_seq=seq,
                    updated_at=updated)


def encode_cursor(sort_key: str, run_id: str) -> str:
    """Opaque page cursor: the last row's (created_at as the adapter stores it, run_id).
    Created-at, not updated-at: a run's creation never moves, so a run that changes status
    between two page requests can neither appear twice nor be skipped."""
    raw = json.dumps([sort_key, run_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Inverse of `encode_cursor`; ValueError on anything it did not produce."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        key, run_id = json.loads(raw)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError(f"invalid cursor {cursor!r}") from exc
    if not isinstance(key, str) or not isinstance(run_id, str):
        raise ValueError(f"invalid cursor {cursor!r}")
    try:
        datetime.fromisoformat(key)
    except ValueError as exc:
        raise ValueError(f"invalid cursor {cursor!r}") from exc
    return key, run_id
