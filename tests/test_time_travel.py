"""Phase 9 time-travel: `GET /runs/{id}?at=k` is the same fold over the log prefix.
  * at every k the state equals the fold of the first k events (and, by the golden pin, the
    full fold's own history) — no second derivation
  * bounds: k < 1 or k > last_seq → 422 naming the range; unknown run → 404
  * a tampered prefix is refused at k, not only at the tail
"""
from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient

from agentos.core.events import from_record
from agentos.core.fold import fold


def _client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR"):
        monkeypatch.delenv(k, raising=False)
    from agentos.api import main
    importlib.reload(main)
    return main, TestClient(main.app)


def _completed_run(c):
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [
        {"id": "a", "agent": "calc"}, {"id": "b", "agent": "calc", "depends_on": ["a"]},
        {"id": "c", "agent": "calc", "depends_on": ["a"]}, {"id": "d", "agent": "calc", "depends_on": ["b", "c"]}]})
    return c.post("/workflows/w/runs", params={"sync": "true"}).json()


def test_state_at_every_cut_point_is_the_prefix_fold(monkeypatch):
    _main, c = _client(monkeypatch)
    run = _completed_run(c)
    records = c.get(f"/runs/{run['id']}/events").json()["data"]
    events = [from_record(r) for r in records]
    assert run["status"] == "completed" and len(events) >= 9
    seen_statuses = set()
    for k in range(1, len(events) + 1):
        via_api = c.get(f"/runs/{run['id']}", params={"at": k}).json()
        expected = fold(events[:k]).model_dump(mode="json")
        assert via_api == json.loads(json.dumps(expected, default=str)), f"seq {k}"
        assert via_api["last_seq"] == k
        seen_statuses.add(via_api["status"])
    assert {"running", "completed"} <= seen_statuses
    assert c.get(f"/runs/{run['id']}", params={"at": 1}).json()["steps"] == []
    # ?at= is unhydrated (no step outputs inlined); everything else matches the live fold
    live = c.get(f"/runs/{run['id']}").json()
    at_last = c.get(f"/runs/{run['id']}", params={"at": len(events)}).json()
    for r in (live, at_last):
        for s in r["steps"]:
            s.pop("output", None)
    assert at_last == live


def test_bounds_and_unknown_run(monkeypatch):
    _main, c = _client(monkeypatch)
    run = _completed_run(c)
    last = run["last_seq"]
    for bad in (0, -1, last + 1):
        r = c.get(f"/runs/{run['id']}", params={"at": bad})
        assert r.status_code == 422 and f"1..{last}" in r.json()["detail"], bad
    assert c.get("/runs/nope", params={"at": 1}).status_code == 404
    assert c.get(f"/runs/{run['id']}", params={"at": "x"}).status_code == 422


def test_a_tampered_prefix_is_refused_at_k(monkeypatch):
    main, c = _client(monkeypatch)
    run = _completed_run(c)
    store = main.store
    # edit event 3 in place without rechaining: event 4's prev_hash no longer matches
    store._events[run["id"]][2]["occurred_at"] = "2001-01-01T00:00:00+00:00"   # the attacker's write
    r = c.get(f"/runs/{run['id']}", params={"at": 5})
    assert r.status_code == 500 and "integrity" in r.json()["detail"].lower()
    assert c.get(f"/runs/{run['id']}", params={"at": 2}).status_code == 200   # before the edit
