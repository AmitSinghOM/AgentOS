"""Run events — the public API of AgentOS (docs/DEVELOPMENT_STRUCTURE.md §3).

Rules that keep a 2026 log readable in 2033:
- every event carries `event_type` and `schema_version`;
- events are append-only and immutable; fields are only ever ADDED; a new meaning is a
  new event type, never a re-typed field;
- serialization is plain JSON with explicit field names; reading an older
  `schema_version` goes through `agentos.core.upcast`.

`seq` is assigned by the store on append and is dense and monotonic per run.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from agentos.core.models import (
    ApprovalKind,
    BlobRef,
    Cost,
    Effect,
    EffectClass,
    Principal,
    Provenance,
)

CURRENT_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Envelope shared by every event. Concrete types add payload fields."""

    event_type: ClassVar[str]

    run_id: str
    seq: int = 0                          # 0 = not yet appended; store assigns 1..n
    schema_version: int = CURRENT_SCHEMA_VERSION
    occurred_at: datetime = Field(default_factory=_now)
    parent_run_id: str | None = None      # dynamic DAGs (§2.3): child runs point at parents
    # v0.6.0, additive: tamper-evident chain (agentos.core.integrity, C12). None on logs
    # written before; the engine stamps both on every append.
    prev_hash: str | None = None
    hash: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Flat JSON-safe dict: envelope + payload. The store persists exactly this."""
        data = self.model_dump(mode="json")
        data["event_type"] = type(self).event_type
        return data


class RunStarted(Event):
    event_type: ClassVar[str] = "run.started"
    workflow: str
    workflow_version: int
    request_id: str                       # client-supplied; run start is idempotent on it
    principal: Principal | None = None
    agent_versions: dict[str, int] = Field(default_factory=dict)  # name → pinned version
    inputs_ref: BlobRef | None = None     # v0.5.0, additive: run inputs, by hash (§11 A4)


class StepStarted(Event):
    event_type: ClassVar[str] = "step.started"
    step_id: str
    attempt: int
    agent: str
    idempotency_key: str                  # run_id:step_id:sha256(inputs)
    declared_effects: list[EffectClass] = Field(default_factory=list)
    agent_version: int = 1                # the pinned definition this attempt ran against


class StepCompleted(Event):
    event_type: ClassVar[str] = "step.completed"
    step_id: str
    attempt: int
    idempotency_key: str
    output_ref: BlobRef                   # bytes live in the BlobStore (§11 A4)
    # Added in v0.3.0 (additive; older logs default these — see tests/golden/v0.2.0.json)
    effects: list[Effect] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    provenance: Provenance | None = None


class StepProgress(Event):
    """Heartbeat with a human-readable position, rate-limited by the engine (§11 A5).
    Also renews the lease, so an hour-long step is not mistaken for a dead worker."""

    event_type: ClassVar[str] = "step.progress"
    step_id: str
    attempt: int
    fraction: float                       # 0..1
    note: str = ""


class StepDeadLettered(Event):
    """Poison step (C11 / §11 A1): the step will not be retried; the run fails with the
    cause in the log. `POST /runs/{id}/steps/{step}/retry` (later) replays from here."""

    event_type: ClassVar[str] = "step.dead_lettered"
    step_id: str
    attempt: int
    cause: str
    effect_class: EffectClass | None = None   # set when the cause is an undeclared effect
    cost: Cost = Field(default_factory=Cost)  # what it cost before it was refused


class StepFailed(Event):
    event_type: ClassVar[str] = "step.failed"
    step_id: str
    attempt: int
    error: str
    terminal: bool = True                 # False while retries remain
    retry_at: datetime | None = None      # when the next attempt may start (terminal=False)


class StepRetryRequested(Event):
    """A principal asked for a dead-lettered or failed step to be attempted again (C11).
    Reopens the run: status back to running, the step's dead-letter cleared, and the
    next attempt number continues from where it left off."""

    event_type: ClassVar[str] = "step.retry_requested"
    step_id: str
    principal: Principal | None = None
    reason: str = ""


class RunCompleted(Event):
    event_type: ClassVar[str] = "run.completed"


class RunFailed(Event):
    event_type: ClassVar[str] = "run.failed"
    error: str
    step_id: str | None = None


# ---- operator control (C5). Requests are persisted intent; the worker (or, for an
# idle run, the API under a lease) appends the finalizing event at the next boundary.

class ControlEvent(Event):
    principal: Principal | None = None
    reason: str = ""


class RunCancelRequested(ControlEvent):
    event_type: ClassVar[str] = "run.cancel_requested"


class RunCancelled(Event):
    """Terminal. Every step that completed before this point is in the log (C4)."""
    event_type: ClassVar[str] = "run.cancelled"


class StepCancelled(Event):
    """A step interrupted cooperatively (its progress() raised Cancelled)."""
    event_type: ClassVar[str] = "step.cancelled"
    step_id: str
    attempt: int


class RunPauseRequested(ControlEvent):
    event_type: ClassVar[str] = "run.pause_requested"


class RunPaused(Event):
    """Not terminal. The current wave finished and was recorded; nothing new starts
    until run.resumed. The lease is released; the run leaves the queue."""
    event_type: ClassVar[str] = "run.paused"


class RunResumed(ControlEvent):
    event_type: ClassVar[str] = "run.resumed"


# ---- human-in-the-loop (C7, DESIGN §4.4). Approval is a run state, not a library
# pattern: the gated step has NOT started when approval.requested is appended, and its
# step.started must have a higher seq than approval.granted.

class ApprovalRequested(Event):
    event_type: ClassVar[str] = "approval.requested"
    approval_id: str
    step_id: str
    effect_classes: list[EffectClass]     # the declared classes that need a decision
    reason: str = ""
    expires_at: datetime | None = None
    # v0.4.0, additive: cost-ceiling approvals (DESIGN §8 budget guardrails)
    kind: ApprovalKind = ApprovalKind.effect
    cost_at_request: str | None = None
    proposed_ceiling: str | None = None


class RunSuspended(Event):
    """Not terminal. Lease released, run leaves the queue, no resources held while a
    human decides — hours or days (DESIGN §4.4)."""
    event_type: ClassVar[str] = "run.suspended"


class ApprovalGranted(ControlEvent):
    event_type: ClassVar[str] = "approval.granted"
    approval_id: str
    step_id: str


class ApprovalRejected(ControlEvent):
    event_type: ClassVar[str] = "approval.rejected"
    approval_id: str
    step_id: str


class ExecutorSubstituted(Event):
    """§11 A3: appended BEFORE `step.started` when the concrete model an executor resolves
    for this agent differs from the one the same agent used earlier in this run (alias
    re-pointed, model retired, provider config changed). Completed steps are never
    re-executed (C1), so a substitution only ever affects future steps — and it is in the
    audit trail, never silent."""

    event_type: ClassVar[str] = "executor.substituted"
    step_id: str
    agent: str
    executor: str
    from_model: str
    to_model: str
    reason: str = ""
    principal: Principal | None = None    # system unless an operator forced it


class PolicyApplied(Event):
    """Phase 8 #2: appended right after `run.started` when an operator policy is configured.
    Records WHICH ceiling governed the run (`policy_sha256`) and every way it narrowed the
    workflow's own budget (`narrowed`, possibly empty). Replay never consults the policy —
    gate outcomes are already events — so this is the audit line, not an input to the fold."""

    event_type: ClassVar[str] = "governance.policy_applied"
    policy_sha256: str
    narrowed: list[str] = []


CONTROL_REQUEST_TYPES = (RunCancelRequested, RunPauseRequested)


EVENT_TYPES: dict[str, type[Event]] = {
    cls.event_type: cls
    for cls in (RunStarted, StepStarted, StepProgress, StepCompleted, StepDeadLettered,
                StepFailed, StepRetryRequested, StepCancelled, RunCompleted, RunFailed,
                RunCancelRequested, RunCancelled, RunPauseRequested, RunPaused, RunResumed,
                ApprovalRequested, RunSuspended, ApprovalGranted, ApprovalRejected,
                ExecutorSubstituted, PolicyApplied)
}

EventTypeName = Literal[
    "run.started", "step.started", "step.progress", "step.completed", "step.dead_lettered",
    "step.failed", "step.retry_requested", "step.cancelled", "run.completed", "run.failed",
    "run.cancel_requested", "run.cancelled", "run.pause_requested", "run.paused", "run.resumed",
    "approval.requested", "run.suspended", "approval.granted", "approval.rejected",
    "executor.substituted", "governance.policy_applied",
]


def from_record(record: dict[str, Any]) -> Event:
    """Rebuild a typed event from a persisted record, upcasting older versions first.
    Unknown event types are an error: silently dropping them would corrupt the fold."""
    from agentos.core.upcast import upcast  # local import keeps module graph acyclic

    record = upcast(dict(record))
    event_type = record.pop("event_type")
    cls = EVENT_TYPES.get(event_type)
    if cls is None:
        raise ValueError(f"unknown event type {event_type!r} — cannot fold this log")
    return cls.model_validate(record)
