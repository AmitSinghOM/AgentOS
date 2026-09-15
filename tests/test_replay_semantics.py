"""C14 (#14): state replay, not code replay. A step that returns a random value replays to
the RECORDED value; agent code may be non-deterministic. docs/REPLAY.md."""
from __future__ import annotations

import random

from agentos.core.engine import Engine
from agentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Provenance,
    RetryPolicy,
    RunStatus,
    StepResult,
    WorkflowDefinition,
)
from agentos.store.memory import MemoryStore


class Dice:
    """Non-deterministic on purpose: every call returns a fresh random number and, on the
    first call for step 'b', crashes — so the run has to be resumed."""

    name, version = "dice", "t"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.crashed = False

    def execute(self, req, progress) -> StepResult:
        self.calls.append(req.step_id)
        if req.step_id == "b" and not self.crashed:
            self.crashed = True
            raise RuntimeError("worker died mid-step")
        return StepResult(output={"roll": random.random()},
                          effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                          provenance=Provenance(executor=self.name, executor_version=self.version))


def _setup():
    store = MemoryStore()
    store.put_agent(Agent(name="d", type=AgentType.echo, executor="dice"))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "d"},
        {"id": "b", "agent": "d", "depends_on": ["a"], "retry": RetryPolicy(max_attempts=2, backoff_seconds=0.3)},   # hand-off point
        {"id": "c", "agent": "d", "depends_on": ["b"]}]))
    dice = Dice()
    return store, Engine(store=store, blobs=store, executors={"dice": dice}, lease=store), dice


def test_a_random_step_replays_to_the_recorded_value_not_a_new_roll():
    store, eng, dice = _setup()
    run_id = eng.create_run("w")
    first = eng.advance(run_id)                      # a completes; b crashes → retry pending
    assert first.status is RunStatus.running and [s.node_id for s in first.steps] == ["a"]
    recorded_a = first.steps[0].output["roll"]

    # "Resume": a fresh engine over the same store — the way a worker picks a run up
    # after a crash. Nothing is re-executed for `a`; its recorded roll is what `b` sees.
    eng2 = Engine(store=store, blobs=store, executors={"dice": dice}, lease=store)
    final = eng2.advance_until_terminal(run_id)
    assert final.status is RunStatus.completed
    assert final.steps[0].output["roll"] == recorded_a          # replayed, not re-rolled
    assert dice.calls == ["a", "b", "b", "c"]                    # a ran exactly once
    # And a third fold of the log — the "replay" — is byte-identical state.
    again = Engine(store=store, blobs=store, executors={"dice": Dice()}, lease=store)
    assert again.get_run(run_id).model_dump() == final.model_dump()
    assert again.get_run(run_id).steps[0].output["roll"] == recorded_a


def test_agent_code_may_change_between_attempts_without_breaking_replay():
    """Temporal-style code replay would flag a changed activity order or a new random
    call as non-determinism. Here the scheduler replays STATE; the executor is free."""
    store, eng, _dice = _setup()
    run_id = eng.create_run("w")
    eng.advance(run_id)

    class NewDice(Dice):                               # different implementation resumes it
        def execute(self, req, progress):
            random.random(); random.random()           # extra randomness, different code path
            return StepResult(output={"roll": 7, "version": 2},
                              effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                              provenance=Provenance(executor="dice", executor_version="v2"))

    eng2 = Engine(store=store, blobs=store, executors={"dice": NewDice()}, lease=store)
    final = eng2.advance_until_terminal(run_id)
    assert final.status is RunStatus.completed
    assert final.steps[0].provenance.executor_version == "t"       # a: recorded, old code
    assert final.steps[1].provenance.executor_version == "v2"      # b, c: new code, recorded
    assert final.steps[1].output == {"roll": 7, "version": 2}
