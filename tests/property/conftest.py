"""Property-based tests (Hypothesis) — the executable form of the replay guarantee.

docs/REPLAY.md says state is the fold of the log and nothing else; tests/golden pins that for
a handful of recorded logs. These tests pin it for *random* logs: random DAGs driven through
the real engine with random executor outcomes and random operator interleavings, so the
claim is checked over the space of valid logs rather than the examples someone thought of.

Shared here (audit E-12):

* `workflows()` — a random DAG of 1–6 nodes. Node `n_i` depends on a random subset of
  `n_0..n_{i-1}` (acyclic by construction), runs agent `calc` (compute only) or `gated`
  (declares `write_external`, so the default budget suspends it for a human approval), with
  a `RetryPolicy(max_attempts 1..3, backoff 0)` and `max_parallelism` 1..4. The strategy
  also draws which (step, attempt) pairs the executor fails, so failures, retries and
  dead-letters appear in the logs.
* the operator interleaving is drawn interactively inside `drive()` from `st.data()`: at each
  step the pool depends on the run's state — `advance` (what a worker does), `pause`, `cancel`
  while running; `resume` while paused; `approve` / `reject` (first pending approval) while
  suspended; `retry` (first dead-lettered / failed step) once the run has failed. An action
  that is invalid for the run's state raises `ControlNotAllowed` / `RetryNotAllowed` and
  appends nothing — that is the contract, so it is swallowed, not avoided.
* `ScriptedExecutor` — outcome per (step, attempt) fixed up front, so a wave that runs
  steps concurrently never draws from Hypothesis off the main thread.
* `drive()` — create the run, apply the drawn interleaving, then advance until the run is
  terminal, paused or suspended (bounded), and return the log.
* `PROPERTY` — the settings every property uses: 50 examples, no deadline (a fold over an
  API client is I/O-shaped), `derandomize=True` so CI replays the same examples every run,
  and no example database so nothing is written under the repo.

Seeding: `derandomize=True` derives the seed from the test itself, so a CI failure reproduces
locally with the same command. To EXPLORE (fresh random seeds, more examples) set
`AGENTOS_PROPERTY_EXAMPLES=500`; that switches derandomize off and lets `--hypothesis-seed=N`
replay a run. Never set it in CI.
"""
from __future__ import annotations

import importlib
import itertools
import os
import threading

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

from dagentos.core.engine import ControlNotAllowed, Engine, RetryNotAllowed
from dagentos.core.events import Event
from dagentos.core.models import (
    Agent,
    AgentType,
    ApprovalStatus,
    Cost,
    Effect,
    EffectClass,
    Principal,
    PrincipalKind,
    Provenance,
    RetryPolicy,
    RunStatus,
    StepResult,
    WorkflowDefinition,
    WorkflowNode,
)
from dagentos.core.ports import Store

_EXPLORE = int(os.environ.get("AGENTOS_PROPERTY_EXAMPLES", "0"))
PROPERTY = settings(max_examples=_EXPLORE or 50, deadline=None, derandomize=not _EXPLORE,
                    database=None, suppress_health_check=[HealthCheck.too_slow])

HUMAN = Principal(kind=PrincipalKind.human, id="amit")
TERMINAL = frozenset({RunStatus.completed, RunStatus.failed, RunStatus.cancelled})
IDLE = TERMINAL | {RunStatus.paused, RunStatus.suspended}

CALC = Agent(name="calc", type=AgentType.echo)
GATED = Agent(name="gated", type=AgentType.echo,
              declared_effects=[EffectClass.compute, EffectClass.write_external])

# Unique workflow names: the API's memory store is shared by every example of a module.
_names = itertools.count()


def fresh_name(prefix: str) -> str:
    return f"{prefix}{next(_names)}"


class ScriptedExecutor:
    """Fails exactly the (step_id, attempt) pairs in `crash_at`; otherwise returns a
    StepResult whose effects are the step's declared effects. Thread-safe call log."""

    name, version = "scripted", "test"

    def __init__(self, crash_at: frozenset[tuple[str, int]] = frozenset()) -> None:
        self.crash_at = crash_at
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def execute(self, req, progress):
        with self._lock:
            self.calls.append((req.step_id, req.attempt))
        if (req.step_id, req.attempt) in self.crash_at:
            raise RuntimeError(f"{req.step_id} attempt {req.attempt}: scripted failure")
        return StepResult(
            output={"step": req.step_id, "attempt": req.attempt, "from": sorted(req.inputs)},
            effects=[Effect(effect_class=c) for c in sorted(req.declared_effects)],
            cost=Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version),
        )


@st.composite
def workflows(draw) -> tuple[list[WorkflowNode], int, frozenset[tuple[str, int]]]:
    """(nodes, max_parallelism, crash_at) for a random acyclic 1–6 node workflow."""
    n = draw(st.integers(min_value=1, max_value=6))
    nodes: list[WorkflowNode] = []
    crash_at: set[tuple[str, int]] = set()
    for i in range(n):
        deps = (draw(st.lists(st.sampled_from([f"n{j}" for j in range(i)]), unique=True))
                if i else [])
        agent = draw(st.sampled_from(["calc", "calc", "gated"]))
        attempts = draw(st.integers(min_value=1, max_value=3))
        nodes.append(WorkflowNode(id=f"n{i}", agent=agent, depends_on=sorted(deps),
                                  retry=RetryPolicy(max_attempts=attempts, backoff_seconds=0.0)))
        for attempt in range(1, attempts + 1):
            if draw(st.sampled_from([False, False, False, True])):
                crash_at.add((f"n{i}", attempt))
    parallelism = draw(st.integers(min_value=1, max_value=4))
    return nodes, parallelism, frozenset(crash_at)


ACTIONS = ("advance", "pause", "resume", "cancel", "approve", "reject", "retry")


def put_workflow(store: Store, name: str, nodes: list[WorkflowNode],
                 parallelism: int) -> WorkflowDefinition:
    store.put_agent(CALC)
    store.put_agent(GATED)
    wf = WorkflowDefinition(name=name, version=1, nodes=nodes, max_parallelism=parallelism)
    store.put_workflow(wf)
    return wf


def make_engine(store, executor: ScriptedExecutor) -> Engine:
    return Engine(store=store, blobs=store, executors={"echo": executor}, lease=store)


def _apply(engine: Engine, run_id: str, action: str) -> None:
    if action == "advance":
        engine.advance(run_id)
    elif action == "pause":
        engine.request_pause(run_id, principal=HUMAN)
    elif action == "resume":
        engine.resume(run_id, principal=HUMAN)
    elif action == "cancel":
        engine.request_cancel(run_id, principal=HUMAN)
    elif action in ("approve", "reject"):
        run = engine.get_run(run_id, hydrate=False)
        # Pick by step id, not approval id: approval ids are random uuids, and a draw that
        # depended on them would make the interleaving non-reproducible for Hypothesis.
        pending = sorted((a.step_id, a.approval_id) for a in run.approvals.values()
                         if a.status is ApprovalStatus.pending)
        if pending:
            getattr(engine, action)(run_id, pending[0][1], principal=HUMAN)
    elif action == "retry":
        run = engine.get_run(run_id, hydrate=False)
        reopenable = sorted({*run.dead_lettered, *run.failed_steps})
        if reopenable:
            engine.request_retry(run_id, reopenable[0], principal=HUMAN)
    else:  # pragma: no cover — the strategy only draws from ACTIONS
        raise AssertionError(action)


def _options(run) -> list[str]:
    """The actions worth drawing for this state (weighted by repetition); empty when nothing
    can move the run. `advance` is a no-op on a paused or suspended run, so it is not offered
    there; a few invalid requests stay in the pool on purpose (pause on paused, cancel on a
    failed run): a request the engine must refuse is part of the contract."""
    if run.status is RunStatus.paused:
        return ["resume", "resume", "resume", "cancel", "pause"]
    if run.status is RunStatus.suspended:
        return ["approve", "approve", "approve", "reject", "cancel", "pause"]
    if run.status is RunStatus.failed:
        return ["retry", "retry", "cancel"] if (run.dead_lettered or run.failed_steps) else []
    if run.status in TERMINAL:
        return []
    return ["advance", "advance", "advance", "pause", "cancel"]


def drive(engine: Engine, store: Store, wf_name: str, data: st.DataObject,
          *, max_actions: int = 24) -> tuple[str, list[Event]]:
    """Create a run and apply a random operator interleaving drawn interactively from `data`:
    each action is chosen from the pool `_options` gives for the run's CURRENT state, so
    approvals, resumes and retries are reached often, not only when a blind list happens to
    line up. Then advance until idle (bounded). Returns (run_id, log)."""
    run_id = engine.create_run(wf_name)
    for _ in range(data.draw(st.integers(0, max_actions), label="actions")):
        run = engine.get_run(run_id, hydrate=False)
        options = _options(run)
        if not options:
            break
        action = data.draw(st.sampled_from(options), label="action")
        try:
            _apply(engine, run_id, action)
        except (ControlNotAllowed, RetryNotAllowed):
            pass        # invalid for the run's current state: nothing appended, by contract
    for _ in range(12):
        run = engine.get_run(run_id, hydrate=False)
        if run.status in IDLE:
            break
        engine.advance(run_id)
    return run_id, store.read_events(run_id)


@pytest.fixture(scope="module")
def api():
    """The API over a memory store, built ONCE per module: Hypothesis refuses function-scoped
    fixtures (each example would see a different client), and the store it shares is exactly
    what `GET /runs/{id}?at=k` reads. Same env as tests/test_time_travel.py."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AGENTOS_STORE", "memory")
        mp.setenv("AGENTOS_AUTH", "asserted")
        for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR"):
            mp.delenv(k, raising=False)
        from dagentos.api import main
        importlib.reload(main)
        main.store.put_agent(CALC)
        main.store.put_agent(GATED)
        yield main, TestClient(main.app)
