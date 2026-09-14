"""Prometheus adapter: metrics derived from the event log (ROADMAP Phase 3).

Run latency, throughput, error rate, retries, dead-letters, approvals, cost — all
computed from events, so the numbers a Grafana panel shows are exactly the numbers the
log implies. Queue depth is the one metric that is not an event fact; it is read from
the queue when `/metrics` is scraped (see `QueueDepthCollector`).

Labels are kept low-cardinality on purpose: workflow name, step id, agent name, effect
class, principal kind. Never run_id, never approval_id.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

RunResolver = Callable[[str], "WorkflowRun | None"]

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from agentos.core.events import (
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    Event,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunSuspended,
    StepCompleted,
    StepDeadLettered,
    StepFailed,
    StepStarted,
)
from agentos.core.models import WorkflowRun

RUN_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3600, float("inf"))
STEP_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, float("inf"))


class PrometheusObserver:
    """Implements agentos.core.ports.Observer. `resolve(run_id)` supplies run context
    (workflow, started_at) for runs whose `run.started` this process did not observe —
    the worker never does; the API appends it."""

    def __init__(self, registry: CollectorRegistry | None = None, *,
                 resolve: RunResolver | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._resolve = resolve
        r = self.registry
        self.runs_started = Counter("agentos_runs_started_total", "Runs started",
                                    ["workflow"], registry=r)
        self.runs_ended = Counter("agentos_runs_ended_total", "Runs reaching a terminal state",
                                  ["workflow", "outcome"], registry=r)
        self.run_seconds = Histogram("agentos_run_duration_seconds",
                                     "run.started → terminal event", ["workflow", "outcome"],
                                     buckets=RUN_BUCKETS, registry=r)
        self.steps_started = Counter("agentos_steps_started_total", "Step attempts started",
                                     ["workflow", "step", "agent"], registry=r)
        self.steps_ended = Counter("agentos_steps_ended_total", "Step attempts settled",
                                   ["workflow", "step", "outcome"], registry=r)
        self.step_seconds = Histogram("agentos_step_duration_seconds",
                                      "step.started → settle", ["workflow", "step", "outcome"],
                                      buckets=STEP_BUCKETS, registry=r)
        self.retries = Counter("agentos_step_retries_scheduled_total",
                               "Non-terminal step failures (a retry was scheduled)",
                               ["workflow", "step"], registry=r)
        self.dead_letters = Counter("agentos_steps_dead_lettered_total", "Dead-lettered steps",
                                    ["workflow", "step", "effect_class"], registry=r)
        self.cost = Counter("agentos_cost_total", "Recorded cost (sum of step costs)",
                            ["workflow", "currency"], registry=r)
        self.meters = Counter("agentos_meter_units_total", "Metered units (tokens, seconds…)",
                              ["workflow", "agent", "meter"], registry=r)
        self.approvals_requested = Counter("agentos_approvals_requested_total",
                                           "Approval gates opened", ["workflow", "effect_class"],
                                           registry=r)
        self.approvals_decided = Counter("agentos_approvals_decided_total", "Approval decisions",
                                         ["workflow", "decision", "principal_kind"], registry=r)
        self.approval_wait = Histogram("agentos_approval_wait_seconds",
                                       "approval.requested → decision", ["workflow", "decision"],
                                       buckets=RUN_BUCKETS, registry=r)
        self.suspended = Gauge("agentos_runs_suspended", "Runs currently awaiting approval",
                               ["workflow"], registry=r)
        self._run_started_at: dict[str, tuple[str, datetime]] = {}
        self._step_started_at: dict[tuple[str, str, int], datetime] = {}
        self._approval_at: dict[str, tuple[str, datetime]] = {}
        self._pending_by_run: dict[str, set[str]] = {}
        self._suspended: set[str] = set()

    # ------------------------------------------------------------------ port
    def observe(self, event: Event, run: WorkflowRun | None = None) -> None:
        wf = self._workflow(event)
        if isinstance(event, RunStarted):
            self._run_started_at[event.run_id] = (event.workflow, event.occurred_at)
            self.runs_started.labels(event.workflow).inc()
        elif isinstance(event, StepStarted):
            self._step_started_at[(event.run_id, event.step_id, event.attempt)] = event.occurred_at
            self.steps_started.labels(wf, event.step_id, event.agent).inc()
        elif isinstance(event, StepCompleted):
            self._settle_step(event, wf, "completed")
            self.cost.labels(wf, event.cost.currency).inc(float(event.cost.decimal()))
            agent = self._agent_for(event)
            for m in event.cost.units:
                self.meters.labels(wf, agent, m.name).inc(m.quantity)
        elif isinstance(event, StepFailed):
            if event.terminal:
                self._settle_step(event, wf, "failed")
            else:
                self._settle_step(event, wf, "retry")
                self.retries.labels(wf, event.step_id).inc()
        elif isinstance(event, StepDeadLettered):
            self.dead_letters.labels(wf, event.step_id,
                                     event.effect_class.value if event.effect_class else "").inc()
            self._settle_step(event, wf, "dead_lettered")
        elif isinstance(event, ApprovalRequested):
            self._approval_at[event.approval_id] = (event.step_id, event.occurred_at)
            self._pending_by_run.setdefault(event.run_id, set()).add(event.approval_id)
            for c in event.effect_classes:
                self.approvals_requested.labels(wf, c.value).inc()
        elif isinstance(event, RunSuspended):
            if event.run_id not in self._suspended:
                self._suspended.add(event.run_id)
                self.suspended.labels(wf).inc()
        elif isinstance(event, ApprovalGranted | ApprovalRejected):
            decision = "granted" if isinstance(event, ApprovalGranted) else "rejected"
            kind = event.principal.kind.value if event.principal else ""
            self.approvals_decided.labels(wf, decision, kind).inc()
            opened = self._approval_at.pop(event.approval_id, None)
            if opened is None and self._resolve is not None:
                # Requested in the worker, decided in the API: ask the log when.
                try:
                    run = self._resolve(event.run_id)
                    a = run.approvals.get(event.approval_id) if run else None
                    opened = (a.step_id, a.requested_at) if a else None
                except Exception:  # noqa: BLE001 — telemetry must not raise
                    opened = None
            if opened:
                self.approval_wait.labels(wf, decision).observe(
                    (event.occurred_at - opened[1]).total_seconds())
            pending = self._pending_by_run.get(event.run_id, set())
            pending.discard(event.approval_id)
            if isinstance(event, ApprovalGranted) and not pending:
                self._unsuspend(event.run_id, wf)      # last gate decided → running again
        elif isinstance(event, RunCompleted | RunFailed | RunCancelled):
            outcome = {RunCompleted: "completed", RunFailed: "failed",
                       RunCancelled: "cancelled"}[type(event)]
            self._unsuspend(event.run_id, wf)
            self.runs_ended.labels(wf, outcome).inc()
            started = self._run_info(event.run_id)
            self._run_started_at.pop(event.run_id, None)
            if started:
                self.run_seconds.labels(wf, outcome).observe(
                    (event.occurred_at - started[1]).total_seconds())
            for key in [k for k in self._step_started_at if k[0] == event.run_id]:
                self._step_started_at.pop(key)

    # ------------------------------------------------------------- helpers
    def _settle_step(self, event, wf: str, outcome: str) -> None:
        self.steps_ended.labels(wf, event.step_id, outcome).inc()
        started = self._step_started_at.pop((event.run_id, event.step_id, event.attempt), None)
        if started is not None:
            self.step_seconds.labels(wf, event.step_id, outcome).observe(
                (event.occurred_at - started).total_seconds())

    def _unsuspend(self, run_id: str, wf: str) -> None:
        if run_id in self._suspended:
            self._suspended.discard(run_id)
            self.suspended.labels(wf).dec()

    def _workflow(self, event: Event) -> str:
        entry = self._run_info(event.run_id)
        return entry[0] if entry else "unknown"

    def _run_info(self, run_id: str) -> tuple[str, datetime] | None:
        """(workflow, started_at). From run.started when this process saw it; otherwise
        from the resolver — the worker never sees run.started (the API appends it)."""
        entry = self._run_started_at.get(run_id)
        if entry is None and self._resolve is not None:
            try:
                run = self._resolve(run_id)
            except Exception:  # noqa: BLE001 — telemetry must not raise
                run = None
            if run is not None:
                entry = (run.workflow, run.started_at)
                self._run_started_at[run_id] = entry
        return entry

    def _agent_for(self, event: StepCompleted) -> str:
        return event.provenance.executor if event.provenance else "unknown"

    def render(self) -> tuple[bytes, str]:
        """Body + content type for a /metrics endpoint."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class QueueDepthCollector:
    """Not an event fact: reads the queue at scrape time. Optional; needs a store that
    exposes `queue_depth()`."""

    def __init__(self, registry: CollectorRegistry, depth_fn) -> None:
        self._gauge = Gauge("agentos_queue_depth", "Runs waiting in the queue",
                            registry=registry)
        self._depth_fn = depth_fn

    def refresh(self) -> None:
        self._gauge.set(self._depth_fn())
