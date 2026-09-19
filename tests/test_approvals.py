"""Approval as a run state (C7, DESIGN §4.4, §11 A1/A2): the gated step never starts until
a principal decides; the decision is in the log; nothing is held while waiting."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dagentos.core.engine import ControlNotAllowed, Engine
from dagentos.core.events import (
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    RunSuspended,
    StepDeadLettered,
    StepStarted,
)
from dagentos.core.models import (
    Agent,
    AgentType,
    ApprovalStatus,
    Budget,
    Cost,
    Effect,
    EffectClass,
    Principal,
    PrincipalKind,
    Provenance,
    RunStatus,
    StepResult,
    WorkflowDefinition,
)
from dagentos.store.memory import MemoryStore
from dagentos.worker import Worker

HUMAN = Principal(kind=PrincipalKind.human, id="amit")
AGENT = Principal(kind=PrincipalKind.agent, id="reviewer-bot")


class RecordingExecutor:
    name, version = "rec", "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []   # (step_id, approval_id)

    def execute(self, req, progress):
        self.calls.append((req.step_id, req.approval_id))
        return StepResult(output={"step": req.step_id},
                          effects=[Effect(effect_class=c) for c in req.declared_effects],
                          cost=Cost(amount="0.10"),
                          provenance=Provenance(executor="rec", executor_version="test"))


class PinnedWall:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def build(budget: Budget | None = None, wall=None, payer_effects=(EffectClass.spend,)):
    """n1 (compute) → [pay (gated), n3 (compute)] → n4 (compute)."""
    store = MemoryStore()
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, *payer_effects]))
    store.put_workflow(WorkflowDefinition(name="w", budget=budget or Budget(), nodes=[
        {"id": "n1", "agent": "calc"},
        {"id": "pay", "agent": "payer", "depends_on": ["n1"]},
        {"id": "n3", "agent": "calc", "depends_on": ["n1"]},
        {"id": "n4", "agent": "calc", "depends_on": ["pay", "n3"]},
    ]))
    ex = RecordingExecutor()
    eng = Engine(store=store, blobs=store, executors={"echo": ex}, lease=store,
                 wall=wall or PinnedWall())
    worker = Worker(eng, store, lease=store, queue=store, holder="w")
    return store, eng, ex, worker


def pending(run):
    return [a for a in run.approvals.values() if a.status is ApprovalStatus.pending]


# ------------------------------------------------------------ suspend before dispatch

def test_gated_step_suspends_before_dispatch_and_siblings_still_run():
    store, eng, ex, worker = build()
    run_id = eng.create_run("w")
    store.push(run_id)
    assert worker.run_once(timeout=0.5) == run_id

    run = eng.get_run(run_id)
    assert run.status is RunStatus.suspended
    assert [c[0] for c in ex.calls] == ["n1", "n3"]              # pay never dispatched
    assert {s.node_id for s in run.steps} == {"n1", "n3"}         # siblings recorded
    (a,) = pending(run)
    assert a.step_id == "pay" and a.effect_classes == [EffectClass.spend]
    assert "declares spend" in a.reason
    events = store.read_events(run_id)
    assert isinstance(events[-1], RunSuspended)
    assert not any(isinstance(e, StepStarted) and e.step_id == "pay" for e in events)
    assert run.attempts.get("pay") is None                        # no attempt consumed
    # Idle: lease released, not in the queue, sweep skips it.
    assert store.acquire(run_id, "probe", 0.01) is not None
    assert store.pull(0.05) is None
    assert worker.recover() == []
    assert eng.advance(run_id).status is RunStatus.suspended       # advance is a no-op


def test_approve_resumes_and_step_runs_exactly_once_after_the_grant():
    store, eng, ex, _worker = build()
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended
    (a,) = pending(run)

    run = eng.approve(run.id, a.approval_id, principal=HUMAN, reason="looks right")
    assert run.status is RunStatus.running
    assert run.approvals[a.approval_id].status is ApprovalStatus.granted
    assert run.approvals[a.approval_id].decided_by == HUMAN

    final = eng.advance(run.id)
    assert final.status is RunStatus.completed
    assert [c for c in ex.calls if c[0] == "pay"] == [("pay", a.approval_id)]   # once, tagged
    events = store.read_events(run.id)
    grant = next(e for e in events if isinstance(e, ApprovalGranted))
    start = next(e for e in events if isinstance(e, StepStarted) and e.step_id == "pay")
    assert start.seq > grant.seq                                    # C7 acceptance
    assert start.attempt == 1
    assert final.steps[-1].node_id == "n4" and final.total_cost == "0.40"


def test_spend_requires_human_unless_workflow_allows_agent_approval():
    _, eng, _, _ = build()
    run = eng.start_run("w")
    (a,) = pending(run)
    with pytest.raises(ControlNotAllowed, match="requires a human principal"):
        eng.approve(run.id, a.approval_id, principal=AGENT)
    assert eng.get_run(run.id).status is RunStatus.suspended       # nothing changed

    _, eng2, _ex2, _ = build(Budget(allow_agent_approval=True))
    run2 = eng2.start_run("w")
    (a2,) = pending(run2)
    assert eng2.approve(run2.id, a2.approval_id, principal=AGENT).status is RunStatus.running
    assert eng2.advance(run2.id).status is RunStatus.completed

    # send_message is gated but not human-only: an agent may approve it by default.
    _, eng3, _, _ = build(payer_effects=(EffectClass.send_message,))
    run3 = eng3.start_run("w")
    (a3,) = pending(run3)
    assert eng3.approve(run3.id, a3.approval_id, principal=AGENT).status is RunStatus.running


def test_reject_dead_letters_with_decider_named_and_retry_reasks():
    store, eng, ex, _ = build()
    run = eng.start_run("w")
    (a,) = pending(run)
    run = eng.reject(run.id, a.approval_id, principal=HUMAN, reason="too expensive")

    assert run.status is RunStatus.failed
    assert run.approvals[a.approval_id].status is ApprovalStatus.rejected
    assert run.dead_lettered["pay"] == "approval rejected by human:amit: too expensive"
    events = store.read_events(run.id)
    assert [type(e).__name__ for e in events[-3:]] == ["ApprovalRejected", "StepDeadLettered",
                                                       "RunFailed"]
    assert not any(c[0] == "pay" for c in ex.calls)

    # Human retry reopens; the step re-requests approval rather than running.
    reopened = eng.request_retry(run.id, "pay", principal=HUMAN, reason="budget raised")
    assert reopened.status is RunStatus.running
    again = eng.advance(run.id)
    assert again.status is RunStatus.suspended
    assert len(pending(again)) == 1 and pending(again)[0].approval_id != a.approval_id
    assert len([e for e in store.read_events(run.id) if isinstance(e, ApprovalRequested)]) == 2


def test_multiple_gates_stay_suspended_until_all_decided():
    store = MemoryStore()
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_agent(Agent(name="mailer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.send_message]))
    store.put_workflow(WorkflowDefinition(name="two", nodes=[
        {"id": "p", "agent": "payer"}, {"id": "m", "agent": "mailer"},
        {"id": "z", "agent": "payer", "depends_on": ["p", "m"]}]))
    ex = RecordingExecutor()
    eng = Engine(store=store, blobs=store, executors={"echo": ex}, lease=store)
    run = eng.start_run("two")
    assert run.status is RunStatus.suspended and len(pending(run)) == 2
    first, second = pending(run)
    run = eng.approve(run.id, first.approval_id, principal=HUMAN)
    assert run.status is RunStatus.suspended                       # one still pending
    run = eng.approve(run.id, second.approval_id, principal=HUMAN)
    assert run.status is RunStatus.running
    run = eng.advance(run.id)
    # z (payer) needs its own approval — a new gate, a new suspension.
    assert run.status is RunStatus.suspended and pending(run)[0].step_id == "z"
    eng.approve(run.id, pending(run)[0].approval_id, principal=HUMAN)
    assert eng.advance(run.id).status is RunStatus.completed
    assert [c[0] for c in ex.calls] == ["p", "m", "z"] or sorted(c[0] for c in ex.calls[:2]) == ["m", "p"]


def test_expired_approval_is_rejected_by_system_on_sweep():
    wall = PinnedWall()
    store, eng, _ex, worker = build(Budget(approval_timeout_seconds=3600), wall=wall)
    run = eng.start_run("w")
    (a,) = pending(run)
    assert a.expires_at == wall.now + timedelta(hours=1)

    assert eng.expire_approvals() == []                            # not yet
    wall.now += timedelta(hours=1, seconds=1)
    assert worker.recover() == []                                  # sweep expires it
    run = eng.get_run(run.id)
    assert run.status is RunStatus.failed
    rej = next(e for e in store.read_events(run.id) if isinstance(e, ApprovalRejected))
    assert rej.principal == Principal(kind=PrincipalKind.system, id="expiry")
    assert "expired at" in run.dead_lettered["pay"]


def test_decisions_on_wrong_state_are_refused():
    _, eng, _, _ = build()
    run = eng.start_run("w")
    (a,) = pending(run)
    with pytest.raises(KeyError):
        eng.approve(run.id, "nope", principal=HUMAN)
    with pytest.raises(KeyError):
        eng.approve("no-run", a.approval_id, principal=HUMAN)
    eng.approve(run.id, a.approval_id, principal=HUMAN)
    with pytest.raises(ControlNotAllowed):
        eng.approve(run.id, a.approval_id, principal=HUMAN)         # already granted
    with pytest.raises(ControlNotAllowed):
        eng.reject(run.id, a.approval_id, principal=HUMAN)


def test_cancel_while_suspended_is_immediate():
    store, eng, _, _ = build()
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended
    assert eng.request_cancel(run.id, principal=HUMAN).status is RunStatus.cancelled
    assert not any(isinstance(e, StepDeadLettered) for e in store.read_events(run.id))


def test_tier3_refusal_wins_over_tier2_request():
    """A step declaring both an approvable class and a forbidden one is refused outright;
    nobody is asked to approve something that could never run."""
    _store, eng, _ex, _ = build(payer_effects=(EffectClass.spend, EffectClass.spawn_run))
    run = eng.start_run("w")
    assert run.status is RunStatus.failed and run.approvals == {}
    assert "spawn_run" in run.dead_lettered["pay"]


def test_approval_api(monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from dagentos.api import main
    importlib.reload(main)
    main.engine._executors = {"echo": RecordingExecutor()}
    c = TestClient(main.app)
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/agents", json={"name": "payer", "type": "echo",
                            "declared_effects": ["compute", "spend"]})
    c.post("/workflows", json={"name": "w", "nodes": [
        {"id": "n1", "agent": "calc"}, {"id": "pay", "agent": "payer", "depends_on": ["n1"]}]})
    run = c.post("/workflows/w/runs", params={"sync": "true"}).json()
    assert run["status"] == "suspended"
    rid = run["id"]
    inbox = c.get("/approvals").json()["data"]
    assert len(inbox) == 1 and inbox[0]["run_id"] == rid and inbox[0]["step_id"] == "pay"
    aid = inbox[0]["approval_id"]

    assert c.post(f"/runs/{rid}/approvals/{aid}/approve", json={"reason": "x"}).status_code == 422
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve",
               json={"principal": {"kind": "agent", "id": "bot"}})
    assert r.status_code == 403
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve", params={"sync": "true"},
               json={"principal": {"kind": "human", "id": "amit"}, "reason": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert c.post(f"/runs/{rid}/approvals/{aid}/approve",
                  json={"principal": {"kind": "human", "id": "amit"}}).status_code == 409
    assert c.get(f"/runs/{rid}/approvals").json()["data"][0]["status"] == "granted"
    assert c.get("/approvals").json()["data"] == []
    assert c.post(f"/runs/{rid}/approvals/nope/reject",
                  json={"principal": {"kind": "human", "id": "amit"}}).status_code == 404


# ---------------------------------------------------- cost-ceiling suspension (A6)

def _cost_wf(max_run_cost="0.25"):
    """Four steps at 0.10 each; ceiling trips after s3 (0.30 > 0.25)."""
    store = MemoryStore()
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="c", budget=Budget(max_run_cost=max_run_cost), nodes=[
        {"id": "s1", "agent": "calc"}, {"id": "s2", "agent": "calc", "depends_on": ["s1"]},
        {"id": "s3", "agent": "calc", "depends_on": ["s2"]},
        {"id": "s4", "agent": "calc", "depends_on": ["s3"]}]))
    ex = RecordingExecutor()
    eng = Engine(store=store, blobs=store, executors={"echo": ex}, lease=store)
    return store, eng, ex


def test_cost_ceiling_suspends_after_recording_and_grant_raises_ceiling():
    _store, eng, ex = _cost_wf()
    run = eng.start_run("c")
    assert run.status is RunStatus.suspended
    assert [s.node_id for s in run.steps] == ["s1", "s2", "s3"]      # tripping step recorded
    assert run.total_cost == "0.30" and [c[0] for c in ex.calls] == ["s1", "s2", "s3"]
    (a,) = pending(run)
    assert a.kind.value == "cost" and a.step_id == "s3" and a.effect_classes == []
    assert a.cost_at_request == "0.30" and a.proposed_ceiling == "0.55"
    assert "approving raises the ceiling to 0.55" in a.reason

    with pytest.raises(ControlNotAllowed, match="cost ceiling"):
        eng.approve(run.id, a.approval_id, principal=AGENT)         # money is human-only
    run = eng.approve(run.id, a.approval_id, principal=HUMAN, reason="worth it")
    assert run.status is RunStatus.running and run.cost_ceiling == "0.55"
    final = eng.advance(run.id)
    assert final.status is RunStatus.completed and final.total_cost == "0.40"
    assert [c[0] for c in ex.calls] == ["s1", "s2", "s3", "s4"]     # s3 not re-run


def test_cost_ceiling_rejection_fails_run_without_dead_letter():
    store, eng, ex = _cost_wf()
    run = eng.start_run("c")
    (a,) = pending(run)
    run = eng.reject(run.id, a.approval_id, principal=HUMAN, reason="over budget")
    assert run.status is RunStatus.failed
    assert "cost ceiling approval rejected by human:amit: over budget" in run.error
    assert run.dead_lettered == {}                                   # nothing to reopen
    assert [type(e).__name__ for e in store.read_events(run.id)][-2:] == \
        ["ApprovalRejected", "RunFailed"]
    assert len(ex.calls) == 3


def test_cost_ceiling_trips_again_at_the_raised_ceiling():
    _store, eng, _ex = _cost_wf(max_run_cost="0.15")                   # trips after s2 (0.20)
    run = eng.start_run("c")
    (a,) = pending(run)
    assert a.cost_at_request == "0.20" and a.proposed_ceiling == "0.35"
    eng.approve(run.id, a.approval_id, principal=HUMAN)
    run = eng.advance(run.id)                                        # s3 → 0.30 ok, s4 → 0.40 > 0.35
    assert run.status is RunStatus.suspended and run.total_cost == "0.40"
    second = [x for x in pending(run)]
    assert len(second) == 1 and second[0].cost_at_request == "0.40" and second[0].proposed_ceiling == "0.55"
    eng.approve(run.id, second[0].approval_id, principal=HUMAN)
    assert eng.advance(run.id).status is RunStatus.completed
