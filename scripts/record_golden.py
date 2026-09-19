#!/usr/bin/env python3
"""Record a golden run log for tests/golden/.

Usage: python scripts/record_golden.py <label>
Writes tests/golden/<label>.json = {"events": [...records...], "expected": {folded run}}.
Run this once per released version; never edit an existing file — a log written by a
released version is immutable history, and the test proves the current build still
folds it identically (docs/DEVELOPMENT_STRUCTURE.md §5.3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dagentos.agents.echo import EchoExecutor
from dagentos.core.engine import Engine
from dagentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Provenance,
    StepResult,
    WorkflowDefinition,
)
from dagentos.core.policy import OperatorPolicy
from dagentos.core.seal import HmacKeyring
from dagentos.store.memory import MemoryStore

# From v0.9.0 every golden log also carries governance.policy_applied and integrity.sealed
# events. The keyring is a FIXTURE, not a secret: it exists so the recorded seals are
# well-formed events the fold must keep ignoring, and so verify_seals over a golden log has a
# key to judge them with (tests/golden/test_golden.py).
GOLDEN_KEYRING = HmacKeyring("golden", {"golden": bytes.fromhex("11" * 32)})
GOLDEN_POLICY = OperatorPolicy(always_approve=frozenset({EffectClass.spend}),
                               max_run_cost="5.00")

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"


class _Aliased:
    """Deterministic stand-in for a provider plugin (v0.5.0): resolves a different model
    for step 'd' than for 'a', so the golden log contains an `executor.substituted`."""

    name, version = "aliased", "golden"

    def resolve(self, req):
        return "model-B" if req.step_id == "d" else "model-A"

    def execute(self, req, progress):
        return StepResult(
            output={"model": self.resolve(req), "run": req.inputs.get("run")},
            effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=self.resolve(req), model_alias="chat.fast"))


def main(label: str) -> int:
    out = GOLDEN / f"{label}.json"
    if out.exists():
        print(f"refusing to overwrite {out} — golden files are immutable", file=sys.stderr)
        return 2
    store = MemoryStore()
    store.put_agent(Agent(name="greeter", type=AgentType.echo, config={"message": "hi"}))
    store.put_agent(Agent(name="writer", type=AgentType.llm, executor="aliased",
                          config={"model": "chat.fast"}))
    store.put_workflow(WorkflowDefinition(name="diamond", version=1, nodes=[
        {"id": "a", "agent": "writer"},
        {"id": "b", "agent": "greeter", "depends_on": ["a"]},
        {"id": "c", "agent": "greeter", "depends_on": ["a"]},
        {"id": "d", "agent": "writer", "depends_on": ["b", "c"]},
    ]))
    engine = Engine(store=store, blobs=store,
                    executors={"echo": EchoExecutor(), "aliased": _Aliased()},
                    policy=GOLDEN_POLICY, keyring=GOLDEN_KEYRING)
    run = engine.start_run("diamond", request_id=f"golden-{label}",
                           inputs={"topic": "golden"})
    events = [e.to_record() for e in store.read_events(run.id)]
    expected = engine.get_run(run.id, hydrate=False).model_dump(mode="json")
    out.write_text(json.dumps({"label": label, "events": events, "expected": expected},
                              indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(events)} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dev"))
