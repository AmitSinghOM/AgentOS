"""Function-calling round trip on the plain-HTTP provider (audit E-17, ROADMAP A8 half).

The model is offered the registry tools whose effect class the step declared (the same
`ToolRegistry.select` rule the two SDK harnesses apply); a call outside that set is
rejected, never run, and the refusal goes back to the model as the tool result; executed
results go back as delimited data; the loop is bounded by `max_tool_rounds`; usage is
summed across rounds; and each call is recorded in the shape the SDK harnesses record
(`name, call_id, arguments_sha256, output_sha256, rejected?`). Every test drives the
in-process reference server's scripted trajectory (`openai_reference_server._tool_turn`)
or the committed cassette recorded from it — never the network.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("agentos_provider_openai_compat")

from agentos_provider_openai_compat import OpenAICompatExecutor, ToolError, from_env
from agentos_provider_openai_compat.executor import TOOL_NOTE

from dagentos.core.models import Agent, AgentType, BlobRef, Budget, EffectClass, StepRequest
from dagentos.providerkit.prompt import DATA_BOUNDARY, wrap_input
from dagentos.providerkit.tools import BUILTIN_TOOLS, ToolRegistry, ToolSpec, word_count

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import openai_reference_server as ref

CASSETTES = HERE / "cassettes"
TOOL_CASSETTE = "tool_round_trip"
RECORD_SCRIPT = HERE.parent / "scripts" / "record_cassettes.py"
QUICKSTART_LLM = HERE.parents[2] / "docs" / "quickstart-llm.md"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def spy(fn):
    """`fn` as a registry tool that remembers every call, so a test can assert it was NOT
    run. `functools.wraps` keeps the signature, annotations and docstring the schema is
    derived from."""
    calls: list[dict] = []

    @functools.wraps(fn)
    def wrapper(**kw):
        calls.append(kw)
        return fn(**kw)

    wrapper.calls = calls
    return wrapper


def send_mail(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def boom(text: str) -> str:
    """A tool with a bug."""
    raise RuntimeError("disk full")


def registry(*extra: ToolSpec) -> ToolRegistry:
    return ToolRegistry([*BUILTIN_TOOLS, *extra])


def executor(script, *, reg: ToolRegistry | None = None, seen: list | None = None,
             env: dict | None = None) -> OpenAICompatExecutor:
    cfg = from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1", **(env or {})})
    return OpenAICompatExecutor(cfg, registry=reg if reg is not None else registry(),
                                transport=ref.transport(tool_script=script, seen=seen))


def request(config: dict, declared=(EffectClass.compute,), inputs=None) -> StepRequest:
    return StepRequest(
        run_id="r", step_id="s", attempt=1, idempotency_key="r:s",
        agent=Agent(name="counter", type=AgentType.llm, executor="openai-compat",
                    config={"model": "chat.fast", **config}, declared_effects=list(declared)),
        inputs=inputs or {"run": {"topic": "event logs"}},
        inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(declared), budget=Budget(),
    )


def noop(fraction: float, note: str = "") -> None:
    pass


def tool_messages(body: dict) -> list[dict]:
    return [m for m in body["messages"] if m["role"] == "tool"]


# ------------------------------------------------------------------ the round trip

def test_one_tool_call_is_executed_recorded_and_its_result_sent_back_as_data():
    tool = spy(word_count)
    seen: list[dict] = []
    ex = executor([[("word_count", {"text": "event logs"})]],
                  reg=ToolRegistry([ToolSpec("word_count", tool, EffectClass.compute)]), seen=seen)
    res = ex.execute(request({"prompt": "Count {run.topic}", "tools": ["word_count"]}), noop)

    assert tool.calls == [{"text": "event logs"}]
    [rec] = res.output["tool_calls"]
    assert rec == {"name": "word_count", "call_id": "call_0_0",
                   "arguments_sha256": _sha(json.dumps({"text": "event logs"})),
                   "output_sha256": _sha("2")}                     # rejected absent
    assert res.output["tools_offered"] == ["word_count"] and res.output["tools_withheld"] == []
    assert res.output["turns"] == 2 and res.output["finish_reason"] == "stop"
    assert res.output["text"].endswith('<input name="tool.word_count">2</input>')
    assert [(e.effect_class, e.description) for e in res.effects][1:] == \
        [(EffectClass.compute, "tool word_count")]

    first, second = seen
    [offered] = first["tools"]
    assert offered["type"] == "function" and offered["function"]["name"] == "word_count"
    assert offered["function"]["description"].startswith("Number of whitespace-separated words")
    params = offered["function"]["parameters"]
    assert params["type"] == "object" and params["required"] == ["text"]
    assert params["properties"]["text"]["type"] == "string"
    assert "tool_choice" not in first                              # only when configured
    # Round 2 carries the assistant's call verbatim and the result, delimited as data (C12).
    assistant = second["messages"][-2]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["id"] == "call_0_0"
    [result] = tool_messages(second)
    assert result == {"role": "tool", "tool_call_id": "call_0_0",
                      "content": wrap_input("tool.word_count", "2")}
    system = second["messages"][0]["content"]
    assert DATA_BOUNDARY in system and system.endswith(TOOL_NOTE)   # measured live, see executor


def test_a_call_outside_the_declared_effect_classes_is_rejected_and_never_run():
    """send_mail is `send_message`; the step declares only `compute`. The model is not
    offered send_mail (withheld, as in the SDK harnesses) — and when it calls it anyway the
    call is recorded `rejected: true`, the function never runs, and the model is told why so
    it can finish. No effect is reported for it: nothing happened."""
    tool = spy(send_mail)
    seen: list[dict] = []
    ex = executor([[("send_mail", {"to": "a@b.c", "body": "hi"})]], seen=seen,
                  reg=registry(ToolSpec("send_mail", tool, EffectClass.send_message)))
    res = ex.execute(request({"tools": ["word_count", "send_mail"]}), noop)

    assert tool.calls == []
    assert [t["function"]["name"] for t in seen[0]["tools"]] == ["word_count"]
    assert res.output["tools_withheld"] == ["send_mail"]
    [rec] = res.output["tool_calls"]
    assert rec["name"] == "send_mail" and rec["rejected"] is True
    [refusal] = tool_messages(seen[1])
    assert "was not run" in refusal["content"] and "send_message" in refusal["content"]
    assert rec["output_sha256"] == _sha(refusal["content"])
    assert "was not run" in res.output["text"]                     # the model finished
    assert [e.effect_class for e in res.effects] == [EffectClass.compute]


def test_a_call_to_a_tool_nobody_registered_is_rejected_naming_what_is_offered():
    seen: list[dict] = []
    ex = executor([[("rapidapi_lookup", {"q": "x"})]], seen=seen)
    res = ex.execute(request({"tools": ["word_count"]}), noop)
    [rec] = res.output["tool_calls"]
    assert rec["rejected"] is True and rec["name"] == "rapidapi_lookup"
    [refusal] = tool_messages(seen[1])
    assert "not offered" in refusal["content"] and "word_count" in refusal["content"]
    assert [e.effect_class for e in res.effects] == [EffectClass.compute]


def test_arguments_that_fail_validation_are_rejected_and_the_model_can_retry():
    """Seen live on qwen2.5:0.5b with the SDK harnesses: `word_count` called with the wrong
    key. The tool is not run; the validation error goes back; the retry succeeds."""
    tool = spy(word_count)
    ex = executor([[("word_count", {"txt": "a b c"})], [("word_count", {"text": "a b c"})]],
                  reg=ToolRegistry([ToolSpec("word_count", tool, EffectClass.compute)]))
    res = ex.execute(request({"tools": ["word_count"]}), noop)
    assert tool.calls == [{"text": "a b c"}]
    bad, good = res.output["tool_calls"]
    assert bad["rejected"] is True and len(bad["output_sha256"]) == 64
    assert "rejected" not in good and good["output_sha256"] == _sha("3")
    assert res.output["turns"] == 3
    # Attempted on an offered tool: one effect for the distinct tool, as pydantic-ai records.
    assert [(e.effect_class, e.description) for e in res.effects][1:] == \
        [(EffectClass.compute, "tool word_count")]


def test_max_tool_rounds_exhausted_fails_the_step_with_a_tool_error_naming_the_cap():
    tool = spy(word_count)
    seen: list[dict] = []
    forever = [[("word_count", {"text": f"round {i}"})] for i in range(10)]
    ex = executor(forever, seen=seen,
                  reg=ToolRegistry([ToolSpec("word_count", tool, EffectClass.compute)]))
    with pytest.raises(ToolError, match=r"max_tool_rounds=2"):
        ex.execute(request({"tools": ["word_count"], "max_tool_rounds": 2}), noop)
    assert len(tool.calls) == 2 and len(seen) == 3        # round 3's calls were never run
    seen.clear()
    with pytest.raises(ToolError, match=r"max_tool_rounds=4"):        # the default
        executor(forever, seen=seen).execute(request({"tools": ["word_count"]}), noop)
    assert len(seen) == 5


def test_a_tool_that_raises_fails_the_step_naming_the_tool():
    ex = executor([[("boom", {"text": "x"})]], reg=registry(ToolSpec("boom", boom,
                                                                    EffectClass.compute)))
    with pytest.raises(ToolError, match=r"tool 'boom' raised RuntimeError: disk full"):
        ex.execute(request({"tools": ["boom"]}), noop)


def test_usage_is_summed_across_rounds_and_priced_once():
    ex = executor([[("word_count", {"text": "a b"})]],
                  env={"AGENTOS_OPENAI_ALIASES": '{"chat.fast": "gpt-4o-mini"}'})
    res = ex.execute(request({"tools": ["word_count"]}), noop)
    # The reference server answers (12, 9) then (22, 19): a sum, not either round alone.
    assert res.output["usage"] == {"input_tokens": 34, "output_tokens": 28}
    meters = {m.name: m.quantity for m in res.cost.units}
    assert meters["input_tokens"] == 34 and meters["output_tokens"] == 28
    assert meters["model_requests"] == 2                 # same extra meter as the SDK harnesses
    assert res.output["priced"] is True
    assert res.cost.amount == ex.pricing.cost("gpt-4o-mini", 34, 28)[0].amount


def test_tool_choice_is_sent_only_alongside_offered_tools():
    seen: list[dict] = []
    executor([], seen=seen).execute(
        request({"tools": ["word_count"], "tool_choice": "required"}), noop)
    assert seen[0]["tool_choice"] == "required"
    seen.clear()
    spec = ToolSpec("send_mail", send_mail, EffectClass.send_message)
    executor([], seen=seen, reg=registry(spec)).execute(
        request({"tools": ["send_mail"], "tool_choice": "required"}), noop)
    assert "tools" not in seen[0] and "tool_choice" not in seen[0]   # all withheld


def test_an_agent_without_tools_sends_and_records_exactly_what_it_did_before():
    """No `config.tools`: no `tools` in the request (so every committed cassette still
    matches) and no new keys in the output."""
    seen: list[dict] = []
    cfg = from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1"})
    ex = OpenAICompatExecutor(cfg, registry=registry(), transport=ref.transport(seen=seen))
    res = ex.execute(request({"prompt": "hi {run.topic}"}), noop)
    assert set(seen[0]) == {"model", "messages", "temperature", "seed"}
    assert seen[0]["messages"][0]["content"].endswith(DATA_BOUNDARY)      # no tool note
    assert set(res.output) == {"text", "model", "finish_reason", "usage", "priced", "alias"}
    assert {m.name for m in res.cost.units} == {"input_tokens", "output_tokens", "requests"}


def test_describe_lists_the_registry_like_the_inner_harnesses():
    d = executor([], reg=registry(ToolSpec("send_mail", send_mail,
                                           EffectClass.send_message))).describe()
    assert d["tools"] == {"send_mail": "send_message", "utc_now": "compute",
                          "word_count": "compute"}


# ------------------------------------------------------------------ cassette (record/replay)

def _replay_executor(transport: httpx.BaseTransport | None = None) -> OpenAICompatExecutor:
    cfg = from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1",
                    "AGENTOS_OPENAI_CASSETTES": "replay",
                    "AGENTOS_OPENAI_CASSETTE_DIR": str(CASSETTES)})
    return OpenAICompatExecutor(cfg, registry=registry(), transport=transport,
                                cassette_name=TOOL_CASSETTE)


def test_tool_round_trip_replays_from_the_committed_cassette_without_the_network():
    class Boom(httpx.BaseTransport):
        def handle_request(self, request):
            raise AssertionError("network used during replay")

    res = _replay_executor(Boom()).execute(
        request(ref.TOOL_ROUND_TRIP_CONFIG, inputs=ref.TOOL_ROUND_TRIP_INPUTS), noop)
    [rec] = res.output["tool_calls"]
    assert rec["name"] == "word_count" and rec["output_sha256"] == _sha("2")
    assert res.output["turns"] == 2


def test_the_committed_tool_cassette_is_what_the_record_script_produces(tmp_path):
    """The fixture is reproducible: `scripts/record_cassettes.py --tools` against the
    reference server writes the same interactions (keys and bodies) as the committed file."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("openai_compat_record_cassettes", RECORD_SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.record_tools(tmp_path) == 0
    fresh = json.loads((tmp_path / f"{TOOL_CASSETTE}.json").read_text())
    committed = json.loads((CASSETTES / f"{TOOL_CASSETTE}.json").read_text())

    def exchanges(c):   # headers carry the package version; the exchange itself must match
        return [(i["key"], i["request"]["body"], i["response"]) for i in c["interactions"]]

    assert len(committed["interactions"]) == 2 and exchanges(fresh) == exchanges(committed)
    assert committed["source"] == "http://reference-server/v1"     # labelled as the fake


# ------------------------------------------------------------------ parity and docs

def test_tool_call_records_match_the_pydantic_ai_harness_on_the_same_trajectory():
    """A workflow moves between this provider and an inner harness by changing `executor`;
    the tool-call record, the withheld list, the effects and the meters must not move."""
    pytest.importorskip("agentos_provider_pydantic_ai")
    from agentos_provider_pydantic_ai import PydanticAIExecutor
    from agentos_provider_pydantic_ai import from_env as pai_env
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    spec = ToolSpec("send_mail", send_mail, EffectClass.send_message)
    cfg = {"tools": ["word_count", "send_mail"]}
    mine = executor([[("word_count", {"text": "a b"})]], reg=registry(spec)).execute(
        request(cfg), noop)

    steps = [ModelResponse(parts=[ToolCallPart("word_count", {"text": "a b"},
                                               tool_call_id="c1")],
                           usage=RequestUsage(input_tokens=12, output_tokens=9)),
             ModelResponse(parts=[TextPart("2")],
                           usage=RequestUsage(input_tokens=22, output_tokens=19))]
    model = FunctionModel(lambda _m, _i: steps.pop(0), model_name="scripted")
    sib = PydanticAIExecutor(pai_env({}), registry=registry(spec),
                             model_factory=lambda _id: model).execute(request(cfg), noop)

    assert set(mine.output) - {"finish_reason"} == set(sib.output)
    assert set(mine.output["tool_calls"][0]) == set(sib.output["tool_calls"][0])
    assert mine.output["tool_calls"][0]["output_sha256"] == sib.output["tool_calls"][0][
        "output_sha256"]                                    # same tool, same result, same hash
    assert mine.output["tools_withheld"] == sib.output["tools_withheld"] == ["send_mail"]
    assert [e.effect_class for e in mine.effects] == [e.effect_class for e in sib.effects]
    assert [e.description for e in mine.effects][1:] == [e.description for e in sib.effects][1:]
    assert {m.name for m in mine.cost.units} == {m.name for m in sib.cost.units}
    assert mine.output["usage"] == sib.output["usage"]


def test_the_quickstart_tool_agent_runs_as_written():
    """docs/quickstart-llm.md "Letting the model call a tool": the agent JSON on the page,
    unedited, drives a round trip and records the call."""
    page = QUICKSTART_LLM.read_text()
    section = page.split("### Letting the model call a tool", 1)[1]
    agent = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))
    assert agent["executor"] == "openai-compat" and agent["config"]["tools"] == ["word_count"]
    declared = tuple(EffectClass(e) for e in agent["declared_effects"])
    ex = executor([[("word_count", {"text": "event logs"})]])
    res = ex.execute(request(agent["config"], declared=declared,
                             inputs={"run": {"topic": "event logs"}}), noop)
    assert [c["name"] for c in res.output["tool_calls"]] == ["word_count"]
    assert "rejected" not in res.output["tool_calls"][0]
