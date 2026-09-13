"""Phase 2 scheduler: parallel waves, retries with exponential backoff, dead-letter after
the last attempt, and the human retry that reopens a run (C4, C9, C11)."""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agentos.core.engine import Engine, RetryNotAllowed
from agentos.core.events import (
    RunFailed,
    StepCompleted,
    StepDeadLettered,
    StepFailed,
    StepRetryRequested,
    StepStarted,
)
from agentos.core.models import (
    Agent,
    AgentType,
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
)
from agentos.store.memory import MemoryStore


class FlakyExecutor:
    """Fails step ids in `fail_first` for the first N attempts, then succeeds. Records
    wall-clock start/end per call so tests can prove concurrency."""

    name, version = "flaky", "test"

    def __init__(self, fail_first: dict[str, int] | None = None, hold: float = 0.0,
                 poison: set[str] | None = None) -> None:
        self.fail_first = dict(fail_first or {})
        self.hold, self.poison = hold, poison or set()
        self.calls: list[tuple[str, int]] = []
        self.spans: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def execute(self, req, progress):
        t0 = time.monotonic()
        with self._lock:
            self.calls.append((req.step_id, req.attempt))
        if self.hold:
            time.sleep(self.hold)
        with self._lock:
            self.spans.setdefault(req.step_id, []).append((t0, time.monotonic()))
        if req.step_id in self.poison:
            raise RuntimeError(f"{req.step_id} is poison")
        if self.fail_first.get(req.step_id, 0) >= req.attempt:
            raise RuntimeError(f"{req.step_id} flaked on attempt {req.attempt}")
        return StepResult(output={"step": req.step_id, "attempt": req.attempt,
                                  "from": sorted(req.inputs)},
                          effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                          provenance=Provenance(executor="flaky", executor_version="test"))


class PinnedWall:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def fan_out_in(retry: RetryPolicy | None = None, parallelism: int = 4) -> WorkflowDefinition:
    """The ROADMAP Phase 2 demo: 1 → [2,3,4] → 5."""
    r = retry or RetryPolicy()
    return WorkflowDefinition(name="fan", version=1, max_parallelism=parallelism, nodes=[
        {"id": "n1", "agent": "a"},
        {"id": "n2", "agent": "a", "depends_on": ["n1"], "retry": r},
        {"id": "n3", "agent": "a", "depends_on": ["n1"], "retry": r},
        {"id": "n4", "agent": "a", "depends_on": ["n1"], "retry": r},
        {"id": "n5", "agent": "a", "depends_on": ["n2", "n3", "n4"]},
    ])


def make(executor, wf, wall=None):
    store = MemoryStore()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(wf)
    eng = Engine(store=store, blobs=store, executors={"echo": executor}, wall=wall or PinnedWall())
    return store, eng


# ------------------------------------------------------------- parallel waves (C9)

def test_independent_branches_run_concurrently_and_fan_in_waits():
    ex = FlakyExecutor(hold=0.25)
    _store, eng = make(ex, fan_out_in(parallelism=3))
    t0 = time.monotonic()
    run = eng.start_run("fan")
    wall = time.monotonic() - t0

    assert run.status is RunStatus.completed
    order = [s.node_id for s in run.steps]
    assert order[0] == "n1" and order[-1] == "n5"
    # 3 waves × 0.25 s ≈ 0.75 s; strictly sequential would be 5 × 0.25 = 1.25 s.
    assert wall < 1.1, f"branches did not overlap: {wall:.2f}s"
    s2, s3, s4 = (ex.spans[k][0] for k in ("n2", "n3", "n4"))
    assert max(s2[0], s3[0], s4[0]) < min(s2[1], s3[1], s4[1])   # all three overlapped
    assert ex.spans["n5"][0][0] >= max(s2[1], s3[1], s4[1])       # fan-in waited
    assert run.steps[-1].output["from"] == ["n2", "n3", "n4"]


def test_parallelism_is_bounded():
    ex = FlakyExecutor(hold=0.2)
    _, eng = make(ex, fan_out_in(parallelism=1))
    t0 = time.monotonic()
    eng.start_run("fan")
    assert time.monotonic() - t0 >= 0.95                           # 5 × 0.2, no overlap


# ----------------------------------------------------------- retries with backoff

def test_flaky_branch_retries_with_backoff_and_run_completes():
    """The demo: one branch fails once, retries after backoff, the run completes."""
    wall = PinnedWall()
    ex = FlakyExecutor(fail_first={"n3": 1})
    store, eng = make(ex, fan_out_in(RetryPolicy(max_attempts=3, backoff_seconds=30)), wall)

    run_id = eng.create_run("fan")
    run = eng.advance(run_id)                                      # wave 2: n3 fails

    assert run.status is RunStatus.running
    assert set(run.pending_retries) == {"n3"}
    assert run.pending_retries["n3"] == wall.now + timedelta(seconds=30)
    assert eng.next_retry_delay(run) == 30.0
    assert {s.node_id for s in run.steps} == {"n1", "n2", "n4"}   # siblings recorded (C4)
    failed = [e for e in store.read_events(run_id) if isinstance(e, StepFailed)]
    assert failed[0].terminal is False and failed[0].retry_at == run.pending_retries["n3"]

    run = eng.advance(run_id)                                      # too early: nothing runs
    assert run.status is RunStatus.running and len(ex.calls) == 4  # n1, n2, n3, n4 only

    wall.now += timedelta(seconds=30)
    run = eng.advance(run_id)
    assert run.status is RunStatus.completed
    assert run.attempts["n3"] == 2 and run.pending_retries == {}
    assert [c for c in ex.calls if c[0] == "n3"] == [("n3", 1), ("n3", 2)]
    assert run.steps[-1].node_id == "n5"


def test_backoff_is_exponential_and_capped():
    p = RetryPolicy(max_attempts=6, backoff_seconds=2, backoff_multiplier=3, max_backoff_seconds=40)
    assert [p.delay_before(a) for a in (1, 2, 3, 4, 5, 6)] == [0, 2, 6, 18, 40, 40]


def test_poison_step_dead_letters_after_last_attempt_with_cause():
    wall = PinnedWall()
    ex = FlakyExecutor(poison={"n2"})
    store, eng = make(ex, fan_out_in(RetryPolicy(max_attempts=3, backoff_seconds=1)), wall)
    run_id = eng.create_run("fan")
    for _ in range(3):
        eng.advance(run_id)
        wall.now += timedelta(seconds=5)
    run = eng.get_run(run_id)

    assert run.status is RunStatus.failed
    assert [c for c in ex.calls if c[0] == "n2"] == [("n2", 1), ("n2", 2), ("n2", 3)]
    events = store.read_events(run_id)
    failed = [e for e in events if isinstance(e, StepFailed) and e.step_id == "n2"]
    assert [f.terminal for f in failed] == [False, False, True]
    dl = next(e for e in events if isinstance(e, StepDeadLettered))
    assert dl.step_id == "n2" and dl.attempt == 3 and "after 3 attempt(s)" in dl.cause
    assert "poison" in run.dead_lettered["n2"]
    assert {s.node_id for s in run.steps} == {"n1", "n3", "n4"}   # healthy siblings kept
    assert isinstance(events[-1], RunFailed)                        # terminal event is last


def test_sync_path_sleeps_through_short_backoff():
    ex = FlakyExecutor(fail_first={"n2": 1})
    _, eng = make(ex, fan_out_in(RetryPolicy(max_attempts=2, backoff_seconds=0.05)),
                  wall=lambda: datetime.now(UTC))
    run = eng.start_run("fan")
    assert run.status is RunStatus.completed and run.attempts["n2"] == 2


# ------------------------------------------------------------- human retry (C11)

def test_retry_request_reopens_dead_lettered_step_and_records_principal():
    ex = FlakyExecutor(poison={"n2"})
    store, eng = make(ex, fan_out_in())
    run = eng.start_run("fan")
    assert run.status is RunStatus.failed and "n2" in run.dead_lettered

    ex.poison.clear()                                              # "fixed the agent"
    who = Principal(kind=PrincipalKind.human, id="amit")
    reopened = eng.request_retry(run.id, "n2", principal=who, reason="fixed upstream")
    assert reopened.status is RunStatus.running
    assert reopened.dead_lettered == {} and reopened.error is None
    assert {s.node_id for s in reopened.steps} == {"n1", "n3", "n4"}  # nothing lost

    final = eng.advance(run.id)
    assert final.status is RunStatus.completed
    assert final.attempts["n2"] == 2                               # continues, not restarts
    # Healthy steps were replayed from the log, not re-executed.
    assert ex.calls.count(("n1", 1)) == 1
    assert ex.calls.count(("n3", 1)) == 1 and ex.calls.count(("n4", 1)) == 1
    assert ex.calls.count(("n5", 1)) == 1
    req = next(e for e in store.read_events(run.id) if isinstance(e, StepRetryRequested))
    assert req.principal == who and req.reason == "fixed upstream"
    # Attempt numbering is continuous across the reopen.
    starts = [e.attempt for e in store.read_events(run.id)
              if isinstance(e, StepStarted) and e.step_id == "n2"]
    assert starts == [1, 2]


def test_retry_request_refused_for_healthy_or_unknown_step():
    _, eng = make(FlakyExecutor(), fan_out_in())
    run = eng.start_run("fan")
    with pytest.raises(RetryNotAllowed):
        eng.request_retry(run.id, "n2")                            # completed fine
    with pytest.raises(RetryNotAllowed):
        eng.request_retry(run.id, "nope")
    with pytest.raises(KeyError):
        eng.request_retry("no-such-run", "n2")


def test_retry_via_api_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    import importlib

    from agentos.api import main
    importlib.reload(main)
    client = TestClient(main.app)
    # Swap in a poison executor behind the API's engine.
    ex = FlakyExecutor(poison={"n2"})
    main.engine._executors = {"echo": ex}
    client.post("/agents", json={"name": "a", "type": "echo"})
    client.post("/workflows", json=fan_out_in().model_dump(mode="json"))
    run = client.post("/workflows/fan/runs", params={"sync": "true"}).json()
    assert run["status"] == "failed" and "n2" in run["dead_lettered"]

    assert client.post(f"/runs/{run['id']}/steps/n3/retry").status_code == 409   # healthy
    assert client.post("/runs/nope/steps/n2/retry").status_code == 404

    ex.poison.clear()
    resp = client.post(f"/runs/{run['id']}/steps/n2/retry", params={"sync": "true"},
                       json={"principal": {"kind": "human", "id": "amit"}, "reason": "fixed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    events = client.get(f"/runs/{run['id']}/events").json()["data"]
    rr = next(e for e in events if e["event_type"] == "step.retry_requested")
    assert rr["principal"]["kind"] == "human" and rr["reason"] == "fixed"


def test_completed_sibling_is_never_lost_when_wave_mate_dead_letters():
    """C4: n2 dead-letters in the same wave as n3/n4 completing → both are recorded."""
    ex = FlakyExecutor(poison={"n2"}, hold=0.05)
    store, eng = make(ex, fan_out_in())
    run = eng.start_run("fan")
    assert run.status is RunStatus.failed
    completed = {e.step_id for e in store.read_events(run.id) if isinstance(e, StepCompleted)}
    assert completed == {"n1", "n3", "n4"}
