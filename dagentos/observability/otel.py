"""OpenTelemetry adapter: spans derived from the event log (§11 A9).

One span per run (run.started → terminal event) and one per step attempt (step.started →
step.completed / step.failed / step.dead_lettered / step.cancelled). Span start and end
times come from each event's `occurred_at`, not from export-time wall clock, so telemetry
rebuilt from a replayed log is identical to what was emitted live.

Attribute naming: OpenTelemetry GenAI semantic conventions (`gen_ai.*`) where a concept
exists there — provider/model/tokens/cost — so 2030 tooling reads 2026 traces; everything
AgentOS-specific is namespaced `agentos.*`. The semconv package is still incubating, so
the version used is recorded on every span (`agentos.semconv.version`).
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import threading
from collections.abc import Callable
from datetime import datetime

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanProcessor
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)

from dagentos.core.events import (
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    Event,
    ExecutorSubstituted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    RunSuspended,
    StepCancelled,
    StepCompleted,
    StepDeadLettered,
    StepFailed,
    StepProgress,
    StepStarted,
)
from dagentos.core.models import Meter, WorkflowRun

RunResolver = Callable[[str], "WorkflowRun | None"]

log = logging.getLogger("agentos.observability.otel")

try:
    SEMCONV_VERSION = importlib.metadata.version("opentelemetry-semantic-conventions")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    SEMCONV_VERSION = "unknown"

# gen_ai.* names per the GenAI semantic conventions (incubating); pinned here as
# strings so a semconv package rename cannot silently change our wire format.
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

_TOKEN_METERS = {"input_tokens": GEN_AI_USAGE_INPUT_TOKENS,
                 "output_tokens": GEN_AI_USAGE_OUTPUT_TOKENS}


def _ns(ts: datetime) -> int:
    return int(ts.timestamp() * 1_000_000_000)


def run_ids(run_id: str) -> tuple[int, int]:
    """(trace_id, run span_id) derived from the run id, so the API process (which appends
    run.started) and the worker (which appends everything else) put their spans in the
    same trace without propagating context between them. The run id is a uuid4 hex —
    128 bits, exactly a trace id; anything else is hashed to size."""
    if len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id):
        trace_id = int(run_id, 16)
    else:
        trace_id = int(hashlib.sha256(run_id.encode()).hexdigest()[:32], 16)
    span_id = int(hashlib.sha256(f"run:{run_id}".encode()).hexdigest()[:16], 16)
    return trace_id or 1, span_id or 1


class _ForcedIdGenerator(RandomIdGenerator):
    """RandomIdGenerator that hands out a pre-set (trace_id, span_id) once, for the run
    span, so its ids are the deterministic ones every step span points at."""

    def __init__(self) -> None:
        self._forced = threading.local()

    def force(self, trace_id: int, span_id: int) -> None:
        self._forced.ids = (trace_id, span_id)

    def generate_trace_id(self) -> int:
        ids = getattr(self._forced, "ids", None)
        return ids[0] if ids else super().generate_trace_id()

    def generate_span_id(self) -> int:
        ids = getattr(self._forced, "ids", None)
        if ids:
            self._forced.ids = None
            return ids[1]
        return super().generate_span_id()


class OtelObserver:
    """Implements dagentos.core.ports.Observer."""

    def __init__(self, processor: SpanProcessor | None = None, *,
                 exporter: SpanExporter | None = None, service_name: str = "agentos",
                 resolve: RunResolver | None = None) -> None:
        self._ids = _ForcedIdGenerator()
        self._resolve = resolve
        self._provider = TracerProvider(id_generator=self._ids, resource=Resource.create({
            "service.name": service_name,
            "agentos.semconv.version": SEMCONV_VERSION,
        }))
        if processor is not None:
            self._provider.add_span_processor(processor)
        elif exporter is not None:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("agentos", SEMCONV_VERSION)
        # run.started facts per run, kept until the terminal event; the run span itself is
        # created THEN, by whichever process sees the end (see _end_run).
        self._runs: dict[str, dict] = {}
        self._steps: dict[tuple[str, str, int], trace.Span] = {}

    # ------------------------------------------------------------------ port
    def observe(self, event: Event, run: WorkflowRun | None = None) -> None:
        if isinstance(event, RunStarted):
            self._start_run(event)
        elif isinstance(event, StepStarted):
            self._start_step(event)
        elif isinstance(event, StepProgress):
            self._step_event(event, "progress", {"agentos.step.fraction": event.fraction,
                                                 "agentos.step.note": event.note})
        elif isinstance(event, StepCompleted):
            self._end_step(event, StatusCode.OK, self._completion_attrs(event))
        elif isinstance(event, StepFailed):
            if event.terminal:
                self._end_step(event, StatusCode.ERROR, {"agentos.step.error": event.error})
            else:
                self._end_step(event, StatusCode.ERROR, {
                    "agentos.step.error": event.error, "agentos.step.retry": True,
                    "agentos.step.retry_at": event.retry_at.isoformat() if event.retry_at else ""})
        elif isinstance(event, StepDeadLettered):
            self._run_event(event, "step.dead_lettered", {
                "agentos.step.id": event.step_id, "agentos.dead_letter.cause": event.cause,
                "agentos.effect.class": event.effect_class.value if event.effect_class else ""})
            self._end_step(event, StatusCode.ERROR, {"agentos.dead_letter.cause": event.cause})
        elif isinstance(event, StepCancelled):
            self._end_step(event, StatusCode.ERROR, {"agentos.step.cancelled": True})
        elif isinstance(event, ApprovalRequested):
            self._run_event(event, "approval.requested", {
                "agentos.approval.id": event.approval_id, "agentos.step.id": event.step_id,
                "agentos.effect.classes": ",".join(c.value for c in event.effect_classes)})
        elif isinstance(event, ExecutorSubstituted):
            self._run_event(event, "executor.substituted", {
                "agentos.step.id": event.step_id, "agentos.agent": event.agent,
                GEN_AI_SYSTEM: event.executor,
                "agentos.model.from": event.from_model, "agentos.model.to": event.to_model,
                "agentos.reason": event.reason})
        elif isinstance(event, ApprovalGranted | ApprovalRejected):
            kind = "granted" if isinstance(event, ApprovalGranted) else "rejected"
            p = event.principal
            self._run_event(event, f"approval.{kind}", {
                "agentos.approval.id": event.approval_id,
                "agentos.principal.kind": p.kind.value if p else "",
                "agentos.principal.id": p.id if p else ""})
        elif isinstance(event, RunSuspended | RunPaused | RunResumed):
            self._run_event(event, type(event).event_type, {})
        elif isinstance(event, RunCompleted):
            self._end_run(event, StatusCode.OK, {})
        elif isinstance(event, RunFailed):
            self._end_run(event, StatusCode.ERROR, {"agentos.run.error": event.error})
        elif isinstance(event, RunCancelled):
            self._end_run(event, StatusCode.ERROR, {"agentos.run.cancelled": True})

    # ---------------------------------------------------------------- spans
    def _start_run(self, ev: RunStarted) -> None:
        self._runs[ev.run_id] = {
            "start": ev.occurred_at, "agentos.run.id": ev.run_id,
            "agentos.workflow.name": ev.workflow, "agentos.workflow.version": ev.workflow_version,
            "agentos.request.id": ev.request_id, "agentos.parent_run.id": ev.parent_run_id or "",
        }

    def _run_facts(self, run_id: str) -> dict | None:
        facts = self._runs.get(run_id)
        if facts is None and self._resolve is not None:
            try:
                run = self._resolve(run_id)
            except Exception:  # noqa: BLE001 — telemetry must not raise
                run = None
            if run is not None:
                facts = {"start": run.started_at, "agentos.run.id": run_id,
                         "agentos.workflow.name": run.workflow,
                         "agentos.workflow.version": run.workflow_version,
                         "agentos.request.id": run.request_id,
                         "agentos.parent_run.id": run.parent_run_id or ""}
                self._runs[run_id] = facts
        return facts

    def _run_context(self, run_id: str):
        """Parent context for anything under a run: the deterministic run span ids, whether
        or not this process will be the one to emit the run span."""
        trace_id, span_id = run_ids(run_id)
        return trace.set_span_in_context(NonRecordingSpan(SpanContext(
            trace_id, span_id, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))))

    def _end_run(self, ev: Event, code: StatusCode, attrs: dict) -> None:
        facts = self._run_facts(ev.run_id) or {
            "start": ev.occurred_at, "agentos.run.id": ev.run_id,
            "agentos.workflow.name": "unknown", "agentos.run.start_unknown": True}
        self._runs.pop(ev.run_id, None)
        trace_id, span_id = run_ids(ev.run_id)
        self._ids.force(trace_id, span_id)
        span = self._tracer.start_span(
            f"run {facts['agentos.workflow.name']}", kind=SpanKind.INTERNAL,
            start_time=_ns(facts["start"]),
            attributes={k: v for k, v in facts.items() if k != "start"})
        span.set_attributes(attrs)
        span.set_status(Status(code))
        span.end(end_time=_ns(ev.occurred_at))
        # Any step span left open (e.g. cancelled run) ends with the run.
        for key in [k for k in self._steps if k[0] == ev.run_id]:
            s = self._steps.pop(key)
            s.set_status(Status(StatusCode.ERROR, "run ended before step settled"))
            s.end(end_time=_ns(ev.occurred_at))

    def _start_step(self, ev: StepStarted) -> None:
        span = self._tracer.start_span(
            f"step {ev.step_id}", context=self._run_context(ev.run_id), kind=SpanKind.INTERNAL,
            start_time=_ns(ev.occurred_at),
            attributes={
                "agentos.run.id": ev.run_id, "agentos.step.id": ev.step_id,
                "agentos.step.attempt": ev.attempt, "agentos.agent.name": ev.agent,
                "agentos.agent.version": ev.agent_version,
                "agentos.effect.declared": ",".join(c.value for c in ev.declared_effects),
                "agentos.idempotency_key": ev.idempotency_key,
            })
        self._steps[(ev.run_id, ev.step_id, ev.attempt)] = span

    def _end_step(self, ev, code: StatusCode, attrs: dict) -> None:
        span = self._steps.pop((ev.run_id, ev.step_id, ev.attempt), None)
        if span is None:
            return
        span.set_attributes({k: v for k, v in attrs.items() if v is not None})
        span.set_status(Status(code))
        span.end(end_time=_ns(ev.occurred_at))

    def _step_event(self, ev, name: str, attrs: dict) -> None:
        span = self._steps.get((ev.run_id, ev.step_id, ev.attempt))
        if span is not None:
            span.add_event(name, attributes=attrs, timestamp=_ns(ev.occurred_at))

    def _run_event(self, ev: Event, name: str, attrs: dict) -> None:
        """Run-level happenings (approval asked/decided, suspended, substitution, dead
        letter) are zero-duration child spans of the run, not span events: the process
        that sees them is often not the one that will emit the run span."""
        span = self._tracer.start_span(
            name, context=self._run_context(ev.run_id), kind=SpanKind.INTERNAL,
            start_time=_ns(ev.occurred_at),
            attributes={"agentos.run.id": ev.run_id,
                        **{k: v for k, v in attrs.items() if v is not None}})
        span.end(end_time=_ns(ev.occurred_at))

    @staticmethod
    def _completion_attrs(ev: StepCompleted) -> dict:
        attrs: dict = {
            "agentos.effect.reported": ",".join(e.effect_class.value for e in ev.effects),
            "agentos.cost.amount": ev.cost.amount, "agentos.cost.currency": ev.cost.currency,
            "agentos.cost.pricing_snapshot": ev.cost.pricing_snapshot_hash or "",
            "agentos.output.sha256": ev.output_ref.sha256, "agentos.output.size": ev.output_ref.size,
        }
        if ev.provenance is not None:
            attrs[GEN_AI_SYSTEM] = ev.provenance.executor
            attrs["agentos.executor.version"] = ev.provenance.executor_version
            if ev.provenance.model_id:
                attrs[GEN_AI_REQUEST_MODEL] = ev.provenance.model_id
        for m in ev.cost.units:
            attrs[_TOKEN_METERS.get(m.name, f"agentos.meter.{m.name}")] = m.quantity
        return attrs

    def force_flush(self) -> None:
        self._provider.force_flush()

    def shutdown(self) -> None:
        self._provider.shutdown()


__all__ = ["SEMCONV_VERSION", "Meter", "OtelObserver"]
