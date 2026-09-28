"""agentos-provider-acp — every run here drives a REAL subprocess (`tests/fake_acp_agent.py`)
over real newline-delimited JSON-RPC on stdio, the framing `kiro-cli acp` uses; no mocks of
the transport. kiro-cli itself is not required (the live path is `scripts/quickstart_acp.py`).

Boundaries under test:
- the handshake: `initialize` advertises NO fs/terminal capability (AgentOS lends the agent
  nothing of its own), `session/new` uses the configured cwd, the prompt is the rendered
  agent prompt with the inputs as delimited data (C12);
- a text turn: chunks concatenated into `text`, stop reason recorded, `compute` effect,
  provenance names the harness command;
- a tool call the agent runs without asking is recorded (title, kind, hashes) and REPORTED
  by its mapped effect class — declared → completes; undeclared → the core dead-letters it;
- `session/request_permission` is answered from the step's declaration: declared class →
  `allow_once` (never `allow_always`), undeclared → `reject_once`; the decision is recorded;
- usage_update → context tokens metered, ACP cost carried into the step cost;
- `refusal` stop reason is a BadResponse (retry policy applies), a hung agent is killed at
  the timeout, a protocol-version mismatch and a crashed agent fail with the fix named;
- the subprocess is gone after every run, success or failure;
- config comes from AGENTOS_ACP_* only, and ConfigError names the offending variable;
- through the core: a step declaring an approval-required class is suspended before the
  agent is ever spawned.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("agentos_provider_acp")   # pyproject testpaths: skip when not installed
from agentos_provider_acp import (
    AcpConfig,
    AcpExecutor,
    BadResponse,
    ConfigError,
    ProviderError,
    ProviderUnreachable,
    effect_class_for_kind,
    from_env,
    permission_decision,
)

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

FAKE = Path(__file__).with_name("fake_acp_agent.py")


# ----------------------------------------------------------------------------- fixtures
def executor(tmp_path: Path, scenario: str, *, timeout: float = 10.0) -> tuple[AcpExecutor, Path]:
    """An executor whose agent is the fake, plus the path of the fake's traffic log."""
    log = tmp_path / f"{scenario}.jsonl"
    cfg = AcpConfig(command=[sys.executable, str(FAKE)], cwd=tmp_path,
                    timeout_seconds=timeout,
                    env={"FAKE_ACP_SCENARIO": scenario, "FAKE_ACP_LOG": str(log)})
    return AcpExecutor(cfg), log


def req(declared=(EffectClass.compute,), config: dict | None = None,
        inputs: dict | None = None) -> StepRequest:
    return StepRequest(
        run_id="r", step_id="s", attempt=1, idempotency_key="r:s",
        agent=Agent(name="coder", type=AgentType.llm, executor="acp",
                    config={"prompt": "Summarise {run.topic}.", **(config or {})},
                    declared_effects=list(declared)),
        inputs=inputs or {"run": {"topic": "event logs"}},
        inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(declared), budget=Budget(),
    )


def traffic(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def sent_by_executor(log: Path, method: str) -> list[dict]:
    return [t["msg"] for t in traffic(log) if t["dir"] == "in" and t["msg"].get("method") == method]


def noop_progress(fraction: float, note: str = "") -> None:
    pass


def fake_pids() -> set[int]:
    """PIDs of any fake agent still alive (the executor must leave none behind)."""
    out = subprocess.run(["pgrep", "-f", str(FAKE)], capture_output=True, text=True,
                         check=False).stdout
    return {int(p) for p in out.split()} if out.strip() else set()


# ------------------------------------------------------------------------- the handshake
def test_handshake_lends_nothing_and_prompts_with_delimited_inputs(tmp_path):
    ex, log = executor(tmp_path, "text")
    ex.execute(req(), noop_progress)

    init = sent_by_executor(log, "initialize")
    assert len(init) == 1
    caps = init[0]["params"]["clientCapabilities"]
    assert init[0]["params"]["protocolVersion"] == 1
    assert caps["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert caps["terminal"] is False

    new = sent_by_executor(log, "session/new")[0]["params"]
    assert new["cwd"] == str(tmp_path) and new["mcpServers"] == []

    prompt = sent_by_executor(log, "session/prompt")[0]["params"]
    assert prompt["sessionId"] == "sess-fake-1"
    [block] = prompt["prompt"]
    assert block["type"] == "text"
    assert "Summarise " in block["text"] and "event logs" in block["text"]
    assert "<<<" in block["text"] or "data" in block["text"].lower()   # C12 delimiting present


def test_text_turn_records_output_stop_reason_effect_and_provenance(tmp_path):
    ex, _ = executor(tmp_path, "text")
    res = ex.execute(req(), noop_progress)
    assert res.output["text"] == "Hello from the fake agent."
    assert res.output["stop_reason"] == "end_turn"
    assert res.output["tool_calls"] == []
    assert [e.effect_class for e in res.effects] == [EffectClass.compute]
    assert res.provenance.executor == "acp"
    assert res.provenance.model_id == "fake-acp"          # the agent's advertised name
    assert res.provenance.prompt_hash and len(res.provenance.prompt_hash) == 64
    assert res.cost.amount == "0"


# ----------------------------------------------------------------------------- tool calls
def test_tool_call_without_permission_is_recorded_and_reported_by_class(tmp_path):
    ex, _ = executor(tmp_path, "tool_read")
    res = ex.execute(req(declared=(EffectClass.compute, EffectClass.read)), noop_progress)
    [call] = res.output["tool_calls"]
    assert call["call_id"] == "call_1" and call["kind"] == "read"
    assert call["title"] == "Read README.md" and call["status"] == "completed"
    assert call["effect_class"] == "read"
    assert len(call["raw_input_sha256"]) == 64 and len(call["raw_output_sha256"]) == 64
    assert "rawInput" not in json.dumps(res.output)       # hashes, never inlined payloads
    assert {e.effect_class for e in res.effects} == {EffectClass.compute, EffectClass.read}


def test_undeclared_tool_class_is_reported_so_the_core_dead_letters_it(tmp_path):
    """The agent edited a file without asking; the executor reports `execute_code`
    honestly and the governor refuses the step — the conservative direction."""
    ex, _ = executor(tmp_path, "undeclared")
    res = ex.execute(req(declared=(EffectClass.compute,)), noop_progress)
    assert EffectClass.execute_code in {e.effect_class for e in res.effects}

    store = MemoryStore()
    store.put_agent(Agent(name="coder", type=AgentType.llm, executor="acp",
                          config={"prompt": "Fix it."},
                          declared_effects=[EffectClass.compute]))
    store.put_workflow(WorkflowDefinition(name="fix", nodes=[{"id": "fix", "agent": "coder"}]))
    engine = Engine(store=store, blobs=store, executors={"acp": ex})
    run = engine.start_run("fix")
    while run.status in (RunStatus.pending, RunStatus.running):
        run = engine.advance(run.id)
    assert run.status is RunStatus.failed
    types = [type(e).event_type for e in store.read_events(run.id)]
    assert "step.dead_lettered" in types
    dead = next(e for e in store.read_events(run.id) if type(e).event_type == "step.dead_lettered")
    assert "undeclared effect 'execute_code'" in dead.cause


# ------------------------------------------------------------------------------ permission
def test_permission_for_a_declared_class_is_allowed_once_never_always(tmp_path):
    ex, log = executor(tmp_path, "permission")
    res = ex.execute(req(declared=(EffectClass.compute, EffectClass.execute_code)), noop_progress)
    [answer] = [t["msg"] for t in traffic(log) if t["dir"] == "in" and "result" in t["msg"]
                and t["msg"]["id"] > 1000]
    assert answer["result"]["outcome"] == {"outcome": "selected", "optionId": "allow-once"}
    [call] = res.output["tool_calls"]
    assert call["permission"] == "allowed" and call["status"] == "completed"
    assert res.output["text"] == "Tests pass."


def test_permission_for_an_undeclared_class_is_rejected_and_recorded(tmp_path):
    ex, log = executor(tmp_path, "permission")
    res = ex.execute(req(declared=(EffectClass.compute,)), noop_progress)
    [answer] = [t["msg"] for t in traffic(log) if t["dir"] == "in" and "result" in t["msg"]
                and t["msg"]["id"] > 1000]
    assert answer["result"]["outcome"]["optionId"] == "reject-once"
    [call] = res.output["tool_calls"]
    assert call["permission"] == "rejected" and call["status"] == "failed"
    # A rejected call never ran, so it is NOT an effect — the step stays within its declaration.
    assert {e.effect_class for e in res.effects} == {EffectClass.compute}
    assert res.output["text"] == "I was not allowed to run the tests."


@pytest.mark.parametrize("kind,expected", [
    ("read", EffectClass.read), ("search", EffectClass.read), ("fetch", EffectClass.read),
    ("think", EffectClass.compute), ("switch_mode", EffectClass.compute),
    ("edit", EffectClass.execute_code), ("delete", EffectClass.execute_code),
    ("move", EffectClass.execute_code), ("execute", EffectClass.execute_code),
    ("other", EffectClass.execute_code), (None, EffectClass.execute_code),
    ("some-future-kind", EffectClass.execute_code),
])
def test_tool_kind_maps_to_an_effect_class_conservatively(kind, expected):
    assert effect_class_for_kind(kind) is expected


def test_permission_decision_prefers_allow_once_and_falls_back_sanely():
    opts = [{"optionId": "a", "kind": "allow_always"}, {"optionId": "o", "kind": "allow_once"},
            {"optionId": "r", "kind": "reject_once"}, {"optionId": "ra", "kind": "reject_always"}]
    assert permission_decision(opts, allowed=True) == ("o", "allowed")
    assert permission_decision(opts, allowed=False) == ("r", "rejected")
    # Only an allow_always on offer: still never select it — decisions are per step.
    assert permission_decision([{"optionId": "a", "kind": "allow_always"},
                                {"optionId": "r", "kind": "reject_once"}], allowed=True) \
        == ("r", "rejected")
    # No reject option at all: cancel rather than allow.
    assert permission_decision([{"optionId": "a", "kind": "allow_always"}], allowed=False) \
        == (None, "cancelled")


# ----------------------------------------------------------------------------------- usage
def test_usage_update_is_metered_and_acp_cost_is_carried(tmp_path):
    ex, _ = executor(tmp_path, "usage")
    res = ex.execute(req(), noop_progress)
    meters = {m.name: m.quantity for m in res.cost.units}
    assert meters["context_tokens_used"] == 1234
    assert meters["tool_calls"] == 0
    assert res.cost.amount == "0.0042" and res.cost.currency == "USD"
    assert res.output["usage"] == {"context_tokens_used": 1234, "context_window": 200000}


# -------------------------------------------------------------------------------- failures
def test_refusal_is_a_bad_response_that_names_the_stop_reason(tmp_path):
    ex, _ = executor(tmp_path, "refusal")
    with pytest.raises(BadResponse, match="refusal"):
        ex.execute(req(), noop_progress)


def test_hung_agent_is_killed_at_the_timeout_and_no_process_remains(tmp_path):
    ex, _ = executor(tmp_path, "hang", timeout=1.5)
    before = fake_pids()
    with pytest.raises(ProviderError, match="timed out after 1.5s"):
        ex.execute(req(), noop_progress)
    assert fake_pids() - before == set()


def test_protocol_version_mismatch_fails_with_the_versions_named(tmp_path):
    ex, _ = executor(tmp_path, "bad_version")
    with pytest.raises(ProviderError, match="protocolVersion 99.*expected 1"):
        ex.execute(req(), noop_progress)


def test_agent_that_exits_early_is_unreachable_with_exit_code_named(tmp_path):
    ex, _ = executor(tmp_path, "crash")
    with pytest.raises(ProviderUnreachable, match="exited with 3"):
        ex.execute(req(), noop_progress)


def test_missing_command_is_unreachable_with_the_fix_named(tmp_path):
    cfg = AcpConfig(command=["definitely-not-an-acp-agent-xyz"], cwd=tmp_path)
    with pytest.raises(ProviderUnreachable, match="AGENTOS_ACP_COMMAND"):
        AcpExecutor(cfg).execute(req(), noop_progress)


def test_no_subprocess_survives_a_successful_run(tmp_path):
    ex, _ = executor(tmp_path, "text")
    before = fake_pids()
    ex.execute(req(), noop_progress)
    assert fake_pids() - before == set()


# ---------------------------------------------------------------------------------- config
def test_config_from_env_reads_only_agentos_acp_variables(tmp_path):
    cfg = from_env({"AGENTOS_ACP_COMMAND": json.dumps(["kiro-cli", "acp", "--agent", "x"]),
                    "AGENTOS_ACP_CWD": str(tmp_path), "AGENTOS_ACP_TIMEOUT": "42",
                    "KIRO_SOMETHING": "ignored"})
    assert cfg.command == ["kiro-cli", "acp", "--agent", "x"]
    assert cfg.cwd == tmp_path and cfg.timeout_seconds == 42.0


def test_config_defaults_to_kiro_cli_acp_in_the_current_directory():
    cfg = from_env({})
    assert cfg.command == ["kiro-cli", "acp"]
    assert cfg.cwd == Path.cwd()
    assert cfg.timeout_seconds == 600.0


@pytest.mark.parametrize("env,var", [
    ({"AGENTOS_ACP_COMMAND": "not json"}, "AGENTOS_ACP_COMMAND"),
    ({"AGENTOS_ACP_COMMAND": "[]"}, "AGENTOS_ACP_COMMAND"),
    ({"AGENTOS_ACP_COMMAND": '"kiro-cli acp"'}, "AGENTOS_ACP_COMMAND"),
    ({"AGENTOS_ACP_TIMEOUT": "soon"}, "AGENTOS_ACP_TIMEOUT"),
    ({"AGENTOS_ACP_TIMEOUT": "0"}, "AGENTOS_ACP_TIMEOUT"),
    ({"AGENTOS_ACP_CWD": "relative/path"}, "AGENTOS_ACP_CWD"),
])
def test_bad_config_names_the_variable(env, var):
    with pytest.raises(ConfigError, match=var):
        from_env(env)


# ------------------------------------------------------------------------- describe/health
def test_describe_and_health_report_the_harness(tmp_path):
    ex, _ = executor(tmp_path, "text")
    d = ex.describe()
    assert d["wire_format"] == "acp" and d["protocol_version"] == 1
    assert d["command"] == [sys.executable, str(FAKE)]
    assert d["client_capabilities"] == {"fs": {"readTextFile": False, "writeTextFile": False},
                                        "terminal": False}
    h = ex.health()
    assert h["reachable"] is True and h["agent"] == {"name": "fake-acp", "version": "0.0.1"}


def test_health_names_the_fix_when_the_command_is_missing(tmp_path):
    ex = AcpExecutor(AcpConfig(command=["definitely-not-an-acp-agent-xyz"], cwd=tmp_path))
    h = ex.health()
    assert h["reachable"] is False and "AGENTOS_ACP_COMMAND" in h["hint"]


def test_health_is_cached_so_get_executors_does_not_respawn_the_agent(tmp_path):
    """`GET /executors` calls health() per request; spawning the agent costs seconds."""
    ex, log = executor(tmp_path, "text")
    ex.health()
    ex.health()
    assert len(sent_by_executor(log, "initialize")) == 1


# ------------------------------------------------------------------------ through the core
def test_a_step_declaring_an_approval_required_class_is_suspended_before_spawn(tmp_path):
    """`spend` is approval-required under the default budget: the run suspends and the
    agent process is never started — no step.started, no traffic in the fake's log."""
    ex, log = executor(tmp_path, "text")
    store = MemoryStore()
    store.put_agent(Agent(name="payer", type=AgentType.llm, executor="acp",
                          config={"prompt": "Pay the invoice."},
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="pay", nodes=[{"id": "pay", "agent": "payer"}]))
    engine = Engine(store=store, blobs=store, executors={"acp": ex})
    run = engine.start_run("pay")
    assert run.status is RunStatus.suspended
    assert [type(e).event_type for e in store.read_events(run.id)] == \
        ["run.started", "approval.requested", "run.suspended"]
    assert not log.exists()                                   # never spawned


def test_plugin_discovery_finds_the_executor():
    from dagentos.plugins import discover_executors
    found = discover_executors()
    assert "acp" in found and found["acp"].name == "acp"


@pytest.mark.skipif(shutil.which("kiro-cli") is None or not os.environ.get("AGENTOS_ACP_LIVE"),
                    reason="live kiro-cli run only with AGENTOS_ACP_LIVE=1 and kiro-cli on PATH")
def test_live_kiro_cli_health():
    h = AcpExecutor(from_env({})).health()
    assert h["reachable"] is True and h["agent"]["name"]
