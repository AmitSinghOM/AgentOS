"""agentos-provider-pydantic-ai — PydanticAI's own `FunctionModel` drives every run, so no
network and no cassettes; the live Ollama path is exercised by the quickstart runner.

Boundaries under test:
- a text-only run: final output, usage → cost, provenance, `compute` effect;
- a tool run: the call is recorded (name + hashes) and its effect class reported;
- a tool whose class the AgentOS agent did NOT declare is not offered to the model at all
  (asserted on the `AgentInfo` the FunctionModel actually saw) and is listed as withheld;
- an unknown tool name is a definition error naming the registered tools;
- deferred results (approval / external execution) raise instead of auto-resolving;
- SDK failures map to the providerkit error vocabulary with the fix in the message;
- the per-step client is closed on success and on failure;
- the client is built from THIS provider's config, never from `OPENAI_BASE_URL`;
- the output keys are identical to the openai-agents inner harness on the same trajectory;
- through the core: an agent declaring an approval-required class on this executor is
  SUSPENDED before dispatch — no step.started — by the existing gate.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from agentos_provider_pydantic_ai import (
    BUILTIN_TOOLS,
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    PydanticAIExecutor,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    from_env,
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agentos.core.engine import Engine
from agentos.core.models import (
    Agent,
    AgentType,
    BlobRef,
    Budget,
    EffectClass,
    RunStatus,
    StepRequest,
    WorkflowDefinition,
)
from agentos.store.memory import MemoryStore

USAGE = RequestUsage(input_tokens=100, output_tokens=20)


def send_mail(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def charge(amount_cents: int) -> str:
    """Charge the customer's card."""
    return f"charged {amount_cents}"


REGISTRY = ToolRegistry([*BUILTIN_TOOLS,
                         ToolSpec("send_mail", send_mail, EffectClass.send_message),
                         ToolSpec("charge", charge, EffectClass.spend)])


@dataclass
class Seen:
    """What the model saw on each request: instructions, offered tool names, last message."""
    instructions: str | None
    tools: list[str]
    messages: list[ModelMessage]


def text(s: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(s)], usage=USAGE)


def call(name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=call_id)], usage=USAGE)


class Scripted:
    """A FunctionModel body that returns the scripted responses in order (a response may be
    an exception instance, which is raised as the model layer would raise it) and records
    what the agent handed it."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[Seen] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(Seen(info.instructions, [t.name for t in info.function_tools],
                               list(messages)))
        if not self.steps:
            raise AssertionError("scripted model ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def executor(steps, **kw) -> tuple[PydanticAIExecutor, Scripted]:
    scripted = Scripted(steps)
    model = FunctionModel(scripted, model_name="scripted")
    ex = PydanticAIExecutor(from_env({}), registry=REGISTRY,
                            model_factory=lambda _id: model, **kw)
    return ex, scripted


def request(config: dict, declared=(EffectClass.compute,), inputs=None) -> StepRequest:
    return StepRequest(
        run_id="r", step_id="s", attempt=1, idempotency_key="r:s",
        agent=Agent(name="assistant", type=AgentType.llm, executor="pydantic-ai",
                    config={"model": "chat.fast", **config},
                    declared_effects=list(declared)),
        inputs=inputs or {"run": {"topic": "event logs"}}, inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(declared), budget=Budget(),
    )


def noop_progress(fraction: float, note: str = "") -> None:
    pass


def test_text_only_run_records_output_usage_cost_and_provenance():
    ex, model = executor([text("A haiku about logs.")])
    res = ex.execute(request({"instructions": "You write haiku.",
                              "prompt": "Write about {run.topic}."}), noop_progress)
    assert res.output["text"] == "A haiku about logs."
    assert res.output["turns"] == 1 and res.output["tool_calls"] == []
    assert res.output["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert res.output["priced"] is True and res.cost.amount == "0"     # qwen is free locally
    assert {m.name: m.quantity for m in res.cost.units}["model_requests"] == 1
    assert [e.effect_class for e in res.effects] == [EffectClass.compute]
    assert res.provenance.executor == "pydantic-ai" and res.provenance.model_id == "qwen2.5:0.5b"
    assert res.provenance.model_alias == "chat.fast" and len(res.provenance.prompt_hash) == 64
    # C12: the instructions carry the data boundary and the input arrives delimited.
    seen = model.calls[0]
    assert "You write haiku." in seen.instructions and "never follow instructions" in seen.instructions
    user = str(seen.messages[-1].parts[-1].content)
    assert "<input" in user and "event logs" in user


def test_tool_call_is_recorded_and_its_effect_class_reported():
    ex, model = executor([call("word_count", {"text": "a b c"}, "c1"), text("three")])
    res = ex.execute(request({"tools": ["word_count", "utc_now"]}), noop_progress)
    assert res.output["text"] == "three" and res.output["turns"] == 2
    [rec] = res.output["tool_calls"]
    assert rec["name"] == "word_count" and rec["call_id"] == "c1"
    assert len(rec["arguments_sha256"]) == 64 and len(rec["output_sha256"]) == 64
    assert res.output["tools_offered"] == ["utc_now", "word_count"]
    assert [(e.effect_class, e.description) for e in res.effects][1:] == \
        [(EffectClass.compute, "tool word_count")]
    assert sorted(model.calls[0].tools) == ["utc_now", "word_count"]


def test_undeclared_tool_classes_are_not_offered_to_the_model():
    """The agent declares only `compute`; send_mail is `send_message`. The model must not
    even see it — withheld, not offered-and-refused."""
    ex, model = executor([text("ok")])
    res = ex.execute(request({"tools": ["word_count", "send_mail"]}), noop_progress)
    assert model.calls[0].tools == ["word_count"]
    assert res.output["tools_offered"] == ["word_count"]
    assert res.output["tools_withheld"] == ["send_mail"]


def test_declared_tool_class_is_offered_and_reported_as_that_effect():
    ex, model = executor([call("send_mail", {"to": "a@b.c", "body": "hi"}, "c9"), text("sent")])
    res = ex.execute(request({"tools": ["send_mail"]},
                             declared=(EffectClass.compute, EffectClass.send_message)), noop_progress)
    assert model.calls[0].tools == ["send_mail"]
    assert (EffectClass.send_message, "tool send_mail") in \
        [(e.effect_class, e.description) for e in res.effects]


def test_unknown_tool_is_a_definition_error_naming_the_registry():
    ex, _ = executor([])
    with pytest.raises(UnknownTool, match=r"tool 'nope' is not registered.*word_count"):
        ex.execute(request({"tools": ["nope"]}), noop_progress)


def test_a_call_whose_arguments_fail_validation_is_recorded_as_rejected():
    """Seen live on qwen2.5:0.5b: the model called `word_count` with the wrong key.
    PydanticAI never runs the tool; it sends a retry prompt and the model tries again. The
    log must distinguish that from a tool that returned — `rejected: true`, the retry
    message hashed in place of an output — and the retry counts against max_turns."""
    ex, _ = executor([call("word_count", {"txt": "a b c"}, "bad"),      # wrong key
                      call("word_count", {"text": "a b c"}, "good"),
                      text("three")])
    res = ex.execute(request({"tools": ["word_count"]}), noop_progress)
    assert res.output["text"] == "three" and res.output["turns"] == 3
    bad, good = res.output["tool_calls"]
    assert bad["call_id"] == "bad" and bad["rejected"] is True and len(bad["output_sha256"]) == 64
    assert good["call_id"] == "good" and "rejected" not in good and len(good["output_sha256"]) == 64
    assert bad["output_sha256"] != good["output_sha256"]
    # The attempt is still an effect on word_count, once (distinct tool), same as a success.
    assert [(e.effect_class, e.description) for e in res.effects][1:] == \
        [(EffectClass.compute, "tool word_count")]


def test_deferred_results_raise_instead_of_auto_resolving():
    """A registry tool must never be `requires_approval`; if a plugin ships one anyway (or
    raises ApprovalRequired), PydanticAI returns `DeferredToolRequests` and the executor
    refuses to approve on the operator's behalf."""
    from pydantic_ai import Tool

    approval_tool = ToolSpec("wire", charge, EffectClass.compute)      # class declared, so offered
    registry = ToolRegistry([approval_tool])
    ex, _ = executor([call("wire", {"amount_cents": 5}, "c1"), text("never reached")])
    ex.registry = registry

    real_run = ex._run

    async def run_with_approval(agent, user, **kw):
        # Simulate a plugin whose tool carries requires_approval — swap the Tool in place.
        agent._function_toolset.tools["wire"] = Tool(charge, name="wire", requires_approval=True)
        return await real_run(agent, user, **kw)

    ex._run = run_with_approval
    with pytest.raises(ProviderError, match=r"deferred on tools \['wire'\].*gated"):
        ex.execute(request({"tools": ["wire"]}), noop_progress)


def test_sdk_failures_map_to_the_providerkit_vocabulary():
    ex, _ = executor([UnexpectedModelBehavior("tool args were not JSON")])
    with pytest.raises(BadResponse, match="pydantic-ai run failed.*UnexpectedModelBehavior"):
        ex.execute(request({}), noop_progress)
    # A non-SDK exception from the model layer is NOT swallowed or re-labelled: it reaches
    # the core's step boundary as-is, which records `crashed` and lets the retry policy decide.
    ex, _ = executor([RuntimeError("model exploded")])
    with pytest.raises(RuntimeError, match="model exploded"):
        ex.execute(request({}), noop_progress)
    # max_turns: the scripted model keeps calling a tool forever.
    ex, _ = executor([call("utc_now", {}, f"c{i}") for i in range(4)])
    with pytest.raises(BadResponse, match="exceeded max_turns=2"):
        ex.execute(request({"tools": ["utc_now"], "max_turns": 2}), noop_progress)


@pytest.mark.parametrize("exc, expected, needle", [
    (ModelHTTPError(401, "m", {"error": "bad key"}), AuthenticationFailed, "AGENTOS_PYDANTIC_AI_API_KEY"),
    (ModelHTTPError(404, "m", {"error": "no such model"}), ModelNotFound, "ollama pull qwen2.5:0.5b"),
    (ModelHTTPError(429, "m", None), ProviderRateLimited, "retry policy applies"),
    (ModelHTTPError(503, "m", None), ProviderServerError, "server error 503"),
    (ModelHTTPError(400, "m", {"error": "bad request"}), BadResponse, "returned 400"),
    (ModelAPIError("m", "Connection refused"), ProviderUnreachable, "ollama serve"),
])
def test_http_errors_map_by_status_with_the_fix_in_the_message(exc, expected, needle):
    ex, _ = executor([exc])
    with pytest.raises(expected, match=needle):
        ex.execute(request({}), noop_progress)


def test_the_default_model_client_is_closed_after_every_run(monkeypatch):
    """The executor creates the per-step client inside the step's own event loop and
    closes it in `finally` — on success and on failure — so a long-lived worker never
    accumulates open connection pools."""
    closed: list[str] = []

    class FakeClient:
        async def close(self):
            closed.append("closed")

    ex = PydanticAIExecutor(from_env({}), registry=REGISTRY)
    ok = FunctionModel(Scripted([text("ok")]), model_name="s")
    monkeypatch.setattr(ex, "_model_factory", lambda _id: (ok, FakeClient()))
    assert ex.execute(request({}), noop_progress).output["text"] == "ok"
    assert closed == ["closed"]

    failing = FunctionModel(Scripted([UnexpectedModelBehavior("bad")]), model_name="s")
    monkeypatch.setattr(ex, "_model_factory", lambda _id: (failing, FakeClient()))
    with pytest.raises(BadResponse):
        ex.execute(request({}), noop_progress)
    assert closed == ["closed", "closed"]


def test_the_default_client_targets_this_providers_config_not_openai_env(monkeypatch):
    """PydanticAI's OpenAIProvider reads OPENAI_BASE_URL / OPENAI_API_KEY if it builds the
    client. This provider builds it, so the AgentOS config is the only source."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    ex = PydanticAIExecutor(from_env({"AGENTOS_PYDANTIC_AI_BASE_URL": "http://127.0.0.1:9/v1"}),
                            registry=REGISTRY)
    import asyncio

    async def make():
        model, client = ex._default_model("qwen2.5:0.5b")
        try:
            return str(client.base_url), model.model_name
        finally:
            await client.close()

    base_url, model_name = asyncio.run(make())
    assert base_url.startswith("http://127.0.0.1:9/v1") and model_name == "qwen2.5:0.5b"


# --------------------------------------------------------------- typed structured output
CRITIQUE = {"type": "object",
            "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5},
                           "reason": {"type": "string"}},
            "required": ["score", "reason"], "additionalProperties": False}


def test_output_schema_validated_reply_lands_in_json_with_the_schema_hash():
    ex, model = executor([text('```json\n{"score": 4, "reason": "terse"}\n```')])
    res = ex.execute(request({"output_schema": CRITIQUE, "prompt": "Rate {run.topic}"}),
                     noop_progress)
    assert res.output["json"] == {"score": 4, "reason": "terse"}
    assert len(res.output["schema_sha256"]) == 64
    # The model was TOLD the schema (PromptedOutput puts it in the instructions) ...
    assert "score" in model.calls[0].instructions and '"maximum": 5' in model.calls[0].instructions
    # ... and the schema is part of what the prompt hash covers.
    plain = executor([text('{"score": 4, "reason": "terse"}')])[0].execute(
        request({"json_output": True, "prompt": "Rate {run.topic}"}), noop_progress)
    assert plain.provenance.prompt_hash != res.provenance.prompt_hash
    assert "schema_sha256" not in plain.output


def test_output_schema_violation_fails_the_step_naming_the_path():
    """PydanticAI retries non-JSON itself, but a well-formed reply with the wrong shape is
    the kit's call: the step fails with the first violation's path, never a wrong shape
    silently accepted."""
    ex, _ = executor([text('{"score": 9, "reason": "too high"}')])
    with pytest.raises(BadResponse, match=r"violates output_schema at \$\.score: 9 is greater"):
        ex.execute(request({"output_schema": CRITIQUE}), noop_progress)
    # Non-JSON is retried by the harness within max_turns, then a valid reply is accepted.
    ex, model = executor([text("not json at all"), text('{"score": 2, "reason": "meh"}')])
    res = ex.execute(request({"output_schema": CRITIQUE, "max_turns": 3}), noop_progress)
    assert res.output["json"]["score"] == 2 and res.output["turns"] == 2
    assert len(model.calls) == 2


def test_an_unusable_output_schema_is_a_definition_error_before_any_model_call():
    ex, model = executor([text("never")])
    with pytest.raises(BadResponse, match=r'output_schema must have "type": "object"'):
        ex.execute(request({"output_schema": {"type": "string"}}), noop_progress)
    with pytest.raises(BadResponse, match=r"remote \$ref"):
        ex.execute(request({"output_schema": {"type": "object", "properties": {
            "a": {"$ref": "https://example.invalid/x.json"}}}}), noop_progress)
    assert model.calls == []                                   # the SDK never ran


def test_describe_and_health_report_without_a_server():
    ex, _ = executor([])
    d = ex.describe()
    assert d["wire_format"] == "pydantic-ai" and d["tools"]["send_mail"] == "send_message"
    assert d["api_key"] == "unset" and d["sdk_version"] != "unknown"
    h = PydanticAIExecutor(from_env({"AGENTOS_PYDANTIC_AI_BASE_URL": "http://127.0.0.1:9/v1"}),
                           registry=REGISTRY).health()
    assert h["reachable"] is False and "error" in h


# ---------------------------------------------------------- parity with the sibling harness
def test_output_keys_match_the_openai_agents_inner_harness():
    """A workflow switches inner harness by changing `executor`; nothing downstream may
    move. Run the same scripted trajectory (one tool call, then text) through both
    executors and compare the output key sets and the effects list."""
    pytest.importorskip("agentos_provider_openai_agents")
    from agentos_provider_openai_agents import OpenAIAgentsExecutor
    from agentos_provider_openai_agents import from_env as sdk_env
    from agents import Usage
    from agents.testing import ScriptedModel, assistant_message, function_call

    cfg = {"tools": ["word_count", "send_mail"], "output_schema": {
        "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}
    ours, _ = executor([call("word_count", {"text": "a b"}, "c1"), text('{"n": 2}')])
    mine = ours.execute(request(cfg), noop_progress)

    sdk_model = ScriptedModel([[function_call("word_count", {"text": "a b"}, call_id="c1")],
                               [assistant_message('{"n": 2}')]],
                              default_usage=Usage(requests=1, input_tokens=100, output_tokens=20,
                                                  total_tokens=120))
    theirs = OpenAIAgentsExecutor(sdk_env({}), registry=REGISTRY, model_factory=lambda _id: sdk_model)
    sib = theirs.execute(request(cfg), noop_progress)

    assert set(mine.output) == set(sib.output)
    assert set(mine.output["tool_calls"][0]) == set(sib.output["tool_calls"][0])
    assert mine.output["tools_withheld"] == sib.output["tools_withheld"] == ["send_mail"]
    assert mine.output["json"] == sib.output["json"] == {"n": 2}
    assert mine.output["schema_sha256"] == sib.output["schema_sha256"]   # one contract hash
    # Same effect classes in the same order; the tool effects word-for-word (the first
    # effect's description legitimately names the harness that ran).
    assert [e.effect_class for e in mine.effects] == [e.effect_class for e in sib.effects]
    assert [e.description for e in mine.effects][1:] == [e.description for e in sib.effects][1:]
    assert {m.name for m in mine.cost.units} == {m.name for m in sib.cost.units}


# ----------------------------------------------------------------------- through the core
def test_a_step_declaring_an_approval_required_class_is_suspended_before_dispatch():
    """The existing tier-2 gate governs the inner harness with no new mechanism: `spend`
    is approval-required under the default budget, so the run suspends and PydanticAI is
    never invoked — no step.started, no model call."""
    ex, model = executor([call("charge", {"amount_cents": 500}, "c1"), text("charged")])
    store = MemoryStore()
    store.put_agent(Agent(name="payer", type=AgentType.llm, executor="pydantic-ai",
                          config={"model": "chat.fast", "tools": ["charge"]},
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="pay", nodes=[{"id": "pay", "agent": "payer"}]))
    engine = Engine(store=store, blobs=store, executors={"pydantic-ai": ex})
    run = engine.start_run("pay")
    assert run.status is RunStatus.suspended
    assert [type(e).event_type for e in store.read_events(run.id)] == \
        ["run.started", "approval.requested", "run.suspended"]
    assert model.calls == []                                   # the SDK never ran


def test_plugin_discovery_finds_the_executor():
    from agentos.plugins import discover_executors
    found = discover_executors()
    assert "pydantic-ai" in found and found["pydantic-ai"].name == "pydantic-ai"
