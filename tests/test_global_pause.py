"""Global pause — the operator's kill switch (ROADMAP A12, audit E-03).

`agentos pause --all` sets ONE deployment-wide fact in the store (`scheduler_state`, an
append-only history whose newest row is the current state); `resume --all` clears it. It is
not a run event: the log is per run and replay must stay per-run deterministic, so the flag
never enters `run_events`. What the flag does:

  * the worker pulls nothing from the queue and the recovery sweep re-enqueues nothing;
  * a run already mid-advance records its in-flight wave, then stops BEFORE the next wave
    (the engine reads the flag at every wave boundary, so `?sync=true` honours it too);
  * runs are neither cancelled nor individually paused — they continue on `resume --all`;
  * a flag that cannot be read is treated as PAUSED (fail-closed): a kill switch that fails
    open under a store fault is no kill switch;
  * only the operator CLI (store access) writes it; the API exposes it read-only.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

import pytest

from dagentos.cli import main as cli_main
from dagentos.core.engine import Engine
from dagentos.core.events import SchedulerState, StepCompleted
from dagentos.core.models import Agent, AgentType, Principal, PrincipalKind, RunStatus
from dagentos.store.memory import MemoryStore
from dagentos.worker import Worker
from tests.test_control import ControllableExecutor, fan

OPERATOR = Principal(kind=PrincipalKind.human, id="amit")


def _make(executor):
    store = MemoryStore()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(fan())
    eng = Engine(store=store, blobs=store, executors={"echo": executor}, lease=store)
    worker = Worker(eng, store, lease=store, queue=store, holder="w", lease_ttl=30.0)
    return store, eng, worker


def _pause(store, reason="leaked provider key"):
    store.set_scheduler_state(SchedulerState(paused=True, principal=OPERATOR, reason=reason,
                                             changed_at=datetime.now(UTC)))


def _resume(store):
    store.set_scheduler_state(SchedulerState(paused=False, principal=OPERATOR, reason="rotated",
                                             changed_at=datetime.now(UTC)))


# ------------------------------------------------------------------ worker: queue boundary

def test_paused_worker_pulls_nothing_and_the_queued_run_waits_for_resume(caplog):
    ex = ControllableExecutor()
    store, eng, worker = _make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)
    _pause(store)

    with caplog.at_level(logging.INFO, logger="agentos.worker"):
        for _ in range(5):
            assert worker.run_once(timeout=0.02) is None
    assert ex.calls == []                                   # nothing leased, nothing dispatched
    assert store.queue_depth() == 1                         # the queue is untouched
    assert eng.get_run(run_id, hydrate=False).status is RunStatus.running   # NOT run.paused
    paused_lines = [r for r in caplog.records if "scheduler paused" in r.getMessage()]
    assert len(paused_lines) == 1, "logged once per transition, not once per tick"
    assert "human:amit" in paused_lines[0].getMessage()
    assert "leaked provider key" in paused_lines[0].getMessage()

    _resume(store)
    with caplog.at_level(logging.INFO, logger="agentos.worker"):
        assert worker.run_once(timeout=0.5) == run_id        # the very next tick takes it
    assert eng.get_run(run_id, hydrate=False).status is RunStatus.completed
    assert ex.calls[0] == "n1" and len(ex.calls) == 5
    assert sum("scheduler resumed" in r.getMessage() for r in caplog.records) == 1


def test_recovery_sweep_reenqueues_nothing_while_paused():
    ex = ControllableExecutor()
    store, eng, worker = _make(ex)
    run_id = eng.create_run("fan")                           # needs a worker, not queued
    _pause(store)
    assert worker.recover() == []
    assert store.queue_depth() == 0
    _resume(store)
    assert worker.recover() == [run_id]
    assert store.queue_depth() == 1


# ------------------------------------------------------------------ engine: wave boundary

def test_in_flight_step_finishes_after_a_mid_wave_pause_and_the_next_wave_waits_for_resume():
    ex = ControllableExecutor(hold={"n1": 0.4})
    store, eng, worker = _make(ex)
    run_id = eng.create_run("fan")
    store.push(run_id)

    t = threading.Thread(target=worker.run_once, kwargs={"timeout": 1.0})
    t.start()
    assert ex.started["n1"].wait(5)
    _pause(store)                                            # arrives while n1 is executing
    t.join(10)
    assert not t.is_alive()

    done = [e for e in store.read_events(run_id) if isinstance(e, StepCompleted)]
    assert [e.step_id for e in done] == ["n1"]               # the in-flight step is recorded
    assert ex.calls == ["n1"]                                # n2/n3/n4 were never dispatched
    run = eng.get_run(run_id, hydrate=False)
    assert run.status is RunStatus.running                   # not cancelled, not run.paused
    assert not run.pause_requested
    assert store.queue_depth() == 1                          # parked, not dropped
    assert worker.run_once(timeout=0.02) is None             # still paused: stays parked
    assert ex.calls == ["n1"]

    _resume(store)
    assert worker.run_once(timeout=0.5) == run_id
    assert eng.get_run(run_id, hydrate=False).status is RunStatus.completed
    assert sorted(ex.calls[1:4]) == ["n2", "n3", "n4"] and ex.calls[4] == "n5"


def test_sync_advance_honours_the_pause_too_so_the_api_cannot_bypass_it():
    """`POST …/runs?sync=true` runs `advance` inside the API process; the gate is in the
    engine's wave loop, not the worker, so that path dispatches nothing either."""
    ex = ControllableExecutor()
    store, eng, _worker = _make(ex)
    _pause(store)
    run = eng.start_run("fan", max_wait=1.0)
    assert run.status is RunStatus.running and ex.calls == []
    _resume(store)
    assert eng.advance_until_terminal(run.id, max_wait=5.0).status is RunStatus.completed


# ------------------------------------------------------------------ fail-closed

class _Unreadable(MemoryStore):
    def get_scheduler_state(self):
        raise ConnectionResetError("simulated: connection reset by peer")


def test_unreadable_scheduler_state_is_treated_as_paused(caplog):
    ex = ControllableExecutor()
    store = _Unreadable()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(fan())
    eng = Engine(store=store, blobs=store, executors={"echo": ex}, lease=store)
    worker = Worker(eng, store, lease=store, queue=store, holder="w")
    run_id = eng.create_run("fan")
    store.push(run_id)

    with caplog.at_level(logging.WARNING, logger="agentos"):
        for _ in range(3):
            assert worker.run_once(timeout=0.02) is None     # worker: pulls nothing
        assert worker.recover() == []                        # sweep: re-enqueues nothing
        assert eng.advance(run_id).status is RunStatus.running   # engine: dispatches nothing
    assert ex.calls == [] and store.queue_depth() == 1
    hits = [r for r in caplog.records if "scheduler state unreadable" in r.getMessage()]
    assert hits and all("ConnectionResetError" in r.getMessage() for r in hits)
    assert sum(r.name == "agentos.worker" for r in hits) == 1   # once per transition


# ------------------------------------------------------------------ CLI round trip

@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "cli.db"))
    return tmp_path


def test_cli_pause_status_resume_round_trip(sqlite_env, capsys):
    assert cli_main(["pause", "--status"]) == 0
    assert "running" in capsys.readouterr().out               # fresh store: never paused

    assert cli_main(["pause", "--all", "--reason", "leaked key", "--principal", "amit"]) == 0
    out = capsys.readouterr().out
    assert "paused" in out and "human:amit" in out and "leaked key" in out

    assert cli_main(["--json", "pause", "--status"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["paused"] is True and doc["reason"] == "leaked key"
    assert doc["principal"] == {"kind": "human", "id": "amit", "attestation": None}
    assert doc["changed_at"]

    assert cli_main(["resume", "--all"]) == 0
    assert cli_main(["pause", "--status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "system:cli" in out           # default principal when none named

    with pytest.raises(SystemExit):                           # neither --all nor --status
        cli_main(["pause"])


def test_cli_pause_is_visible_to_a_worker_on_the_same_store(sqlite_env):
    from dagentos.store.sqlite import SqliteStore
    assert cli_main(["pause", "--all", "--reason", "incident"]) == 0
    store = SqliteStore(sqlite_env / "cli.db")
    try:
        state = store.get_scheduler_state()
        assert state.paused and state.reason == "incident"
        assert state.principal == Principal(kind=PrincipalKind.system, id="cli")
    finally:
        store.close()


# ------------------------------------------------------------------ API: read-only

def _app(monkeypatch):
    import importlib
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.delenv("AGENTOS_AUTH_TOKENS", raising=False)
    monkeypatch.delenv("AGENTOS_AUTH", raising=False)
    from dagentos.api import main
    importlib.reload(main)
    return main


def test_api_shows_the_scheduler_state_and_cannot_set_it(monkeypatch):
    from fastapi.testclient import TestClient
    main = _app(monkeypatch)
    c = TestClient(main.app)
    assert c.get("/scheduler").json() == {"paused": False, "principal": None, "reason": "",
                                          "changed_at": None}
    _pause(main.store, reason="incident 42")
    doc = c.get("/scheduler").json()
    assert doc["paused"] is True and doc["reason"] == "incident 42"
    assert doc["principal"]["id"] == "amit" and doc["changed_at"]
    # Only the operator CLI (store access) sets or clears the flag: no verb on the API does.
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(c, verb)("/scheduler").status_code == 405, verb
    assert c.post("/scheduler/pause").status_code == 404
    assert main.store.get_scheduler_state().paused is True


def test_api_scheduler_route_fails_closed_with_the_exception_class_only(monkeypatch, caplog):
    from fastapi.testclient import TestClient
    main = _app(monkeypatch)
    monkeypatch.setattr(main, "store", _Unreadable())
    c = TestClient(main.app)
    with caplog.at_level("WARNING", logger="agentos.api"):
        r = c.get("/scheduler")
    assert r.status_code == 503
    assert r.json() == {"paused": True, "store": "ConnectionResetError"}
    assert "connection reset by peer" not in r.text and "connection reset by peer" in caplog.text
