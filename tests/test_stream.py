"""Phase 7: `GET /runs/{id}/stream` — SSE that is a consumer of the log.

Properties under test:
- the stream emits every record of a finished run, `id` = seq, `event` = type, `data` is
  the same JSON `GET /runs/{id}/events` returns, and ends after the terminal event;
- `Last-Event-ID` (or `?after=`) resumes: only later events are sent;
- an event appended by a DIFFERENT engine instance (the worker process) appears — no
  shared in-memory state (C6);
- unknown run → 404 before the stream starts; garbage Last-Event-ID → 400;
- the connection is bounded by max_seconds and sends keep-alives while idle;
- a store error ends the stream instead of fabricating events;
- env parsing names the variable and applies the poll floor.
"""
from __future__ import annotations

import importlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from agentos.agents.echo import EchoExecutor
from agentos.api.stream import POLL_FLOOR_SECONDS, StreamConfig, parse_after, stream_run
from agentos.core.engine import Engine
from agentos.core.models import Agent, AgentType, WorkflowDefinition
from agentos.store.memory import MemoryStore
from agentos.store.sqlite import SqliteStore

FAST = StreamConfig(poll_seconds=0.01, keepalive_seconds=1000, max_seconds=1000)


def _frames(text: str) -> list[dict]:
    """Parse SSE text into [{id, event, data}] (comments are skipped)."""
    out = []
    for block in text.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            fields[k] = v
        if fields:
            out.append(fields)
    return out


# --------------------------------------------------------------------------- via the API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "agentos.db"))
    monkeypatch.setenv("AGENTOS_STREAM_POLL_SECONDS", "0.01")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    c.post("/agents", json={"name": "g", "type": "echo", "config": {"message": "hi"}})
    c.post("/workflows", json={"name": "w", "nodes": [
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]})
    return c


def test_stream_replays_a_finished_run_and_ends_after_the_terminal_event(api):
    run = api.post("/workflows/w/runs", params={"sync": "true"}).json()
    events = api.get(f"/runs/{run['id']}/events").json()["data"]

    with api.stream("GET", f"/runs/{run['id']}/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r.read().decode())

    assert [int(f["id"]) for f in frames] == [e["seq"] for e in events]
    assert [f["event"] for f in frames] == [e["event_type"] for e in events]
    assert [json.loads(f["data"]) for f in frames] == events     # same bytes as /events
    assert frames[-1]["event"] == "run.completed"                 # and nothing after it


def test_last_event_id_and_after_resume_from_a_seq(api):
    run = api.post("/workflows/w/runs", params={"sync": "true"}).json()
    last = api.get(f"/runs/{run['id']}/events").json()["last_seq"]

    with api.stream("GET", f"/runs/{run['id']}/stream",
                    headers={"Last-Event-ID": str(last - 2)}) as r:
        frames = _frames(r.read().decode())
    assert [int(f["id"]) for f in frames] == [last - 1, last]

    with api.stream("GET", f"/runs/{run['id']}/stream", params={"after": last - 1}) as r:
        frames = _frames(r.read().decode())
    assert [int(f["id"]) for f in frames] == [last]

    # Header wins over the query alias — and resuming AT the terminal seq ends at once
    # (the first draft waited out max_seconds here: no later event ever arrives).
    with api.stream("GET", f"/runs/{run['id']}/stream", params={"after": 0},
                    headers={"Last-Event-ID": str(last)}) as r:
        assert _frames(r.read().decode()) == []


def test_unknown_run_is_404_and_bad_last_event_id_is_400_before_streaming(api):
    assert api.get("/runs/nope/stream").status_code == 404
    run = api.post("/workflows/w/runs", params={"sync": "true"}).json()
    bad = api.get(f"/runs/{run['id']}/stream", headers={"Last-Event-ID": "seq-9"})
    assert bad.status_code == 400 and "Last-Event-ID" in bad.json()["detail"]
    assert api.get(f"/runs/{run['id']}/stream", headers={"Last-Event-ID": "-1"}).status_code == 400


# --------------------------------------------------------------------------- generator
def _engine(store):
    store.put_agent(Agent(name="g", type=AgentType.echo, config={"message": "hi"}))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]))
    return Engine(store=store, blobs=store, executors={"echo": EchoExecutor()})


def test_events_appended_by_another_engine_instance_appear_live(tmp_path):
    """The API process streams; a separate Engine (the worker) appends. The only thing
    they share is the store — exactly the two-process deployment."""
    path = str(tmp_path / "shared.db")
    api_store, worker_store = SqliteStore(path), SqliteStore(path)
    api_engine, worker = _engine(api_store), _engine(worker_store)
    run_id = api_engine.create_run("w")

    seen: list[str] = []
    gen = stream_run(api_store, run_id, after=0, config=FAST)
    seen.append(next(gen))                     # run.started, appended by the API side
    t = threading.Thread(target=worker.advance, args=(run_id,))
    t.start()
    seen.extend(gen)                           # polls until the worker's terminal event
    t.join(5)
    frames = _frames("".join(seen))
    assert frames[0]["event"] == "run.started" and frames[-1]["event"] == "run.completed"
    assert [int(f["id"]) for f in frames] == list(range(1, len(frames) + 1))
    assert "step.completed" in {f["event"] for f in frames}


def test_stream_is_bounded_by_max_seconds_and_sends_keepalives_while_idle():
    store = MemoryStore()
    engine = _engine(store)
    run_id = engine.create_run("w")              # only run.started; never advanced → idle
    clock = [0.0]

    def tick(seconds):                           # fake sleep advances the fake clock
        clock[0] += seconds

    cfg = StreamConfig(poll_seconds=1.0, keepalive_seconds=3.0, max_seconds=10.0)
    out = "".join(stream_run(store, run_id, after=0, config=cfg,
                             clock=lambda: clock[0], sleep=tick))
    assert out.count(": keep-alive") == 3       # at t=3, 6, 9 while nothing happened
    assert out.rstrip().endswith("reconnect with Last-Event-ID")
    assert clock[0] == 10.0                      # closed exactly at the bound
    assert [f["event"] for f in _frames(out)] == ["run.started"]


def test_a_store_error_ends_the_stream_without_fabricating_events(caplog):
    class Flaky(MemoryStore):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def read_events(self, run_id, after_seq=0):
            self.calls += 1
            if self.calls > 1:
                raise ConnectionError("db went away")
            return super().read_events(run_id, after_seq)

    store = Flaky()
    run_id = _engine(store).create_run("w")
    with caplog.at_level("WARNING", logger="agentos.api.stream"):
        out = "".join(stream_run(store, run_id, after=0, config=FAST, sleep=lambda s: None))
    assert [f["event"] for f in _frames(out)] == ["run.started"]
    assert "stream ended after seq 1" in caplog.text and "db went away" in caplog.text


def test_parse_after_and_env_validation(monkeypatch):
    assert parse_after(None, 4) == 4 and parse_after("", 4) == 4 and parse_after("7", 4) == 7
    with pytest.raises(ValueError):
        parse_after("x", 0)
    with pytest.raises(ValueError):
        parse_after("-1", 0)

    monkeypatch.setenv("AGENTOS_STREAM_POLL_SECONDS", "0.0001")      # below the floor
    assert StreamConfig.from_env().poll_seconds == POLL_FLOOR_SECONDS
    monkeypatch.setenv("AGENTOS_STREAM_MAX_SECONDS", "soon")
    with pytest.raises(RuntimeError, match="AGENTOS_STREAM_MAX_SECONDS"):
        StreamConfig.from_env()
    monkeypatch.setenv("AGENTOS_STREAM_MAX_SECONDS", "0")
    with pytest.raises(RuntimeError, match="AGENTOS_STREAM_MAX_SECONDS"):
        StreamConfig.from_env()
