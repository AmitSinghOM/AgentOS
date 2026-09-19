"""Phase 0 quick-start check: the README walkthrough, driven by the literal
example files, must work end to end. This is the demo the phase is gated on."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def app(monkeypatch):
    """A fresh composition root per test: agent versions are immutable, so tests that
    register the same (name, version) with different bodies must not share a store."""
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from dagentos.api import main
    importlib.reload(main)
    return main.app


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def test_readme_quickstart_with_example_files(app):
    client = TestClient(app)

    agent = _load("echo_agent.json")
    assert client.post("/agents", json=agent).status_code == 201
    assert any(a["name"] == agent["name"] for a in client.get("/agents").json()["data"])

    wf = _load("hello_workflow.json")
    assert client.post("/workflows", json=wf).status_code == 201

    run = client.post(f"/workflows/{wf['name']}/runs", params={"sync": "true"}).json()
    assert run["status"] == "completed"
    assert [s["node_id"] for s in run["steps"]] == [n["id"] for n in wf["nodes"]]
    # The echo agent surfaces its configured message as the step output.
    assert all(agent["config"]["message"] in json.dumps(s["output"]) for s in run["steps"])

    fetched = client.get(f"/runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "completed"


def test_quickstart_requires_json_content_type(app):
    """curl -d @file sends form-encoded by default; the README must pass the
    JSON header or the registry call is rejected. Lock the behaviour so the
    README instruction stays honest."""
    client = TestClient(app)
    body = (EXAMPLES / "echo_agent.json").read_bytes()
    resp = client.post(
        "/agents", content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 422


def test_unknown_workflow_and_run_are_404(app):
    client = TestClient(app)
    assert client.post("/workflows/does-not-exist/runs").status_code == 404
    assert client.get("/runs/nope").status_code == 404
