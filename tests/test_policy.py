"""Phase 8 #2 — the operator policy ceiling: a bound over EVERY workflow's budget.

The question a pilot evaluator asks: "can a workflow author give themselves more than the
operator allowed?" One test per way they might try:
  * allow `spend` freely            → ceiling excludes it: refused before dispatch
  * allow `spend` freely            → policy always_approve: a decision is asked instead
  * allow_agent_approval = true     → policy forbids: an agent's approve is 403, on the
                                      APPROVE path (not only in the wave)
  * point an agent at an executor   → outside allowed_executors: run fails at dispatch,
                                      executor never called
  * raise max_run_cost              → policy is lower: the ceiling trips at the policy value
And the audit: `governance.policy_applied` right after run.started naming the policy hash
and each narrowing; the folded run carries `policy_sha256`; no policy → no event, no change.
Plus the pure function, the loader's error messages, and GET /policy.
"""
from __future__ import annotations

import importlib
import json
from decimal import Decimal

import pytest

from agentos.core.engine import ControlNotAllowed, Engine
from agentos.core.models import (
    Agent,
    AgentType,
    Budget,
    EffectClass,
    Principal,
    PrincipalKind,
    RunStatus,
    WorkflowDefinition,
)
from agentos.core.policy import (
    OperatorPolicy,
    PolicyError,
    apply_ceiling,
    executor_allowed,
    load_policy,
    policy_from_env,
    policy_sha256,
)
from agentos.store.memory import MemoryStore
from tests.test_approvals import PinnedWall, RecordingExecutor

FREE_SPEND = Budget(allowed_effect_classes={EffectClass.read, EffectClass.compute, EffectClass.spend},
                    approval_required_for={EffectClass.write_external})
AGENT = Principal(kind=PrincipalKind.agent, id="bot")
HUMAN = Principal(kind=PrincipalKind.human, id="amit")


def _engine(budget: Budget, policy: OperatorPolicy | None, *, payer_executor: str | None = None):
    """n1 (compute) → pay (compute + spend)."""
    store = MemoryStore()
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo, executor=payer_executor,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="w", budget=budget, nodes=[
        {"id": "n1", "agent": "calc"}, {"id": "pay", "agent": "payer", "depends_on": ["n1"]}]))
    ex = RecordingExecutor()
    executors = {"echo": ex}
    if payer_executor:
        executors[payer_executor] = ex
    eng = Engine(store=store, blobs=store, executors=executors, lease=store, wall=PinnedWall(),
                 policy=policy)
    return store, eng, ex


def _types(store, run_id):
    return [type(e).event_type for e in store.read_events(run_id)]


# ------------------------------------------------------------------ the pure function

def test_no_policy_leaves_the_budget_untouched():
    b, narrowed = apply_ceiling(FREE_SPEND, None)
    assert b == FREE_SPEND and narrowed == []


def test_ceiling_removes_classes_from_both_tiers_and_names_them():
    policy = OperatorPolicy(effect_ceiling=frozenset({EffectClass.read, EffectClass.compute}))
    b, narrowed = apply_ceiling(FREE_SPEND, policy)
    assert b.allowed_effect_classes == {EffectClass.read, EffectClass.compute}
    assert b.approval_required_for == set()
    assert narrowed == ["allowed_effect_classes: removed spend (outside effect_ceiling)",
                        "approval_required_for: removed write_external (outside effect_ceiling)"]


def test_always_approve_moves_a_free_class_to_the_approval_tier():
    b, narrowed = apply_ceiling(FREE_SPEND, OperatorPolicy(always_approve=frozenset({EffectClass.spend})))
    assert EffectClass.spend not in b.allowed_effect_classes
    assert EffectClass.spend in b.approval_required_for
    assert narrowed == ["spend: allowed → approval_required (always_approve)"]
    # a class the workflow already gates, or never allowed, is not reported
    _, narrowed2 = apply_ceiling(Budget(), OperatorPolicy(always_approve=frozenset({EffectClass.spend})))
    assert narrowed2 == []


def test_always_approve_outside_the_ceiling_is_refused_not_asked():
    """Ceiling wins over approval: the class ends in neither tier (tier 3 → refused)."""
    policy = OperatorPolicy(effect_ceiling=frozenset({EffectClass.compute}),
                            always_approve=frozenset({EffectClass.spend}))
    b, _ = apply_ceiling(FREE_SPEND, policy)
    assert EffectClass.spend not in b.allowed_effect_classes | b.approval_required_for


def test_numeric_limits_take_the_minimum_and_agent_approval_can_only_be_revoked():
    wf = Budget(allow_agent_approval=True, max_step_cost="1.00", max_run_cost=None,
                max_step_wall_seconds=30)
    policy = OperatorPolicy(agent_approval_allowed=False, max_step_cost="5.00",
                            max_run_cost="2.00", max_step_wall_seconds=10)
    b, narrowed = apply_ceiling(wf, policy)
    assert (b.allow_agent_approval, b.max_step_cost, b.max_run_cost, b.max_step_wall_seconds) \
        == (False, "1.00", "2.00", 10)
    assert narrowed == ["allow_agent_approval: true → false (agent_approval_allowed=false)",
                        "max_run_cost: None → 2.00", "max_step_wall_seconds: 30.0 → 10.0"]
    # a policy that permits agent approval does not grant it to a workflow that did not ask
    b2, n2 = apply_ceiling(Budget(), OperatorPolicy(agent_approval_allowed=True))
    assert b2.allow_agent_approval is False and n2 == []


def test_executor_allowlist_none_means_any():
    assert executor_allowed(None, "anything")
    assert executor_allowed(OperatorPolicy(), "anything")
    p = OperatorPolicy(allowed_executors=frozenset({"echo"}))
    assert executor_allowed(p, "echo") and not executor_allowed(p, "openai-compat")


def test_policy_hash_is_canonical():
    a = OperatorPolicy(effect_ceiling=frozenset({EffectClass.spend, EffectClass.read}),
                       allowed_executors=frozenset({"tool", "echo"}))
    b = OperatorPolicy.model_validate({"effect_ceiling": ["read", "spend"],
                                       "allowed_executors": ["echo", "tool"]})
    assert policy_sha256(a) == policy_sha256(b) and len(policy_sha256(a)) == 64
    assert policy_sha256(a) != policy_sha256(OperatorPolicy())


# ------------------------------------------------------------------ through the engine

def test_free_spend_outside_the_ceiling_is_refused_before_dispatch_and_audited():
    policy = OperatorPolicy(effect_ceiling=frozenset({EffectClass.read, EffectClass.compute}))
    store, eng, ex = _engine(FREE_SPEND, policy)
    run = eng.start_run("w")
    assert run.status is RunStatus.failed
    assert [c[0] for c in ex.calls] == ["n1"]                 # pay never dispatched
    assert "pay" in run.dead_lettered
    types = _types(store, run.id)
    assert types[:2] == ["run.started", "governance.policy_applied"]
    applied = store.read_events(run.id)[1]
    assert applied.policy_sha256 == policy_sha256(policy) == eng.policy_digest
    assert applied.narrowed[0].startswith("allowed_effect_classes: removed spend")
    assert run.policy_sha256 == policy_sha256(policy)
    assert run.policy_narrowed == applied.narrowed


def test_always_approve_turns_a_free_spend_into_a_decision():
    _store, eng, ex = _engine(FREE_SPEND, OperatorPolicy(always_approve=frozenset({EffectClass.spend})))
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended and [c[0] for c in ex.calls] == ["n1"]
    (approval,) = run.approvals.values()
    assert approval.step_id == "pay" and approval.effect_classes == [EffectClass.spend]
    eng.approve(run.id, approval.approval_id, principal=HUMAN)
    assert eng.advance_until_terminal(run.id).status is RunStatus.completed
    assert [c[0] for c in ex.calls] == ["n1", "pay"]


def test_policy_revokes_agent_approval_on_the_approve_path():
    """The workflow says agents may approve spend; the operator says no. The check that
    matters is in `approve`, which reads the budget independently of the wave."""
    wf_budget = Budget(allow_agent_approval=True)
    _store, eng, _ex = _engine(wf_budget, OperatorPolicy(agent_approval_allowed=False))
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended
    (approval,) = run.approvals.values()
    with pytest.raises(ControlNotAllowed, match="principal"):
        eng.approve(run.id, approval.approval_id, principal=AGENT)
    assert eng.get_run(run.id).approvals[approval.approval_id].status.value == "pending"
    # control: without the policy the same workflow lets the agent approve
    _, eng2, _ = _engine(wf_budget, None)
    run2 = eng2.start_run("w")
    (a2,) = run2.approvals.values()
    assert eng2.approve(run2.id, a2.approval_id, principal=AGENT).status is RunStatus.running


def test_executor_outside_the_allowlist_fails_the_run_at_dispatch_without_calling_it():
    policy = OperatorPolicy(allowed_executors=frozenset({"echo"}))
    store, eng, ex = _engine(Budget(allow_agent_approval=False,
                                    allowed_effect_classes={EffectClass.compute, EffectClass.spend}),
                             policy, payer_executor="fancy-llm")
    run = eng.start_run("w")
    assert run.status is RunStatus.failed
    assert [c[0] for c in ex.calls] == ["n1"]
    assert "operator policy does not allow" in run.error and "allowed_executors: [echo]" in run.error
    assert "step.started" not in _types(store, run.id)[3:]  # nothing started for pay


def test_run_cost_ceiling_is_the_lower_of_workflow_and_policy():
    wf_budget = Budget(allowed_effect_classes={EffectClass.compute, EffectClass.spend},
                       max_run_cost="100.00")
    _store, eng, _ex = _engine(wf_budget, OperatorPolicy(max_run_cost="0.15"))
    run = eng.start_run("w")                     # each step costs 0.10; second step trips 0.15
    assert run.status is RunStatus.suspended
    (approval,) = run.approvals.values()
    assert approval.kind.value == "cost" and Decimal(approval.cost_at_request) == Decimal("0.20")


def test_no_policy_means_no_event_and_no_change():
    store, eng, _ex = _engine(FREE_SPEND, None)
    run = eng.start_run("w")
    assert run.status is RunStatus.completed and "governance.policy_applied" not in _types(store, run.id)
    assert run.policy_sha256 is None and run.policy_narrowed == []
    assert eng.policy is None and eng.policy_digest is None


# ------------------------------------------------------------------ loading

def test_load_policy_errors_name_the_variable_and_entry(tmp_path):
    with pytest.raises(PolicyError, match="AGENTOS_POLICY=.*file not found"):
        load_policy(tmp_path / "nope.json")
    p = tmp_path / "p.json"
    p.write_text("{oops")
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy(p)
    for payload, needle in [
        ({"effect_ceiling": ["spend", "teleport"]}, "effect_ceiling"),
        ({"max_run_cost": "-1"}, "max_run_cost.*non-negative"),
        ({"max_run_cost": "abc"}, "max_run_cost"),
        ({"max_step_wall_seconds": 0}, "max_step_wall_seconds.*> 0"),
        ({"version": 2}, "version"),
        ({"allowed_tools": ["x"]}, "allowed_tools"),          # not in this slice: refused, not ignored
    ]:
        p.write_text(json.dumps(payload))
        with pytest.raises(PolicyError, match=needle):
            load_policy(p)
    p.write_text(json.dumps({"effect_ceiling": ["read", "compute"], "allowed_executors": ["echo"]}))
    policy = load_policy(p)
    assert policy.effect_ceiling == frozenset({EffectClass.read, EffectClass.compute})


def test_the_example_policy_loads_and_names_every_shipped_executor():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "operator_policy.json"
    policy = load_policy(example)
    assert policy.allowed_executors is not None
    assert {"echo", "tool", "openai-compat", "anthropic", "openai-agents", "pydantic-ai"} \
        <= policy.allowed_executors
    assert policy.agent_approval_allowed is False and EffectClass.spend in policy.always_approve
    assert EffectClass.execute_code not in (policy.effect_ceiling or set())   # a real ceiling


def test_policy_from_env_warns_when_unset_and_loads_when_set(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    with caplog.at_level("WARNING", logger="agentos.policy"):
        assert policy_from_env() is None
    assert any("no operator ceiling" in r.getMessage() for r in caplog.records)
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"always_approve": ["spend"]}))
    monkeypatch.setenv("AGENTOS_POLICY", str(p))
    assert policy_from_env() == OperatorPolicy(always_approve=frozenset({EffectClass.spend}))
    monkeypatch.setenv("AGENTOS_POLICY", str(tmp_path / "missing.json"))
    with pytest.raises(PolicyError, match="AGENTOS_POLICY"):
        policy_from_env()


# ------------------------------------------------------------------ API

def test_get_policy_shows_the_ceiling_and_its_hash(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    p = tmp_path / "p.json"
    p.write_text(json.dumps({"effect_ceiling": ["spend", "read", "compute"],
                             "always_approve": ["spend"], "agent_approval_allowed": False}))
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    monkeypatch.setenv("AGENTOS_POLICY", str(p))
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    body = c.get("/policy").json()
    assert body["sha256"] == policy_sha256(load_policy(p))
    assert body["policy"]["effect_ceiling"] == ["compute", "read", "spend"]   # sorted, canonical
    assert body["policy"]["agent_approval_allowed"] is False

    # the same hash lands on every run
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [{"id": "n", "agent": "calc"}]})
    run = c.post("/workflows/w/runs", params={"sync": "true"}).json()
    assert run["policy_sha256"] == body["sha256"]
    ev = c.get(f"/runs/{run['id']}/events").json()["data"]
    assert ev[1]["event_type"] == "governance.policy_applied"
    # the default workflow budget gates write_external/send_message/execute_code; the ceiling
    # removes them from that tier, and the log says so
    assert ev[1]["narrowed"] == [("approval_required_for: removed execute_code, send_message, "
                                  "write_external (outside effect_ceiling)")]

    monkeypatch.delenv("AGENTOS_POLICY")
    importlib.reload(main)
    assert TestClient(main.app).get("/policy").json()["sha256"] is None
