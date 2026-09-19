"""Durability at the API boundary: a run written through one app instance is readable,
byte-for-byte in its events, through a fresh instance over the same SQLite file. This is
the smallest honest version of "survives a restart"; the worker slice adds the crash."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "agentos.db"))

    def make():
        from dagentos.api import main
        importlib.reload(main)          # fresh composition root = "process restart"
        return main

    return make


def test_run_survives_app_restart_and_events_page_by_seq(app_factory):
    m1 = app_factory()
    store_before = m1.store
    c1 = TestClient(m1.app)
    c1.post("/agents", json={"name": "g", "type": "echo", "config": {"message": "hi"}})
    c1.post("/workflows", json={"name": "w", "nodes": [
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]})
    run = c1.post("/workflows/w/runs", params={"sync": "true"},
                  headers={"Idempotency-Key": "k-1"}).json()
    assert run["status"] == "completed"
    events_before = c1.get(f"/runs/{run['id']}/events").json()

    m2 = app_factory()                   # restart (reload returns the same module object,
    c2 = TestClient(m2.app)              #  but its globals are rebuilt from scratch)
    assert m2.store is not store_before
    fetched = c2.get(f"/runs/{run['id']}").json()
    assert fetched["status"] == "completed"
    assert [s["node_id"] for s in fetched["steps"]] == ["a", "b"]
    assert fetched["steps"][0]["output"]["message"] == "hi"    # blob hydrated after restart

    events_after = c2.get(f"/runs/{run['id']}/events").json()
    assert events_after == events_before
    last = events_after["last_seq"]
    assert last >= 6 and events_after["data"][-1]["event_type"] == "run.completed"
    page2 = c2.get(f"/runs/{run['id']}/events", params={"after": last - 2}).json()
    assert [e["seq"] for e in page2["data"]] == [last - 1, last]

    # Idempotency-Key survives the restart too: same key → same run, no new log —
    # whether the retry is sync or async.
    again = c2.post("/workflows/w/runs", headers={"Idempotency-Key": "k-1"})
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    assert c2.get("/runs/nope/events").status_code == 404
