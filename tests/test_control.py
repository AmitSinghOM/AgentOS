"""Operator control (C5, remaining half of C4): cancel and pause as persisted events, a
cooperative cancellation token, no lost completions, and the single-writer exception
that lets the API append a control request while a worker holds the run."""
from __future__ import annotations

import threading
import time

import pytest

from agentos.core.engine import ControlNotAllowed, Engine
from agentos.core.events import (
    RunCancelled,
    RunCancelRequested,
    RunPaused,
    RunPauseRequested,
    RunResumed,
    StepCancelled,
    StepCompleted,
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
    RunStatus,
    StepResult,
    WorkflowDefinition,
)
from agentos.core.ports import ConflictError
from agentos.store.memory import MemoryStore
from agentos.worker import Worker

HUMAN = Principal(kind=PrincipalKind.human, id="amit")


class ControllableExecutor:
    """Per-step behaviour: `hold` seconds; `beats` → calls progress() every 20 ms (so it
    can be interrupted); `silent` → never calls progress (finishes regardless)."""

    name, version = "ctl", "test"

    def __init__(self, hold: dict[str, float] | None = None, silent: set[str] | None = None,
                 gate: dict[str, threading.Event] | None = None) -> None:
        self.hold, self.silent, self.gate = hold or {}, silent or set(), gate or {}
        self.calls: list[str] = []
        self.started = {k: threading.Event() for k in ("n1", "n2", "n3", "n4", "n5")}

    def execute(self, req, progress):
        self.calls.append(req.step_id)
        self.started[req.step_id].set()
        if req.step_id in self.gate:
            self.gate[req.step_id].wait(10)
        deadline = time.monotonic() + self.hold.get(req.step_id, 0.0)
        while time.monotonic() < deadline:
            time.sleep(0.02)
            if req.step_id not in self.silent:
                progress(0.5, "working")
        return StepResult(output={"step": req.step_id},
                          effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
                          provenance=Provenance(executor="ctl", executor_version="test"))


def fan() -> WorkflowDefinition:
    return WorkflowDefinition(name="fan", version=1, max_parallelism=4, nodes=[
        {"id": "n1", "agent": "a"},
        {"id": "n2", "agent": "a", "depends_on": ["n1"]},
        {"id": "n3", "agent": "a", "depends_on": ["n1"]},
        {"id": "n4", "agent": "a", "depends_on": ["n1"]},
        {"id": "n5", "agent": "a", "depends_on": ["n2", "n3", "n4"]},
    ])


def make(executor):
    store = MemoryStore()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(fan())
    eng = Engine(store=store, blobs=store, executors={"echo": executor}, lease=store)
    worker = Worker(eng, store, lease=store, queue=store, holder="w", lease_ttl=30.0)
    return store, eng, worker


def types(store, run_id):
    return [type(e).__name__ for e in store.read_events(run_id)]


# ----------------------------------------------------------------- cancel (C5)

def test_cancel_idle_run_is_finalized_immediately_and_never_executes():
    ex = ControllableExecutor()
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)

    run = eng.request_cancel(run_id, principal=HUMAN, reason="changed my mind")
    assert run.status is RunStatus.cancelled and run.ended_at is not None
    assert types(store, run_id) == ["RunStarted", "RunCancelRequested", "RunCancelled"]
    req = store.read_events(run_id)[1]
    assert isinstance(req, RunCancelRequested) and req.principal == HUMAN

    worker.run_once(timeout=0.2)                     # queued delivery: acked, nothing runs
    assert ex.calls == []
    assert store.pull(0.05) is None


def test_cancel_is_idempotent_and_refused_when_terminal():
    ex = ControllableExecutor(hold={"n1": 0.5})
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)
    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n1"].wait(5)
    eng.request_cancel(run_id, reason="first")
    eng.request_cancel(run_id, reason="second")       # idempotent while pending
    t.join(timeout=10)
    reqs = [e for e in store.read_events(run_id) if isinstance(e, RunCancelRequested)]
    assert len(reqs) == 1 and reqs[0].reason == "first"
    with pytest.raises(ControlNotAllowed):
        eng.request_cancel(run_id)                    # already cancelled → 409 at the API
    done = eng.start_run("fan")
    with pytest.raises(ControlNotAllowed):
        eng.request_cancel(done.id)                   # completed


def test_cancel_mid_wave_interrupts_heartbeating_steps_and_records_silent_ones():
    """The C4 promise: nothing that completed is lost; heartbeating steps stop at their
    next progress(); the run ends cancelled with the terminal event last."""
    ex = ControllableExecutor(hold={"n2": 3.0, "n3": 0.15, "n4": 3.0}, silent={"n3"})
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)

    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n2"].wait(5) and ex.started["n4"].wait(5)
    time.sleep(0.2)                                   # let silent n3 finish
    t0 = time.monotonic()
    eng.request_cancel(run_id, principal=HUMAN)       # worker holds the lease → not finalized here
    t.join(timeout=10)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 2.5, "cancel should interrupt, not wait out the 3 s holds"

    events = store.read_events(run_id)
    run = eng.get_run(run_id)
    assert run.status is RunStatus.cancelled
    assert isinstance(events[-1], RunCancelled)
    completed = {e.step_id for e in events if isinstance(e, StepCompleted)}
    cancelled = {e.step_id for e in events if isinstance(e, StepCancelled)}
    assert completed == {"n1", "n3"}                  # silent n3 finished → recorded
    assert cancelled == {"n2", "n4"}                  # heartbeating → interrupted
    assert run.cancelled_steps == sorted(cancelled) or set(run.cancelled_steps) == cancelled
    assert "n5" not in ex.calls                       # nothing new started
    assert store.pull(0.05) is None                   # acked
    assert store.acquire(run_id, "probe", 0.01) is not None   # lease released


def test_cancel_request_appended_by_api_is_adopted_by_worker_log_not_a_conflict():
    """The single-writer exception: while the worker holds the run, the API appends
    run.cancel_requested; the worker's next append must adopt it, not fail."""
    ex = ControllableExecutor(hold={"n1": 0.6}, silent={"n1"})
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)
    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n1"].wait(5)
    # Append directly (as the API would) while n1 is executing.
    run = eng.get_run(run_id, hydrate=False)
    store.append_events(run_id, run.last_seq, [RunCancelRequested(run_id=run_id, reason="api")])
    t.join(timeout=10)
    ev = types(store, run_id)
    # n1's completion landed AFTER the foreign request, i.e. the worker adopted the seq.
    assert ev.index("RunCancelRequested") < ev.index("StepCompleted")
    assert ev[-1] == "RunCancelled" and ex.calls == ["n1"]


def test_foreign_non_control_append_is_still_a_conflict():
    from agentos.core.engine import _Log
    from agentos.core.events import RunStarted, StepStarted
    store = MemoryStore()
    store.append_events("r", 0, [RunStarted(run_id="r", workflow="w", workflow_version=1,
                                            request_id="q")])
    log = _Log(store, "r", 1, None)
    store.append_events("r", 1, [StepStarted(run_id="r", step_id="x", attempt=1, agent="a",
                                             idempotency_key="k")])      # someone else's write
    with pytest.raises(ConflictError):
        log.append(StepStarted(run_id="r", step_id="y", attempt=1, agent="a",
                               idempotency_key="k2"))


# ------------------------------------------------------------- pause / resume

def test_pause_finishes_current_wave_then_stops_and_resume_completes():
    gate = {"n5": threading.Event()}
    ex = ControllableExecutor(hold={"n2": 0.3, "n3": 0.3, "n4": 0.3}, gate=gate)
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)
    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n2"].wait(5)
    eng.request_pause(run_id, principal=HUMAN, reason="inspect outputs")
    t.join(timeout=10)

    run = eng.get_run(run_id)
    assert run.status is RunStatus.paused
    assert {s.node_id for s in run.steps} == {"n1", "n2", "n3", "n4"}   # wave finished
    assert "n5" not in ex.calls                                          # nothing new
    ev = store.read_events(run_id)
    assert isinstance(ev[-1], RunPaused)
    assert any(isinstance(e, RunPauseRequested) and e.principal == HUMAN for e in ev)
    assert store.pull(0.05) is None                                      # left the queue
    assert worker.recover() == []                                        # sweep skips paused

    with pytest.raises(ControlNotAllowed):
        eng.request_pause(run_id)                                        # already paused
    gate["n5"].set()
    eng.resume(run_id, principal=HUMAN)
    store.push(run_id)
    assert worker.run_once(timeout=1.0) == run_id
    final = eng.get_run(run_id)
    assert final.status is RunStatus.completed and final.steps[-1].node_id == "n5"
    assert any(isinstance(e, RunResumed) and e.principal == HUMAN
               for e in store.read_events(run_id))
    with pytest.raises(ControlNotAllowed):
        eng.resume(run_id)                                               # not paused


def test_pause_idle_run_is_immediate_and_cancel_while_paused_is_immediate():
    ex = ControllableExecutor()
    store, eng, _ = make(ex)
    run_id = eng.create_run("fan")
    assert eng.request_pause(run_id).status is RunStatus.paused
    assert eng.advance(run_id).status is RunStatus.paused                # advance is a no-op
    assert ex.calls == []
    assert eng.request_cancel(run_id).status is RunStatus.cancelled
    assert types(store, run_id)[-1] == "RunCancelled"


def test_attempt_numbering_survives_cancel_and_started_precedes_cancelled():
    ex = ControllableExecutor(hold={"n1": 3.0})
    store, eng, worker = make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)
    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n1"].wait(5)
    eng.request_cancel(run_id)
    t.join(timeout=10)
    ev = store.read_events(run_id)
    st = next(e for e in ev if isinstance(e, StepStarted))
    ca = next(e for e in ev if isinstance(e, StepCancelled))
    assert st.seq < ca.seq and st.attempt == ca.attempt == 1


# -------------------------------------------------------------------- API

def test_control_endpoints(monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from agentos.api import main
    importlib.reload(main)
    client = TestClient(main.app)
    client.post("/agents", json={"name": "a", "type": "echo"})
    client.post("/workflows", json=fan().model_dump(mode="json"))
    run = client.post("/workflows/fan/runs").json()                      # 202, idle
    rid = run["id"]

    assert client.post(f"/runs/{rid}/resume").status_code == 409         # not paused
    assert client.post(f"/runs/{rid}/pause", json={"principal": {"kind": "human", "id": "amit"}}
                       ).json()["status"] == "paused"
    assert client.post(f"/runs/{rid}/pause").status_code == 409          # already paused
    assert client.post(f"/runs/{rid}/resume").json()["status"] == "running"
    resp = client.post(f"/runs/{rid}/cancel", json={"reason": "enough"})
    assert resp.status_code == 202 and resp.json()["status"] == "cancelled"
    assert client.post(f"/runs/{rid}/cancel").status_code == 409         # terminal
    assert client.post("/runs/nope/cancel").status_code == 404
    ev = client.get(f"/runs/{rid}/events").json()["data"]
    assert [e["event_type"] for e in ev] == [
        "run.started", "run.pause_requested", "run.paused", "run.resumed",
        "run.cancel_requested", "run.cancelled"]
    assert ev[1]["principal"]["id"] == "amit" and ev[4]["reason"] == "enough"
