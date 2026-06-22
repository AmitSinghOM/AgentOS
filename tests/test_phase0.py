from fastapi.testclient import TestClient

from agentos.api.main import app
from agentos.core.models import WorkflowDefinition

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_end_to_end_run():
    client.post("/agents", json={"name": "greeter", "type": "echo",
                                 "config": {"message": "hi"}})
    wf = {"name": "hello", "nodes": [
        {"id": "a", "agent": "greeter", "depends_on": []},
        {"id": "b", "agent": "greeter", "depends_on": ["a"]},
    ]}
    assert client.post("/workflows", json=wf).status_code == 201

    run = client.post("/workflows/hello/runs", json={}).json()
    assert run["status"] == "completed"
    assert [s["node_id"] for s in run["steps"]] == ["a", "b"]

    fetched = client.get(f"/runs/{run['id']}").json()
    assert fetched["status"] == "completed"


def test_cycle_rejected():
    wf = WorkflowDefinition(name="loop", nodes=[
        {"id": "x", "agent": "greeter", "depends_on": ["y"]},
        {"id": "y", "agent": "greeter", "depends_on": ["x"]},
    ])
    try:
        wf.validate_dag()
        assert False, "expected cycle to be rejected"
    except ValueError as e:
        assert "cycle" in str(e)
