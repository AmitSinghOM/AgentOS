"""Observability is a consumer of the log: spans and metrics derived from events, core
imports no SDK, and replaying a stored log rebuilds identical telemetry (§11 A9)."""
from __future__ import annotations

import pytest

otel = pytest.importorskip("opentelemetry.sdk")
prom = pytest.importorskip("prometheus_client")

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from dagentos.core.engine import Engine
from dagentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Meter,
    Principal,
    PrincipalKind,
    Provenance,
    RetryPolicy,
    RunStatus,
    StepResult,
    WorkflowDefinition,
)
from dagentos.observability import replay
from dagentos.observability.otel import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    OtelObserver,
)
from dagentos.observability.prometheus import PrometheusObserver
from dagentos.store.memory import MemoryStore


class LlmLike:
    """Reports tokens and a model id like a real provider would."""

    name, version = "fakellm", "9.9"

    def __init__(self, fail_first: set[str] | None = None) -> None:
        self.fail_first, self.seen = fail_first or set(), set()

    def execute(self, req, progress):
        if req.step_id in self.fail_first and req.step_id not in self.seen:
            self.seen.add(req.step_id)
            raise RuntimeError("transient")
        progress(0.5, "thinking")
        return StepResult(
            output={"text": f"{req.step_id} done"},
            effects=[Effect(effect_class=EffectClass.compute)],
            cost=Cost(units=[Meter(name="input_tokens", quantity=120),
                             Meter(name="output_tokens", quantity=30)],
                      amount="0.0015", pricing_snapshot_hash="sha256:p"),
            provenance=Provenance(executor="fakellm", executor_version="9.9",
                                  model_id="fake-1-mini", prompt_hash="h"),
        )


def build(executor=None, observers=()):
    store = MemoryStore()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "n1", "agent": "a"},
        {"id": "n2", "agent": "a", "depends_on": ["n1"],
         "retry": RetryPolicy(max_attempts=2, backoff_seconds=0.01)},
        {"id": "n3", "agent": "a", "depends_on": ["n1"]},
        {"id": "n4", "agent": "a", "depends_on": ["n2", "n3"]},
    ]))
    eng = Engine(store=store, blobs=store, executors={"echo": executor or LlmLike()},
                 lease=store, observers=observers)
    return store, eng


def spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


# ------------------------------------------------------------------ OpenTelemetry

def test_otel_spans_are_derived_from_events_with_genai_attributes():
    exporter = InMemorySpanExporter()
    otel_obs = OtelObserver(processor=SimpleSpanProcessor(exporter))
    _store, eng = build(LlmLike(fail_first={"n2"}), observers=[otel_obs])
    run = eng.start_run("w")
    assert run.status is RunStatus.completed

    spans = exporter.get_finished_spans()
    run_span = next(s for s in spans if s.name == "run w")
    steps = [s for s in spans if s.name.startswith("step ")]
    assert len(steps) == 5                                   # n2 twice (retry) + n1, n3, n4
    assert all(s.parent is not None and s.parent.span_id == run_span.context.span_id
               for s in steps)
    # Timestamps come from occurred_at: the run span brackets every step span.
    assert run_span.start_time <= min(s.start_time for s in steps)
    assert run_span.end_time >= max(s.end_time for s in steps)
    assert run_span.status.status_code is StatusCode.OK

    n2_attempts = sorted((s for s in steps if s.name == "step n2"),
                         key=lambda s: s.attributes["agentos.step.attempt"])
    assert n2_attempts[0].status.status_code is StatusCode.ERROR
    assert n2_attempts[0].attributes["agentos.step.retry"] is True
    ok = n2_attempts[1]
    assert ok.status.status_code is StatusCode.OK
    assert ok.attributes[GEN_AI_SYSTEM] == "fakellm"
    assert ok.attributes[GEN_AI_REQUEST_MODEL] == "fake-1-mini"
    assert ok.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 120
    assert ok.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 30
    assert ok.attributes["agentos.cost.amount"] == "0.0015"
    assert ok.attributes["agentos.agent.version"] == 1
    assert [e.name for e in ok.events] == ["progress"]
    assert run_span.resource.attributes["agentos.semconv.version"]


def test_genai_attribute_names_match_installed_semconv():
    """Pinned strings must equal the semconv package's — a rename upstream shows up here."""
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g
    assert GEN_AI_SYSTEM == g.GEN_AI_SYSTEM
    assert GEN_AI_REQUEST_MODEL == g.GEN_AI_REQUEST_MODEL
    assert GEN_AI_USAGE_INPUT_TOKENS == g.GEN_AI_USAGE_INPUT_TOKENS
    assert GEN_AI_USAGE_OUTPUT_TOKENS == g.GEN_AI_USAGE_OUTPUT_TOKENS


def test_otel_records_approval_and_dead_letter_as_run_child_spans():
    exporter = InMemorySpanExporter()
    otel_obs = OtelObserver(processor=SimpleSpanProcessor(exporter))
    store, eng = build(observers=[otel_obs])
    store.put_workflow(WorkflowDefinition(name="w", version=2, nodes=[
        {"id": "pay", "agent": "payer"}]))
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended
    aid = next(iter(run.approvals))
    eng.reject(run.id, aid, principal=Principal(kind=PrincipalKind.human, id="amit"), reason="no")
    spans = exporter.get_finished_spans()
    run_span = next(s for s in spans if s.name == "run w")
    # Run-level happenings are zero-duration CHILD spans of the run (any process can emit
    # them), in event order, all in the run's trace and parented to the run span.
    children = sorted((s for s in spans if s.parent is not None), key=lambda s: s.start_time)
    assert [s.name for s in children] == ["approval.requested", "run.suspended",
                                          "approval.rejected", "step.dead_lettered"]
    assert all(s.context.trace_id == run_span.context.trace_id for s in children)
    assert all(s.parent.span_id == run_span.context.span_id for s in children)
    rej = children[2]
    assert rej.attributes["agentos.principal.kind"] == "human"
    assert run_span.status.status_code is StatusCode.ERROR


def test_two_processes_produce_one_trace_per_run():
    """The API appends run.started; a worker appends the rest; each has its own observer
    and neither propagates context. Trace and run-span ids derive from the run id, and the
    worker's observer resolves run facts from the store, so the pieces meet in one trace."""
    from dagentos.observability import replay, store_resolver
    from dagentos.observability.otel import run_ids

    api_exp, worker_exp = InMemorySpanExporter(), InMemorySpanExporter()
    store, eng = build()
    api_obs = OtelObserver(processor=SimpleSpanProcessor(api_exp))
    run_id = eng.create_run("w")                                   # "API process"
    api_obs.observe(store.read_events(run_id)[0])                  # sees ONLY run.started
    eng.advance(run_id)
    worker_obs = OtelObserver(processor=SimpleSpanProcessor(worker_exp),
                              resolve=store_resolver(store))       # "worker process"
    for ev in store.read_events(run_id)[1:]:                       # never sees run.started
        worker_obs.observe(ev)

    assert api_exp.get_finished_spans() == ()                      # nothing dangling in the API
    trace_id, run_span_id = run_ids(run_id)
    worker_spans = worker_exp.get_finished_spans()
    run_span = next(s for s in worker_spans if s.name.startswith("run "))
    assert run_span.name == "run w"                                # resolved, not "unknown"
    assert run_span.attributes["agentos.workflow.name"] == "w"
    assert run_span.context.trace_id == trace_id and run_span.context.span_id == run_span_id
    started_at = store.read_events(run_id)[0].occurred_at
    assert run_span.start_time == int(started_at.timestamp() * 1e9)  # from run.started_at
    steps = [s for s in worker_spans if s.name.startswith("step ")]
    assert steps and all(s.context.trace_id == trace_id and s.parent.span_id == run_span_id
                         for s in steps)
    # And a full replay by one observer produces the identical run span ids.
    again = InMemorySpanExporter()
    replay(store, [run_id], [OtelObserver(processor=SimpleSpanProcessor(again))])
    replayed = next(s for s in again.get_finished_spans() if s.name == "run w")
    assert (replayed.context.trace_id, replayed.context.span_id) == (trace_id, run_span_id)


# --------------------------------------------------------------------- Prometheus

def _sample(reg, name, **labels):
    return reg.get_sample_value(name, labels)


def test_prometheus_metrics_are_derived_from_events():
    p = PrometheusObserver()
    _store, eng = build(LlmLike(fail_first={"n2"}), observers=[p])
    eng.start_run("w")
    r = p.registry
    assert _sample(r, "agentos_runs_started_total", workflow="w") == 1
    assert _sample(r, "agentos_runs_ended_total", workflow="w", outcome="completed") == 1
    assert _sample(r, "agentos_run_duration_seconds_count", workflow="w", outcome="completed") == 1
    assert _sample(r, "agentos_steps_started_total", workflow="w", step="n2", agent="a") == 2
    assert _sample(r, "agentos_step_retries_scheduled_total", workflow="w", step="n2") == 1
    assert _sample(r, "agentos_steps_ended_total", workflow="w", step="n2", outcome="retry") == 1
    assert _sample(r, "agentos_steps_ended_total", workflow="w", step="n2", outcome="completed") == 1
    assert _sample(r, "agentos_meter_units_total", workflow="w", agent="fakellm",
                   meter="input_tokens") == 480                      # 4 completed steps × 120
    assert abs(_sample(r, "agentos_cost_total", workflow="w", currency="USD") - 0.006) < 1e-9
    body, ctype = p.render()
    assert b"agentos_run_duration_seconds_bucket" in body and "text/plain" in ctype


def test_prometheus_tracks_suspension_and_approval_wait():
    p = PrometheusObserver()
    store, eng = build(observers=[p])
    store.put_workflow(WorkflowDefinition(name="w", version=2, nodes=[
        {"id": "pay", "agent": "payer"}]))
    run = eng.start_run("w")
    r = p.registry
    assert _sample(r, "agentos_runs_suspended", workflow="w") == 1
    assert _sample(r, "agentos_approvals_requested_total", workflow="w", effect_class="spend") == 1
    aid = next(iter(run.approvals))
    eng.approve(run.id, aid, principal=Principal(kind=PrincipalKind.human, id="amit"))
    assert _sample(r, "agentos_runs_suspended", workflow="w") == 0
    assert _sample(r, "agentos_approvals_decided_total", workflow="w", decision="granted",
                   principal_kind="human") == 1
    assert _sample(r, "agentos_approval_wait_seconds_count", workflow="w", decision="granted") == 1
    eng.advance(run.id)
    assert _sample(r, "agentos_runs_ended_total", workflow="w", outcome="completed") == 1


# ----------------------------------------------------------- replay equivalence

def test_replaying_the_log_rebuilds_identical_telemetry():
    """The property that justifies deriving telemetry from events: live vs replayed spans
    are the same set with the same timings and attributes."""
    live_exp, replay_exp = InMemorySpanExporter(), InMemorySpanExporter()
    live = OtelObserver(processor=SimpleSpanProcessor(live_exp))
    store, eng = build(LlmLike(fail_first={"n2"}), observers=[live])
    run = eng.start_run("w")

    replayed = OtelObserver(processor=SimpleSpanProcessor(replay_exp))
    n = replay(store, [run.id], [replayed])
    assert n == len(store.read_events(run.id))

    def shape(exp):
        return sorted(
            (s.name, s.start_time, s.end_time, s.status.status_code.name,
             tuple(sorted((k, v) for k, v in s.attributes.items())),
             tuple((e.name, e.timestamp) for e in s.events))
            for s in exp.get_finished_spans())

    assert shape(live_exp) == shape(replay_exp)

    # Same for metrics: a fresh Prometheus observer fed the log matches one that was live.
    p_live = PrometheusObserver()
    store2, eng2 = build(LlmLike(fail_first={"n2"}), observers=[p_live])
    run2 = eng2.start_run("w")
    p_replay = PrometheusObserver()
    replay(store2, [run2.id], [p_replay])
    checks = [
        ("agentos_runs_ended_total", {"workflow": "w", "outcome": "completed"}),
        ("agentos_steps_started_total", {"workflow": "w", "step": "n2", "agent": "a"}),
        ("agentos_step_retries_scheduled_total", {"workflow": "w", "step": "n2"}),
        ("agentos_cost_total", {"workflow": "w", "currency": "USD"}),
        ("agentos_meter_units_total", {"workflow": "w", "agent": "fakellm", "meter": "output_tokens"}),
    ]
    for name, labels in checks:
        live_v = p_live.registry.get_sample_value(name, labels)
        assert live_v is not None and live_v == p_replay.registry.get_sample_value(name, labels), name


def test_observer_failure_never_breaks_the_engine(caplog):
    class Broken:
        def observe(self, event, run=None):
            raise RuntimeError("telemetry down")

    _, eng = build(observers=[Broken()])
    caplog.set_level("ERROR", logger="agentos.engine")
    run = eng.start_run("w")
    assert run.status is RunStatus.completed
    assert any("observer Broken failed" in r.getMessage() for r in caplog.records)


def test_core_still_imports_no_telemetry_sdk():
    import subprocess
    import sys
    # Fresh interpreter: import the core, assert no telemetry SDK module got loaded.
    code = ("import sys, dagentos.core.engine, dagentos.core.ports; "
            "bad=[m for m in sys.modules if m.startswith(('opentelemetry','prometheus_client'))]; "
            "print(len(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"


def test_metrics_endpoint_exposes_queue_depth(monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from dagentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    c.post("/agents", json={"name": "a", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [{"id": "s", "agent": "a"}]})
    c.post("/workflows/w/runs")                              # 202: queued, no worker running
    body = c.get("/metrics").text
    assert "agentos_queue_depth 1.0" in body
    assert "agentos_runs_started_total" in body


def test_prometheus_in_the_worker_labels_by_workflow_via_the_resolver():
    """Without the resolver every worker-side metric was workflow="unknown" (the API
    appends run.started, the worker never sees it)."""
    from dagentos.observability import store_resolver

    store, eng = build()
    run_id = eng.create_run("w")
    eng.advance(run_id)
    p = PrometheusObserver(resolve=store_resolver(store))
    for ev in store.read_events(run_id)[1:]:                       # no run.started
        p.observe(ev)
    assert _sample(p.registry, "agentos_runs_ended_total", workflow="w", outcome="completed") == 1
    assert _sample(p.registry, "agentos_runs_ended_total", workflow="unknown",
                   outcome="completed") is None
    assert _sample(p.registry, "agentos_run_duration_seconds_count", workflow="w",
                   outcome="completed") == 1                       # duration from started_at
