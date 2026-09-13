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

from agentos.agents.echo import EchoExecutor
from agentos.core.engine import Engine
from agentos.core.models import Agent, AgentType, WorkflowDefinition
from agentos.store.memory import MemoryStore

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"


def main(label: str) -> int:
    out = GOLDEN / f"{label}.json"
    if out.exists():
        print(f"refusing to overwrite {out} — golden files are immutable", file=sys.stderr)
        return 2
    store = MemoryStore()
    store.put_agent(Agent(name="greeter", type=AgentType.echo, config={"message": "hi"}))
    store.put_workflow(WorkflowDefinition(name="diamond", version=1, nodes=[
        {"id": "a", "agent": "greeter"},
        {"id": "b", "agent": "greeter", "depends_on": ["a"]},
        {"id": "c", "agent": "greeter", "depends_on": ["a"]},
        {"id": "d", "agent": "greeter", "depends_on": ["b", "c"]},
    ]))
    engine = Engine(store=store, blobs=store, executors={"echo": EchoExecutor()})
    run = engine.start_run("diamond", request_id=f"golden-{label}")
    events = [e.to_record() for e in store.read_events(run.id)]
    expected = engine.get_run(run.id, hydrate=False).model_dump(mode="json")
    out.write_text(json.dumps({"label": label, "events": events, "expected": expected},
                              indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(events)} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dev"))
