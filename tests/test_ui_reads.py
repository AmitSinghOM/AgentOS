"""Phase 9 graph: the two read endpoints the run page needs. Both are data routes and so are
authenticated like every other; `/runs` is newest-first and clamped; `/workflows/{name}` is the
CURRENT definition and says nothing about pinned versions — the UI compares."""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR"):
        monkeypatch.delenv(k, raising=False)
    from agentos.api import main
    importlib.reload(main)
    return TestClient(main.app)


def test_runs_list_is_newest_first_clamped_and_summarised(monkeypatch):
    c = _client(monkeypatch)
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/agents", json={"name": "payer", "type": "echo", "declared_effects": ["compute", "spend"]})
    c.post("/workflows", json={"name": "w", "nodes": [{"id": "n", "agent": "calc"}]})
    c.post("/workflows", json={"name": "gated", "nodes": [{"id": "pay", "agent": "payer"}]})
    first = c.post("/workflows/w/runs", params={"sync": "true"}).json()["id"]
    second = c.post("/workflows/gated/runs", params={"sync": "true"}).json()["id"]
    body = c.get("/runs").json()["data"]
    assert [r["id"] for r in body] == [second, first]                # newest first
    assert body[0]["status"] == "suspended" and body[0]["pending_approvals"] == 1
    assert body[1]["status"] == "completed" and body[1]["pending_approvals"] == 0
    assert set(body[0]) == {"id", "workflow", "workflow_version", "status", "total_cost",
                            "last_seq", "started_at", "pending_approvals"}
    assert len(c.get("/runs", params={"limit": 1}).json()["data"]) == 1
    assert len(c.get("/runs", params={"limit": 0}).json()["data"]) == 1        # clamped to 1
    assert len(c.get("/runs", params={"limit": 10000}).json()["data"]) == 2    # clamped to 500


def test_workflow_definition_is_readable_and_404s_when_unknown(monkeypatch):
    c = _client(monkeypatch)
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [
        {"id": "a", "agent": "calc"}, {"id": "b", "agent": "calc", "depends_on": ["a"]}]})
    wf = c.get("/workflows/w").json()
    assert wf["name"] == "w" and wf["version"] == 1
    assert [n["id"] for n in wf["nodes"]] == ["a", "b"] and wf["nodes"][1]["depends_on"] == ["a"]
    assert c.get("/workflows/nope").status_code == 404


def test_both_are_authenticated_in_bearer_mode(monkeypatch, tmp_path):
    import json
    from hashlib import sha256
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({"principals": [{"sha256": sha256(b"tok").hexdigest(),
                                                  "kind": "human", "id": "amit"}]}))
    monkeypatch.setenv("AGENTOS_AUTH_TOKENS", str(tokens))
    c = _client(monkeypatch)
    monkeypatch.setenv("AGENTOS_AUTH", "bearer")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    assert c.get("/runs").status_code == 401 and c.get("/workflows/w").status_code == 401
    assert c.get("/runs", headers={"Authorization": "Bearer tok"}).status_code == 200
