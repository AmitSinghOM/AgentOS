"""The engine as governor (docs/DEVELOPMENT_STRUCTURE.md §2.1, §11 A1/A5/A6; C11).

Every limit is enforced by the core against what the executor DECLARES and REPORTS;
nothing the executor says about its own compliance is trusted."""
from __future__ import annotations

import pytest

from agentos.core.engine import PROGRESS_MIN_INTERVAL, Engine, LeaseLost
from agentos.core.events import StepCompleted, StepDeadLettered, StepProgress, StepStarted
from agentos.core.models import (
    Agent,
    AgentType,
    Budget,
    Cost,
    Effect,
    EffectClass,
    Meter,
    Provenance,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
)
from agentos.store.memory import MemoryStore


class ScriptedExecutor:
    """Returns whatever the test scripts per agent name; optionally calls progress()."""

    name = "scripted"
    version = "test"

    def __init__(self, results: dict[str, StepResult], progress_calls: int = 0) -> None:
        self.results, self.progress_calls, self.calls = results, progress_calls, []

    def execute(self, req: StepRequest, progress) -> StepResult:
        self.calls.append(req.step_id)
        for i in range(self.progress_calls):
            progress(i / max(1, self.progress_calls), f"tick {i}")
        return self.results[req.agent.name]


def _result(output=None, effects=(), amount="0", **meters) -> StepResult:
    return StepResult(
        output=output or {"ok": True},
        effects=[Effect(effect_class=e) for e in effects],
        cost=Cost(units=[Meter(name=k, quantity=v) for k, v in meters.items()], amount=amount,
                  pricing_snapshot_hash="sha256:pricing-2026-09"),
        provenance=Provenance(executor="scripted", executor_version="test",
                              model_id="fake-1", prompt_hash="p"),
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _engine(agents: list[Agent], wf: WorkflowDefinition, executor, clock=None):
    store = MemoryStore()
    for a in agents:
        store.put_agent(a)
    store.put_workflow(wf)
    eng = Engine(store=store, blobs=store, executors={"echo": executor}, clock=clock or FakeClock())
    return store, eng


def _wf(budget: Budget | None = None, agents=("a1", "a2")) -> WorkflowDefinition:
    nodes = [{"id": "s1", "agent": agents[0]}, {"id": "s2", "agent": agents[1], "depends_on": ["s1"]}]
    return WorkflowDefinition(name="w", nodes=nodes, budget=budget or Budget())


# ------------------------------------------------------------- declare-then-do (A1)

def test_declared_effect_outside_budget_is_refused_before_dispatch():
    """Tier 3 of the gate: a class in neither `allowed` nor `approval_required_for` is
    dead-lettered before the executor runs. (`spend` is tier 2 since Phase 3 — it suspends
    for approval instead; see tests/test_approvals.py.)"""
    a1 = Agent(name="a1", type=AgentType.echo)                       # compute only
    a2 = Agent(name="a2", type=AgentType.echo,
               declared_effects=[EffectClass.compute, EffectClass.spawn_run])
    ex = ScriptedExecutor({"a1": _result(), "a2": _result(effects=[EffectClass.spawn_run])})
    store, eng = _engine([a1, a2], _wf(), ex)                        # default budget

    run = eng.start_run("w")

    assert run.status is RunStatus.failed
    assert ex.calls == ["s1"], "s2's executor must never have been called"
    events = store.read_events(run.id)
    dl = [e for e in events if isinstance(e, StepDeadLettered)]
    assert len(dl) == 1 and dl[0].step_id == "s2"
    assert dl[0].effect_class is EffectClass.spawn_run and "not allowed" in dl[0].cause
    assert run.dead_lettered == {"s2": dl[0].cause}
    started = [e for e in events if isinstance(e, StepStarted) and e.step_id == "s2"]
    assert started[0].declared_effects == [EffectClass.compute, EffectClass.spawn_run]


def test_declared_and_allowed_effect_runs():
    a = Agent(name="a1", type=AgentType.echo, declared_effects=[EffectClass.send_message])
    ex = ScriptedExecutor({"a1": _result(effects=[EffectClass.send_message])})
    wf = _wf(Budget(allowed_effect_classes={EffectClass.compute, EffectClass.send_message}),
             agents=("a1", "a1"))
    _, eng = _engine([a], wf, ex)
    run = eng.start_run("w")
    assert run.status is RunStatus.completed
    assert run.steps[0].effects[0].effect_class is EffectClass.send_message


def test_reported_undeclared_effect_is_dead_lettered_with_the_class_named():
    a = Agent(name="a1", type=AgentType.echo)                        # declares compute
    lying = _result(effects=[EffectClass.compute, EffectClass.write_external], amount="0.42")
    ex = ScriptedExecutor({"a1": lying})
    store, eng = _engine([a], _wf(agents=("a1", "a1")), ex)
    run = eng.start_run("w")

    assert run.status is RunStatus.failed
    dl = next(e for e in store.read_events(run.id) if isinstance(e, StepDeadLettered))
    assert dl.effect_class is EffectClass.write_external
    assert "undeclared" in dl.cause
    assert dl.cost.amount == "0.42"                                   # what it cost before refusal
    assert run.total_cost == "0.42"                                   # counted even though refused
    assert not any(isinstance(e, StepCompleted) for e in store.read_events(run.id))


# ------------------------------------------------------------------- cost (A6)

def test_step_cost_over_budget_is_dead_lettered_and_run_ceiling_suspends_after_recording():
    a = Agent(name="a1", type=AgentType.echo)
    ex = ScriptedExecutor({"a1": _result(amount="0.30", input_tokens=1000, output_tokens=50)})
    _, eng = _engine([a], _wf(Budget(max_step_cost="0.25"), agents=("a1", "a1")), ex)
    run = eng.start_run("w")
    assert run.status is RunStatus.failed and "max_step_cost" in run.error
    assert ex.calls == ["s1"]

    _store, eng = _engine([a], _wf(Budget(max_run_cost="0.50"), agents=("a1", "a1")), ex)
    run = eng.start_run("w")
    # s1 = 0.30 (ok), s2 = 0.60 total > 0.50 → recorded THEN suspended for a cost approval
    # (Phase 3; Phase 2 failed here). The charge is in the log either way.
    assert run.status is RunStatus.suspended
    assert [s.node_id for s in run.steps] == ["s1", "s2"]
    assert run.total_cost == "0.60"
    assert run.steps[1].cost.units[0].name == "input_tokens"
    assert run.steps[1].cost.pricing_snapshot_hash == "sha256:pricing-2026-09"
    (a,) = run.approvals.values()
    assert a.kind.value == "cost" and a.cost_at_request == "0.60" and a.proposed_ceiling == "1.10"


def test_cost_is_decimal_not_float():
    a = Agent(name="a1", type=AgentType.echo)
    ex = ScriptedExecutor({"a1": _result(amount="0.1")})
    _, eng = _engine([a], _wf(agents=("a1", "a1")), ex)
    run = eng.start_run("w")
    assert run.total_cost == "0.2"                                    # not 0.30000000000000004


# --------------------------------------------------------------- progress (A5)

def test_progress_renews_lease_and_is_rate_limited():
    a = Agent(name="a1", type=AgentType.echo)
    clock = FakeClock()
    ex = ScriptedExecutor({"a1": _result()}, progress_calls=5)
    store, eng = _engine([a], _wf(agents=("a1", "a1")), ex, clock=clock)

    heartbeats = []

    def heartbeat() -> bool:
        heartbeats.append(clock.t)
        return True

    run_id = eng.create_run("w")
    run = eng.advance(run_id, heartbeat=heartbeat)
    assert run.status is RunStatus.completed
    # 2 steps × (1 pre-step + 5 progress) heartbeats
    assert len(heartbeats) == 12
    # Clock never advanced, so only the FIRST progress() per step emits an event.
    prog = [e for e in store.read_events(run_id) if isinstance(e, StepProgress)]
    assert [(e.step_id, e.fraction) for e in prog] == [("s1", 0.0), ("s2", 0.0)]
    assert run.progress == {"s1": 1.0, "s2": 1.0}                    # completion → 1.0


def test_progress_emits_again_after_interval():
    a = Agent(name="a1", type=AgentType.echo)
    clock = FakeClock()

    class Ticking(ScriptedExecutor):
        def execute(self, req, progress):
            self.calls.append(req.step_id)
            progress(0.1, "a"); clock.t += PROGRESS_MIN_INTERVAL; progress(0.5, "b")
            return self.results[req.agent.name]

    ex = Ticking({"a1": _result()})
    store, eng = _engine([a], _wf(agents=("a1", "a1")), ex, clock=clock)
    run = eng.start_run("w")
    prog = [(e.step_id, e.fraction, e.note) for e in store.read_events(run.id)
            if isinstance(e, StepProgress)]
    assert prog == [("s1", 0.1, "a"), ("s1", 0.5, "b"), ("s2", 0.1, "a"), ("s2", 0.5, "b")]


def test_lease_lost_during_progress_stops_without_writing_completion():
    a = Agent(name="a1", type=AgentType.echo)
    ex = ScriptedExecutor({"a1": _result()}, progress_calls=1)
    store, eng = _engine([a], _wf(agents=("a1", "a1")), ex)
    run_id = eng.create_run("w")
    beats = iter([True, False])                                       # pre-step ok, progress → lost
    with pytest.raises(LeaseLost):
        eng.advance(run_id, heartbeat=lambda: next(beats))
    types = [type(e).__name__ for e in store.read_events(run_id)]
    assert types == ["RunStarted", "StepStarted"]                     # nothing else landed


# ------------------------------------------------------------ provenance / port

def test_provenance_is_recorded_and_request_carries_declaration_and_budget():
    a = Agent(name="a1", type=AgentType.echo, declared_effects=[EffectClass.compute])
    seen: list[StepRequest] = []

    class Capturing(ScriptedExecutor):
        def execute(self, req, progress):
            seen.append(req)
            return super().execute(req, progress)

    ex = Capturing({"a1": _result()})
    budget = Budget(max_step_wall_seconds=30)
    _, eng = _engine([a], _wf(budget, agents=("a1", "a1")), ex)
    run = eng.start_run("w")
    assert run.steps[0].provenance.model_id == "fake-1"
    req = seen[1]
    assert req.step_id == "s2" and req.inputs == {"s1": {"ok": True}}
    assert req.declared_effects == frozenset({EffectClass.compute})
    assert req.budget == budget and req.deadline is not None
    assert req.inputs_ref.sha256 and req.idempotency_key.startswith(f"{run.id}:s2:")


def test_wall_time_over_budget_is_dead_lettered():
    a = Agent(name="a1", type=AgentType.echo)
    clock = FakeClock()

    class Slow(ScriptedExecutor):
        def execute(self, req, progress):
            clock.t += 5.0
            return self.results[req.agent.name]

    store, eng = _engine([a], _wf(Budget(max_step_wall_seconds=2.0), agents=("a1", "a1")),
                         Slow({"a1": _result()}), clock=clock)
    run = eng.start_run("w")
    assert run.status is RunStatus.failed
    dl = next(e for e in store.read_events(run.id) if isinstance(e, StepDeadLettered))
    assert "max_step_wall_seconds" in dl.cause


def test_non_stepresult_return_is_dead_lettered_not_crash():
    a = Agent(name="a1", type=AgentType.echo)

    class Legacy:
        name, version = "legacy", "0"

        def execute(self, req, progress):
            return {"just": "a dict"}                                 # Phase 0 shape

    _store, eng = _engine([a], _wf(agents=("a1", "a1")), Legacy())
    run = eng.start_run("w")
    assert run.status is RunStatus.failed and "not StepResult" in run.error
