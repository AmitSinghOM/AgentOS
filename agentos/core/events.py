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

from agentos.core.models import BlobRef, EffectClass, Principal

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


class StepStarted(Event):
    event_type: ClassVar[str] = "step.started"
    step_id: str
    attempt: int
    agent: str
    idempotency_key: str                  # run_id:step_id:sha256(inputs)
    declared_effects: list[EffectClass] = Field(default_factory=list)


class StepCompleted(Event):
    event_type: ClassVar[str] = "step.completed"
    step_id: str
    attempt: int
    idempotency_key: str
    output_ref: BlobRef                   # bytes live in the BlobStore (§11 A4)


class StepFailed(Event):
    event_type: ClassVar[str] = "step.failed"
    step_id: str
    attempt: int
    error: str
    terminal: bool = True                 # Phase 2: False while retries remain


class RunCompleted(Event):
    event_type: ClassVar[str] = "run.completed"


class RunFailed(Event):
    event_type: ClassVar[str] = "run.failed"
    error: str
    step_id: str | None = None


EVENT_TYPES: dict[str, type[Event]] = {
    cls.event_type: cls
    for cls in (RunStarted, StepStarted, StepCompleted, StepFailed, RunCompleted, RunFailed)
}

EventTypeName = Literal[
    "run.started", "step.started", "step.completed", "step.failed",
    "run.completed", "run.failed",
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
