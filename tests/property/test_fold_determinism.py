"""Property (a): state is the fold of the log — for random logs (docs/REPLAY.md, C14/C15).

A random workflow (`workflows()`: 1–6 nodes, gated steps, retries, scripted failures) is
driven through the real engine over the API's memory store with a random operator
interleaving drawn per state (`drive()`: advance / pause / resume / cancel / approve / reject
/ retry). Over the resulting log, for EVERY cut point k:

  1. `GET /runs/{id}?at=k` equals `fold(events[:k])` — the server's time-travel is the same
     fold over the prefix, no second derivation (tests/test_time_travel.py pins one log);
  2. `fold_from(fold(events[:k]), events[k:]) == fold(events)` — a snapshot at any point plus
     the tail folds to the same state as the whole log;

and for one drawn cut point the real snapshot path: the folded prefix is stored through
`Store.put_snapshot` (JSON round trip, as `Engine._maybe_snapshot` does), and both a direct
`fold_from` over the re-validated snapshot and `Engine.get_run` (which reads the snapshot and
folds only the tail) equal the full fold.

The steady-state invariants of ROADMAP → Chaos engineering plan are asserted on every log:
dense seq; exactly one `step.completed` per step; `step.started` precedes it; no (step,
attempt) executed twice (the executor's call log is the effect counter); the live
`GET /runs/{id}` equals the full fold up to hydration.
"""
from __future__ import annotations

import json
from collections import Counter

from hypothesis import event, given
from hypothesis import strategies as st

from dagentos.core.events import StepCompleted, StepStarted
from dagentos.core.fold import fold, fold_from
from dagentos.core.models import WorkflowRun

from .conftest import (
    PROPERTY,
    ScriptedExecutor,
    drive,
    fresh_name,
    make_engine,
    put_workflow,
    workflows,
)


def _json(run: WorkflowRun) -> dict:
    return json.loads(json.dumps(run.model_dump(mode="json"), default=str))


@PROPERTY
@given(wf=workflows(), data=st.data())
def test_every_prefix_folds_to_the_servers_state_and_any_snapshot_plus_tail_folds_to_the_whole(
        api, wf, data):
    main, c = api
    nodes, parallelism, crash_at = wf
    name = fresh_name("fold")
    put_workflow(main.store, name, nodes, parallelism)
    executor = ScriptedExecutor(crash_at)
    engine = make_engine(main.store, executor)                 # shares the API's store
    run_id, events = drive(engine, main.store, name, data)
    n = len(events)
    full = fold(events)
    # Visible under --hypothesis-show-statistics: proves the strategies reach every status
    # and event type, not only the happy path.
    event(f"final status: {full.status.value}")
    for event_type in sorted({type(e).event_type for e in events}):
        event(f"log has {event_type}")

    # Steady-state invariants (ROADMAP → Chaos engineering plan).
    assert [e.seq for e in events] == list(range(1, n + 1))
    completed = Counter(e.step_id for e in events if isinstance(e, StepCompleted))
    assert all(count == 1 for count in completed.values()), completed
    first_start = {}
    for e in events:
        if isinstance(e, StepStarted):
            first_start.setdefault(e.step_id, e.seq)
        elif isinstance(e, StepCompleted):
            assert first_start[e.step_id] < e.seq
    assert max(Counter(executor.calls).values(), default=0) <= 1, executor.calls
    assert {s.node_id for s in full.steps} == set(completed)
    assert full.integrity_verified == n

    # 1 + 2: every cut point.
    for k in range(1, n + 1):
        prefix = fold(events[:k])
        r = c.get(f"/runs/{run_id}", params={"at": k})
        assert r.status_code == 200, (k, r.text)
        assert r.json() == _json(prefix), f"?at={k} differs from fold(events[:{k}])"
        assert fold_from(prefix, events[k:]) == full, f"snapshot at {k} + tail != full fold"

    # The real snapshot path for one drawn cut point.
    k = data.draw(st.integers(1, n), label="snapshot seq")
    base = fold(events[:k])
    assert base.last_seq == k and base.last_hash == events[k - 1].hash
    state = base.model_dump(mode="json", exclude={"inputs"})
    main.store.put_snapshot(run_id, base.last_seq, base.last_hash, state)
    stored = main.store.get_snapshot(run_id)
    assert stored is not None and stored[0] == k
    assert fold_from(WorkflowRun.model_validate(stored[2]), events[k:]) == full
    assert engine.get_run(run_id, hydrate=False) == full

    # The live read is the same fold, hydrated (step outputs inlined — drop them on both sides).
    live = c.get(f"/runs/{run_id}").json()
    expected = _json(full)
    for r in (live, expected):
        for s in r["steps"]:
            s.pop("output", None)
    assert live == expected
