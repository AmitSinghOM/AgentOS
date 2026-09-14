"""docs/quickstart-llm.md, executed: the exact example agents and workflow, through the
real API and engine, against the cassette recorded from a live Ollama. If this passes,
the quickstart's commands produce what the page says they do."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("agentos_provider_openai_compat")

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
CASSETTES = ROOT / "providers" / "openai-compat" / "tests" / "cassettes"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTES", "replay")
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTE_DIR", str(CASSETTES))
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTE", "quickstart")
    from agentos.api import main
    importlib.reload(main)
    return TestClient(main.app)


def _post_json(c, path, file):
    r = c.post(path, content=(EXAMPLES / file).read_text(),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text


def test_quickstart_llm_as_written(client):
    c = client
    # 3. check what can run
    ex = {e["name"]: e for e in c.get("/executors").json()}
    assert {"echo", "openai-compat"} <= set(ex)
    d = ex["openai-compat"]
    assert d["describe"]["aliases"]["chat.fast"] == "qwen2.5:0.5b"
    assert d["describe"]["cassettes"] == "replay" and "reachable" in d["health"]

    # 4. define the agents and the workflow
    _post_json(c, "/agents", "poet_agent.json")
    _post_json(c, "/agents", "critic_agent.json")
    _post_json(c, "/workflows", "haiku_workflow.json")

    # 5. run it (sync variant so the test needs no worker process)
    r = c.post("/workflows/haiku/runs?sync=true", json={"inputs": {"topic": "event logs"}})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "completed", run["error"]
    assert run["inputs"] == {"topic": "event logs"}
    write, review = run["steps"]
    assert write["node_id"] == "write" and write["output"]["text"].strip()
    assert write["output"]["alias"] == "chat.fast" and write["output"]["model"] == "qwen2.5:0.5b"
    assert review["node_id"] == "review"
    assert isinstance(review["output"]["json"]["score"], int | float)
    for step in (write, review):
        p = step["provenance"]
        assert p["executor"] == "openai-compat" and p["model_id"] == "qwen2.5:0.5b"
        assert p["model_alias"] == "chat.fast" and len(p["prompt_hash"]) == 64
        names = {m["name"] for m in step["cost"]["units"]}
        assert names == {"input_tokens", "output_tokens", "requests"}
        assert step["cost"]["amount"] == "0" and step["cost"]["pricing_snapshot_hash"]
    assert run["total_cost"] == "0" and run["substitutions"] == []

    # the pricing table behind every step's hash is fetchable
    table = c.get(f"/blobs/{write['cost']['pricing_snapshot_hash']}")
    assert table.status_code == 200 and table.json()["schema"] == "agentos.pricing/1"

    # the events say what happened, in order
    types = [e["event_type"] for e in c.get(f"/runs/{run['id']}/events?after=0").json()["data"]]
    assert types[:2] == ["run.started", "step.started"]
    assert types.count("step.completed") == 2 and types[-1] == "run.completed"
    assert "step.progress" in types                       # the provider reports progress


def test_examples_are_valid_definitions():
    for f in ("poet_agent.json", "critic_agent.json"):
        a = json.loads((EXAMPLES / f).read_text())
        assert a["type"] == "llm" and a["executor"] == "openai-compat"
        assert a["config"]["model"] == "chat.fast"
    wf = json.loads((EXAMPLES / "haiku_workflow.json").read_text())
    assert [n["id"] for n in wf["nodes"]] == ["write", "review"]


def test_same_agents_run_on_the_anthropic_wire_format(monkeypatch):
    """The plugin seam, proven: change `executor`, nothing else. Replays the cassette the
    Anthropic provider recorded from the same local Ollama via /v1/messages."""
    pytest.importorskip("agentos_provider_anthropic")
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_ANTHROPIC_CASSETTES", "replay")
    monkeypatch.setenv("AGENTOS_ANTHROPIC_CASSETTE_DIR",
                       str(ROOT / "providers" / "anthropic" / "tests" / "cassettes"))
    monkeypatch.setenv("AGENTOS_ANTHROPIC_CASSETTE", "quickstart")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    for f in ("poet_agent.json", "critic_agent.json"):
        a = json.loads((EXAMPLES / f).read_text())
        a["executor"] = "anthropic"
        assert c.post("/agents", json=a).status_code == 201
    _post_json(c, "/workflows", "haiku_workflow.json")
    run = c.post("/workflows/haiku/runs?sync=true", json={"inputs": {"topic": "event logs"}}).json()
    assert run["status"] == "completed", run["error"]
    write, review = run["steps"]
    assert write["provenance"]["executor"] == "anthropic"
    assert write["provenance"]["model_id"] == "qwen2.5:0.5b" and write["output"]["text"].strip()
    assert isinstance(review["output"]["json"]["score"], int | float)
    assert run["total_cost"] == "0"
