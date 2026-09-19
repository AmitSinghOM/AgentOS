"""agentos-provider-openai-agents — the SDK's own `ScriptedModel` drives every run, so no
network and no cassettes; the live Ollama path is exercised by the quickstart runner.

Boundaries under test:
- a text-only run: final output, usage → cost, provenance, `compute` effect;
- a tool run: the call is recorded (name + hashes) and its effect class reported;
- a tool whose class the AgentOS agent did NOT declare is not offered to the model at all
  (asserted on what the ScriptedModel actually saw) and is listed as withheld;
- an unknown tool name is a definition error naming the registered tools;
- SDK interruptions (needs_approval) raise instead of auto-approving;
- SDK failures map to the providerkit error vocabulary with the fix in the message;
- through the core: an agent declaring an approval-required class on this executor is
  SUSPENDED before dispatch — no step.started — by the existing gate.
"""
from __future__ import annotations

import pytest
from agentos_provider_openai_agents import (
    BUILTIN_TOOLS,
    BadResponse,
    OpenAIAgentsExecutor,
    ProviderError,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    from_env,
)
from agents import ModelBehaviorError, Usage
from agents.testing import ScriptedModel, assistant_message, function_call

from dagentos.core.engine import Engine
from dagentos.core.models import (
    Agent,
    AgentType,
    BlobRef,
    Budget,
    EffectClass,
    RunStatus,
    StepRequest,
    WorkflowDefinition,
)
from dagentos.store.memory import MemoryStore

USAGE = Usage(requests=1, input_tokens=100, output_tokens=20, total_tokens=120)


def send_mail(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def charge(amount_cents: int) -> str:
    """Charge the customer's card."""
    return f"charged {amount_cents}"


REGISTRY = ToolRegistry([*BUILTIN_TOOLS,
                         ToolSpec("send_mail", send_mail, EffectClass.send_message),
                         ToolSpec("charge", charge, EffectClass.spend)])


def executor(steps, **kw) -> tuple[OpenAIAgentsExecutor, ScriptedModel]:
    model = ScriptedModel(steps, default_usage=USAGE)
    ex = OpenAIAgentsExecutor(from_env({}), registry=REGISTRY,
                              model_factory=lambda _id: model, **kw)
    return ex, model


def request(config: dict, declared=(EffectClass.compute,), inputs=None) -> StepRequest:
    return StepRequest(
        run_id="r", step_id="s", attempt=1, idempotency_key="r:s",
        agent=Agent(name="assistant", type=AgentType.llm, executor="openai-agents",
                    config={"model": "chat.fast", **config},
                    declared_effects=list(declared)),
        inputs=inputs or {"run": {"topic": "event logs"}}, inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(declared), budget=Budget(),
    )


def noop_progress(fraction: float, note: str = "") -> None:
    pass


def test_text_only_run_records_output_usage_cost_and_provenance():
    ex, model = executor([[assistant_message("A haiku about logs.")]])
    res = ex.execute(request({"instructions": "You write haiku.",
                              "prompt": "Write about {run.topic}."}), noop_progress)
    assert res.output["text"] == "A haiku about logs."
    assert res.output["turns"] == 1 and res.output["tool_calls"] == []
    assert res.output["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert res.output["priced"] is True and res.cost.amount == "0"     # qwen is free locally
    assert {m.name: m.quantity for m in res.cost.units}["model_requests"] == 1
    assert [e.effect_class for e in res.effects] == [EffectClass.compute]
    assert res.provenance.executor == "openai-agents" and res.provenance.model_id == "qwen2.5:0.5b"
    assert res.provenance.model_alias == "chat.fast" and len(res.provenance.prompt_hash) == 64
    # C12: the instructions carry the data boundary and the input arrives delimited.
    call = model.calls[0]
    assert "You write haiku." in call.system_instructions and "<input" in str(call.input)
    assert "event logs" in str(call.input)


def test_tool_call_is_recorded_and_its_effect_class_reported():
    ex, model = executor([
        [function_call("word_count", {"text": "a b c"}, call_id="c1")],
        [assistant_message("three")],
    ])
    res = ex.execute(request({"tools": ["word_count", "utc_now"]}), noop_progress)
    assert res.output["text"] == "three" and res.output["turns"] == 2
    [call] = res.output["tool_calls"]
    assert call["name"] == "word_count" and call["call_id"] == "c1"
    assert len(call["arguments_sha256"]) == 64 and len(call["output_sha256"]) == 64
    assert res.output["tools_offered"] == ["utc_now", "word_count"]
    assert [(e.effect_class, e.description) for e in res.effects][1:] == \
        [(EffectClass.compute, "tool word_count")]
    assert sorted(t.name for t in model.calls[0].tools) == ["utc_now", "word_count"]


def test_undeclared_tool_classes_are_not_offered_to_the_model():
    """The agent declares only `compute`; send_mail is `send_message`. The model must not
    even see it — withheld, not offered-and-refused."""
    ex, model = executor([[assistant_message("ok")]])
    res = ex.execute(request({"tools": ["word_count", "send_mail"]}), noop_progress)
    assert [t.name for t in model.calls[0].tools] == ["word_count"]
    assert res.output["tools_offered"] == ["word_count"]
    assert res.output["tools_withheld"] == ["send_mail"]


def test_declared_tool_class_is_offered_and_reported_as_that_effect():
    ex, model = executor([
        [function_call("send_mail", {"to": "a@b.c", "body": "hi"}, call_id="c9")],
        [assistant_message("sent")],
    ])
    res = ex.execute(request({"tools": ["send_mail"]},
                             declared=(EffectClass.compute, EffectClass.send_message)), noop_progress)
    assert [t.name for t in model.calls[0].tools] == ["send_mail"]
    assert (EffectClass.send_message, "tool send_mail") in \
        [(e.effect_class, e.description) for e in res.effects]


def test_unknown_tool_is_a_definition_error_naming_the_registry():
    ex, _ = executor([])
    with pytest.raises(UnknownTool, match=r"tool 'nope' is not registered.*word_count"):
        ex.execute(request({"tools": ["nope"]}), noop_progress)


def test_interruptions_raise_instead_of_auto_approving():
    class _Raw:
        name = "charge"

    class Paused:
        def __init__(self):
            self.interruptions = [type("I", (), {"raw_item": _Raw()})()]
            self.new_items = []
            self.final_output = None

    async def paused(*a, **k):
        return Paused()

    ex, _ = executor([], run=paused)
    with pytest.raises(ProviderError, match=r"paused for tool approval on \['charge'\].*gated"):
        ex.execute(request({}), noop_progress)


def test_sdk_failures_map_to_the_providerkit_vocabulary():
    ex, _ = executor([ModelBehaviorError("tool args were not JSON")])
    with pytest.raises(BadResponse, match="agents-sdk run failed.*ModelBehaviorError"):
        ex.execute(request({}), noop_progress)
    # A non-SDK exception from the model layer is NOT swallowed or re-labelled: it reaches
    # the core's step boundary as-is, which records `crashed` and lets the retry policy decide.
    ex, _ = executor([RuntimeError("model exploded")])
    with pytest.raises(RuntimeError, match="model exploded"):
        ex.execute(request({}), noop_progress)
    # max_turns: the scripted model keeps calling a tool forever.
    ex, _ = executor([[function_call("utc_now", {}, call_id=f"c{i}")] for i in range(4)])
    with pytest.raises(BadResponse, match="exceeded max_turns=2"):
        ex.execute(request({"tools": ["utc_now"], "max_turns": 2}), noop_progress)


def test_the_default_model_client_is_closed_after_every_run(monkeypatch):
    """Review finding: a per-step AsyncOpenAI that is never closed leaks connection pools
    in a long-lived worker. The executor now creates it inside the step's own event loop
    and closes it in `finally` — on success and on failure."""
    closed: list[str] = []

    class FakeClient:
        is_closed = False

        async def close(self):
            closed.append("closed")

    ex = OpenAIAgentsExecutor(from_env({}), registry=REGISTRY)
    scripted = ScriptedModel([[assistant_message("ok")]], default_usage=USAGE)
    monkeypatch.setattr(ex, "_model_factory", lambda _id: (scripted, FakeClient()))
    assert ex.execute(request({}), noop_progress).output["text"] == "ok"
    assert closed == ["closed"]

    failing = ScriptedModel([ModelBehaviorError("bad")], default_usage=USAGE)
    monkeypatch.setattr(ex, "_model_factory", lambda _id: (failing, FakeClient()))
    with pytest.raises(BadResponse):
        ex.execute(request({}), noop_progress)
    assert closed == ["closed", "closed"]


def test_describe_and_health_report_without_a_server():
    ex, _ = executor([])
    d = ex.describe()
    assert d["wire_format"] == "openai-agents-sdk" and d["tools"]["send_mail"] == "send_message"
    assert d["api_key"] == "unset"
    h = OpenAIAgentsExecutor(from_env({"AGENTOS_OPENAI_AGENTS_BASE_URL": "http://127.0.0.1:9/v1"}),
                             registry=REGISTRY).health()
    assert h["reachable"] is False and "error" in h


# --------------------------------------------------------------- typed structured output
CRITIQUE = {"type": "object",
            "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5},
                           "reason": {"type": "string"}},
            "required": ["score", "reason"], "additionalProperties": False}


def test_output_schema_is_sent_to_the_model_and_the_validated_reply_lands_in_json():
    ex, model = executor([[assistant_message('{"score": 4, "reason": "terse"}')]])
    res = ex.execute(request({"output_schema": CRITIQUE, "prompt": "Rate {run.topic}"}),
                     noop_progress)
    assert res.output["json"] == {"score": 4, "reason": "terse"}
    assert len(res.output["schema_sha256"]) == 64
    # The SDK handed the schema to the model as its output schema (what becomes response_format).
    sent = model.calls[0].output_schema
    assert sent is not None and sent.json_schema()["properties"]["score"]["maximum"] == 5
    assert sent.is_strict_json_schema() is False
    plain = executor([[assistant_message('{"score": 4, "reason": "terse"}')]])[0].execute(
        request({"json_output": True, "prompt": "Rate {run.topic}"}), noop_progress)
    assert plain.provenance.prompt_hash != res.provenance.prompt_hash


def test_output_schema_violation_fails_the_step_naming_the_path():
    """The SDK calls the kit validator on the reply; a violation is a ModelBehaviorError,
    which the executor already maps to BadResponse — same failure, same message, as on the
    PydanticAI harness."""
    ex, _ = executor([[assistant_message('{"score": 9, "reason": "too high"}')]])
    with pytest.raises(BadResponse, match=r"violates output_schema at \$\.score: 9 is greater"):
        ex.execute(request({"output_schema": CRITIQUE}), noop_progress)
    ex, _ = executor([[assistant_message("not json")]])
    with pytest.raises(BadResponse, match="reply is not JSON"):
        ex.execute(request({"output_schema": CRITIQUE}), noop_progress)


def test_an_unusable_output_schema_is_a_definition_error_before_any_model_call():
    ex, model = executor([[assistant_message("never")]])
    with pytest.raises(BadResponse, match=r'output_schema must have "type": "object"'):
        ex.execute(request({"output_schema": {"type": "string"}}), noop_progress)
    assert model.calls == ()


# ----------------------------------------------------------------------- through the core
def test_a_step_declaring_an_approval_required_class_is_suspended_before_dispatch():
    """The existing tier-2 gate governs the inner harness with no new mechanism: `spend`
    is approval-required under the default budget, so the run suspends and the SDK is
    never invoked — no step.started, no model call."""
    ex, model = executor([[function_call("charge", {"amount_cents": 500}, call_id="c1")],
                          [assistant_message("charged")]])
    store = MemoryStore()
    store.put_agent(Agent(name="payer", type=AgentType.llm, executor="openai-agents",
                          config={"model": "chat.fast", "tools": ["charge"]},
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="pay", nodes=[{"id": "pay", "agent": "payer"}]))
    engine = Engine(store=store, blobs=store, executors={"openai-agents": ex})
    run = engine.start_run("pay")
    assert run.status is RunStatus.suspended
    assert [type(e).event_type for e in store.read_events(run.id)] == \
        ["run.started", "approval.requested", "run.suspended"]
    assert model.calls == ()                                   # the SDK never ran


def test_plugin_discovery_finds_the_executor():
    from dagentos.plugins import discover_executors
    found = discover_executors()
    assert "openai-agents" in found and found["openai-agents"].name == "openai-agents"
