"""Opt-in LIVE function-calling round trip against a real model server (ROADMAP A8; closes
the second coverage gap left by #148: every other tool test replays a recording).

Skipped unless `AGENTOS_LIVE_TOOLS=1`. Then it drives the real executor — no cassette, no
mock transport — against `AGENTOS_OPENAI_BASE_URL` (default: local Ollama) with the
quickstart's `counter` agent exactly as docs/quickstart-llm.md prints it, on the phrase the
committed live cassette was recorded with, and asserts the model made ONE clean
`word_count` call and answered with the right number. The nightly `provider-live` job runs
it on the runner's Ollama after the model pull (`.github/workflows/provider-live.yml`),
with `AGENTOS_OPENAI_ALIASES` pointing the agent's `chat.default` at the job's tool model.

    AGENTOS_LIVE_TOOLS=1 pytest -q providers/openai-compat/tests -k live_tool
    AGENTOS_LIVE_TOOLS=1 AGENTOS_OPENAI_ALIASES='{"chat.default": "qwen2.5:1.5b"}' pytest ...

A failure here is a real-model regression in the tool path (or a model that cannot follow
a tool schema); the replay suites cannot see either.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("agentos_provider_openai_compat")

from agentos_provider_openai_compat import OpenAICompatExecutor, from_env

from dagentos.core.models import Agent, AgentType, BlobRef, Budget, EffectClass, StepRequest

RECORD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_cassettes.py"

pytestmark = pytest.mark.skipif(os.environ.get("AGENTOS_LIVE_TOOLS") != "1",
                                reason="set AGENTOS_LIVE_TOOLS=1 with a model server up")


def _record_script():
    """The record script owns the agent and phrase, so the live run and the live cassette
    are the same scenario. Loaded under its own name: `record_cassettes` is too generic."""
    spec = importlib.util.spec_from_file_location("openai_compat_record_cassettes", RECORD_SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def test_live_tool_round_trip_against_the_configured_server():
    rec = _record_script()
    agent = rec.quickstart_tool_agent()
    declared = frozenset(EffectClass(e) for e in agent["declared_effects"])
    topic = rec.TOOL_LIVE_INPUTS["run"]["topic"]
    count = str(len(topic.split()))
    req = StepRequest(
        run_id="live", step_id="count", attempt=1, idempotency_key="live:count",
        agent=Agent(name=agent["name"], type=AgentType.llm, executor="openai-compat",
                    config=agent["config"], declared_effects=sorted(declared)),
        inputs=rec.TOOL_LIVE_INPUTS, inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=declared, budget=Budget(),
    )
    ex = OpenAICompatExecutor(from_env())              # the process env: base URL, aliases
    health = ex.health()
    assert health.get("reachable"), health
    notes: list[str] = []
    res = ex.execute(req, lambda f, n: notes.append(n))

    calls = res.output["tool_calls"]
    assert [c["name"] for c in calls] == ["word_count"], (calls, res.output["text"])
    assert "rejected" not in calls[0], (calls, res.output["text"])
    assert calls[0]["output_sha256"] == hashlib.sha256(count.encode()).hexdigest()
    assert res.output["turns"] == 2 and res.output["finish_reason"] == "stop"
    assert count in res.output["text"], res.output["text"]
    assert res.output["model"].startswith(ex.resolve(req))   # the alias reached that model
    assert notes[-1].startswith("1 tool call(s), ")
