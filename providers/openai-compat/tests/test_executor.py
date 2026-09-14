"""Provider tests run against recorded cassettes, never the network (§11 A10). The
in-process reference server (`reference_server.py`) stands in for a live server where a
specific status code is needed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("agentos_provider_openai_compat")

from agentos_provider_openai_compat import (
    AuthenticationFailed,
    ConfigError,
    ModelNotFound,
    OpenAICompatExecutor,
    ProviderConfig,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    TemplateError,
)
from agentos_provider_openai_compat.cassette import CassetteMiss
from agentos_provider_openai_compat.pricing import PricingTable

from agentos.core.models import Agent, AgentType, BlobRef, Budget, StepRequest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import reference_server
from record_cassettes import QUICKSTART, SCENARIOS, quickstart_requests, request_for

CASSETTES = HERE / "cassettes"


def replay(name: str, **over) -> OpenAICompatExecutor:
    cfg = ProviderConfig.from_env({
        "AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1",
        "AGENTOS_OPENAI_CASSETTES": "replay",
        "AGENTOS_OPENAI_CASSETTE_DIR": str(CASSETTES), **over})
    return OpenAICompatExecutor(cfg, cassette_name=name)


def scenario(name: str) -> StepRequest:
    return request_for(name, *SCENARIOS[name])


def _progress(f, n):
    pass


# ------------------------------------------------------------------ happy paths (replay)

def test_haiku_from_cassette_with_alias_pricing_and_provenance():
    ex = replay(QUICKSTART)
    notes: list[str] = []
    res = ex.execute(quickstart_requests()[0], lambda f, n: notes.append(n))
    assert res.output["text"].strip()                                   # shape, not wording:
    assert res.output["alias"] == "chat.fast"                           # the same test must
    assert res.output["model"].startswith("qwen2.5:0.5b")               # pass on a live
    assert res.output["priced"] is True and res.cost.amount == "0"     # re-recording (free)
    meters = {m.name: m.quantity for m in res.cost.units}
    assert set(meters) == {"input_tokens", "output_tokens", "requests"} and meters["requests"] == 1
    assert meters["input_tokens"] > 0 and meters["output_tokens"] > 0
    assert res.cost.pricing_snapshot_hash == ex.pricing.sha256
    p = res.provenance
    assert (p.executor, p.model_alias) == ("openai-compat", "chat.fast")
    assert p.model_id == res.output["model"] and len(p.prompt_hash) == 64
    assert [e.effect_class.value for e in res.effects] == ["compute"]
    assert notes[0].startswith("POST ") and notes[0].endswith("model=qwen2.5:0.5b")
    assert notes[-1].endswith("tokens, 0 USD")


def test_json_output_is_parsed():
    ex = replay(QUICKSTART)
    poet = ex.execute(quickstart_requests()[0], _progress)
    res = ex.execute(quickstart_requests(poet.output["text"])[1], _progress)
    j = res.output["json"]
    assert isinstance(j, dict) and isinstance(j.get("score"), int | float)
    assert isinstance(j.get("reason"), str) and j["reason"].strip()


def test_inputs_without_template_are_sent_as_json():
    res = replay("plain").execute(scenario("plain"), _progress)
    assert res.output["text"].strip()
    assert "alias" not in res.output                                     # concrete id used


def test_replay_never_touches_the_network():
    class Boom(httpx.BaseTransport):
        def handle_request(self, request):
            raise AssertionError("network used during replay")

    ex = OpenAICompatExecutor(replay(QUICKSTART).config, transport=Boom(), cassette_name=QUICKSTART)
    assert ex.execute(quickstart_requests()[0], _progress).output["model"] == "qwen2.5:0.5b"


def test_cassettes_are_labelled_with_their_source():
    for f in CASSETTES.glob("*.json"):
        data = json.loads(f.read_text())
        assert data["format"] == "agentos.cassette/1" and data["source"] and data["recorded_at"]
        for i in data["interactions"]:
            assert "authorization" not in {k.lower() for k in i["request"]["headers"]}


# ------------------------------------------------------------------ errors that explain

def test_missing_model_error_says_how_to_pull_it():
    with pytest.raises(ModelNotFound) as e:
        replay("missing_model").execute(scenario("missing_model"), _progress)
    msg = str(e.value)
    assert "ollama pull no-such-model:1b" in msg and "AGENTOS_OPENAI_ALIASES" in msg
    assert "Server said:" in msg                                        # server's own words


def test_cassette_miss_tells_you_to_re_record():
    ex = replay(QUICKSTART)
    req = quickstart_requests()[0].model_copy(update={"inputs": {"run": {"topic": "something else"}}})
    with pytest.raises(CassetteMiss, match="AGENTOS_OPENAI_CASSETTES=record"):
        ex.execute(req, _progress)


def test_unreachable_default_ollama_gives_the_start_hint():
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    ex = OpenAICompatExecutor(ProviderConfig.from_env({}), transport=httpx.MockTransport(down))
    with pytest.raises(ProviderUnreachable) as e:
        ex.execute(scenario("plain"), _progress)
    assert "127.0.0.1:11434" in str(e.value) and "ollama serve" in str(e.value)
    assert ex.health()["reachable"] is False and "ollama serve" in ex.health()["hint"]


def test_auth_failure_names_the_variable_and_key_fixes_it():
    t = reference_server.transport(require_key=True)
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1"})
    with pytest.raises(AuthenticationFailed, match="AGENTOS_OPENAI_API_KEY.*unset"):
        OpenAICompatExecutor(cfg, transport=t).execute(scenario("plain"), _progress)
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1",
                                   "OPENAI_API_KEY": reference_server.API_KEY})
    assert OpenAICompatExecutor(cfg, transport=t).execute(scenario("plain"), _progress).output


@pytest.mark.parametrize("status,exc", [(429, ProviderRateLimited), (500, ProviderServerError),
                                        (503, ProviderServerError)])
def test_transient_server_errors_say_retry_policy_applies(status, exc):
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1"})
    ex = OpenAICompatExecutor(cfg, transport=reference_server.transport(fail_with=status))
    with pytest.raises(exc, match="retry policy applies"):
        ex.execute(scenario("plain"), _progress)


def test_template_error_lists_available_keys():
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1"})
    ex = OpenAICompatExecutor(cfg, transport=reference_server.transport())
    req = request_for("t", {"prompt": "Summarise {draft.text}"}, {"run": {"topic": "x"}})
    with pytest.raises(TemplateError, match=r"\{draft.text\}.*available top-level keys: \['run'\]"):
        ex.execute(req, _progress)


# ------------------------------------------------------------------ config, pricing, hooks

def test_config_env_overrides_and_errors():
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_ALIASES": '{"chat.fast": "gpt-4o-mini"}',
                                   "AGENTOS_OPENAI_BASE_URL": "https://api.openai.com/v1/"})
    assert cfg.resolve("chat.fast") == ("gpt-4o-mini", "chat.fast")
    assert cfg.resolve("chat.default") == ("llama3.2:3b", "chat.default")  # defaults kept
    assert cfg.resolve("gpt-4.1") == ("gpt-4.1", None)
    assert cfg.base_url == "https://api.openai.com/v1"
    with pytest.raises(ConfigError, match="AGENTOS_OPENAI_ALIASES is not valid JSON"):
        ProviderConfig.from_env({"AGENTOS_OPENAI_ALIASES": "{oops"})
    with pytest.raises(ConfigError, match="off \\| replay \\| record"):
        ProviderConfig.from_env({"AGENTOS_OPENAI_CASSETTES": "maybe"})
    with pytest.raises(ConfigError, match="does not exist"):
        ProviderConfig.from_env({"AGENTOS_OPENAI_PRICING": "/nope/pricing.json"})


def test_pricing_is_decimal_and_content_addressed():
    table = PricingTable(ProviderConfig().pricing_path)
    cost, priced = table.cost("gpt-4o-mini", 1000, 500)
    assert priced and cost.amount == "0.00045" and cost.currency == "USD"
    cost, priced = table.cost("gpt-4o-mini-2024-07-18", 1_000_000, 0)   # dated variant
    assert priced and cost.amount == "0.15"
    cost, priced = table.cost("mystery-9b", 10, 10)
    assert not priced and cost.amount == "0" and len(cost.units) == 3
    assert cost.pricing_snapshot_hash == table.sha256 == __import__("hashlib").sha256(
        table.snapshot()).hexdigest()


def test_resolve_describe_and_health_hooks():
    cfg = ProviderConfig.from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1"})
    ex = OpenAICompatExecutor(cfg, transport=reference_server.transport())
    assert ex.resolve(quickstart_requests()[0]) == "qwen2.5:0.5b"
    d = ex.describe()
    assert d["aliases"]["chat.fast"] == "qwen2.5:0.5b" and d["pricing"]["sha256"] == ex.pricing.sha256
    h = ex.health()
    assert h["reachable"] and "qwen2.5:0.5b" in h["models"]
    assert h["aliases_available"] == {"chat.fast": True, "chat.default": True}
    assert ex.pricing_snapshot() == ex.pricing.snapshot()


def test_record_then_replay_round_trip(tmp_path):
    env = {"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1",
           "AGENTOS_OPENAI_CASSETTE_DIR": str(tmp_path)}
    rec = OpenAICompatExecutor(ProviderConfig.from_env({**env, "AGENTOS_OPENAI_CASSETTES": "record"}),
                               transport=reference_server.transport(), cassette_name="rt")
    first = rec.execute(quickstart_requests()[0], _progress)
    assert (tmp_path / "rt.json").exists()
    rep = OpenAICompatExecutor(ProviderConfig.from_env({**env, "AGENTOS_OPENAI_CASSETTES": "replay"}),
                               cassette_name="rt")
    second = rep.execute(quickstart_requests()[0], _progress)
    assert first.output == second.output and first.provenance == second.provenance


def test_agent_definition_round_trips_through_core_models():
    a = Agent(name="writer", type=AgentType.llm, executor="openai-compat",
              config={"model": "chat.fast", "prompt": "x {run.topic}"})
    assert Agent.model_validate_json(a.model_dump_json()) == a
    assert Budget().max_run_cost is None and BlobRef(sha256="0" * 64, size=0).size == 0
