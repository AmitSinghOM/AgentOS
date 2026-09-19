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
    typed = json.loads((EXAMPLES / "critic_typed_agent.json").read_text())
    assert typed["name"] == "critic" and typed["version"] == 2       # v1 stays registered
    assert typed["config"]["output_schema"]["type"] == "object"
    from agentos.providerkit.schema import OutputSchema
    OutputSchema.parse(typed["config"]["output_schema"])           # a usable contract
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




def test_same_agents_run_on_the_pydantic_ai_inner_harness(monkeypatch):
    """The quickstart §6b claim for the second inner harness: the exact example agents,
    with only `executor` changed, run through the real API and engine. Inner harnesses use
    no cassettes, so the model layer is PydanticAI's own `FunctionModel` — a poem for the
    poet, JSON (in fences, as a small model would) for the critic — swapped in at the one
    seam the executor exposes for it."""
    pytest.importorskip("agentos_provider_pydantic_ai")
    from agentos_provider_pydantic_ai import executor as pai
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def scripted(messages, info):
        user = str(messages[-1].parts[-1].content)
        if "Rate this haiku" in user:
            return ModelResponse(parts=[TextPart('```json\n{"score": 4, "reason": "terse"}\n```')])
        return ModelResponse(parts=[TextPart("logs of events, / past and present, / time flows.")])

    monkeypatch.setattr(pai.PydanticAIExecutor, "_default_model",
                        lambda self, model_id: FunctionModel(scripted, model_name=model_id))
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    for f in ("poet_agent.json", "critic_agent.json"):
        a = json.loads((EXAMPLES / f).read_text())
        a["executor"] = "pydantic-ai"
        assert c.post("/agents", json=a).status_code == 201
    _post_json(c, "/workflows", "haiku_workflow.json")
    run = c.post("/workflows/haiku/runs?sync=true", json={"inputs": {"topic": "event logs"}}).json()
    assert run["status"] == "completed", run["error"]
    write, review = run["steps"]
    assert write["provenance"]["executor"] == "pydantic-ai"
    assert write["provenance"]["model_id"] == "qwen2.5:0.5b" and "events" in write["output"]["text"]
    assert write["output"]["turns"] == 1 and write["output"]["tool_calls"] == []
    assert review["output"]["json"] == {"score": 4, "reason": "terse"}
    assert run["total_cost"] == "0"


def test_typed_critic_runs_on_the_inner_harness(monkeypatch):
    """docs/quickstart-llm.md §6b "Typed output": `examples/critic_typed_agent.json` (critic
    v2 with an `output_schema`) through the real API and engine on the pydantic-ai harness.
    The scripted model first returns a well-formed reply that VIOLATES the schema (score 9);
    the kit rejects it, the step fails with the path, the node's retry policy re-runs it,
    and the second reply satisfies the contract — `json` plus `schema_sha256` on the log."""
    pytest.importorskip("agentos_provider_pydantic_ai")
    from agentos_provider_pydantic_ai import executor as pai
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    critic_replies = iter(['{"score": 9, "reason": "off the scale"}',
                           '{"score": 4, "reason": "terse"}'])

    def scripted(messages, info):
        user = str(messages[-1].parts[-1].content)
        if "Rate this haiku" in user:
            return ModelResponse(parts=[TextPart(next(critic_replies))])
        return ModelResponse(parts=[TextPart("logs of events, / past and present, / time flows.")])

    monkeypatch.setattr(pai.PydanticAIExecutor, "_default_model",
                        lambda self, model_id: FunctionModel(scripted, model_name=model_id))
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    poet = json.loads((EXAMPLES / "poet_agent.json").read_text())
    poet["executor"] = "pydantic-ai"
    assert c.post("/agents", json=poet).status_code == 201
    _post_json(c, "/agents", "critic_typed_agent.json")            # critic v2, as documented
    wf = json.loads((EXAMPLES / "haiku_workflow.json").read_text())
    wf["nodes"][1]["agent_version"] = 2
    assert c.post("/workflows", json=wf).status_code == 201
    run = c.post("/workflows/haiku/runs?sync=true", json={"inputs": {"topic": "event logs"}}).json()
    assert run["status"] == "completed", run["error"]
    _write, review = run["steps"]
    assert review["output"]["json"] == {"score": 4, "reason": "terse"}
    assert len(review["output"]["schema_sha256"]) == 64
    assert review["attempt"] == 2                                   # the violation cost one attempt
    records = c.get(f"/runs/{run['id']}/events").json()["data"]
    failed = [r for r in records if r["event_type"] == "step.failed"]
    assert len(failed) == 1
    assert "violates output_schema at $.score" in json.dumps(failed[0])


def test_research_example_fetch_then_poet(monkeypatch):
    """docs/quickstart-llm.md §7: the tool step's response is the poet's input. GitHub is
    mocked; the poet replays the openai-compat cassette recorded for this exact prompt."""
    pytest.importorskip("agentos_provider_openai_compat")
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTES", "replay")
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTE_DIR", str(CASSETTES))
    monkeypatch.setenv("AGENTOS_OPENAI_CASSETTE", "research")
    import httpx

    from agentos.api import main
    importlib.reload(main)

    def github(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com" and request.method == "GET"
        assert request.headers["user-agent"] == "agentos-quickstart"
        return httpx.Response(200, json={"full_name": "AmitSinghOM/AgentOS",
                                         "description": "A control plane for durable, "
                                         "observable, human-in-the-loop LLM agent workflows"})
    main.executors["tool"]._transport = httpx.MockTransport(github)
    c = TestClient(main.app)
    for f in ("repo_facts_agent.json", "repo_poet_agent.json"):
        _post_json(c, "/agents", f)
    _post_json(c, "/workflows", "research_workflow.json")
    run = c.post("/workflows/research/runs?sync=true").json()
    assert run["status"] == "completed", run["error"]
    facts, write = run["steps"]
    assert facts["provenance"]["executor"] == "tool" and facts["output"]["status"] == 200
    assert [e["effect_class"] for e in facts["effects"]] == ["read"]
    assert write["output"]["text"].strip() and write["provenance"]["executor"] == "openai-compat"
