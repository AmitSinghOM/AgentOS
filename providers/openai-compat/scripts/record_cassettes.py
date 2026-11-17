"""Record the cassettes the replay tests use (docs/DEVELOPMENT_STRUCTURE.md §11 A10).

    python scripts/record_cassettes.py --live   # against AGENTOS_OPENAI_BASE_URL (default:
                                                # local Ollama) — what the committed
                                                # cassettes came from
    python scripts/record_cassettes.py          # against the in-process reference server
    python scripts/record_cassettes.py --tools  # tests/cassettes/tool_round_trip.json, against
                                                # the reference server: it replays a SCRIPTED
                                                # function-calling trajectory (a rejected call,
                                                # a bad argument...), which a live model would
                                                # not follow
    python scripts/record_cassettes.py --tools --live
                                                # tests/cassettes/tool_round_trip_live.json:
                                                # the quickstart's `counter` agent, as written
                                                # on docs/quickstart-llm.md, against the real
                                                # server (`chat.default`; set
                                                # AGENTOS_OPENAI_ALIASES to pick the model).
                                                # The one committed fixture that saw a real
                                                # model's `tool_calls`; the nightly re-records it

Scenarios are shared by every provider: `dagentos.providerkit.conformance`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))

import openai_reference_server
from agentos_provider_openai_compat import OpenAICompatExecutor, from_env

from dagentos.core.models import EffectClass
from dagentos.providerkit.conformance import record, request_for
from dagentos.providerkit.tools import BUILTIN_TOOLS, ToolRegistry

CASSETTE_DIR = HERE / "tests" / "cassettes"
TOOL_CASSETTE = "tool_round_trip"
TOOL_LIVE_CASSETTE = "tool_round_trip_live"
# The phrase the live cassette counts. Measured clean (one word_count call, right count) on
# qwen2.5:1.5b (the nightly's tool model, 3/3) and llama3.2:3b (the quickstart's default,
# every probe run); "the quick brown fox" and "HTML tags" are the phrases a 3B model gets
# wrong (`scripts/probe_tool_boundary.py`), so they stay out of the fixture.
TOOL_LIVE_INPUTS = {"run": {"topic": "event logs"}}
QUICKSTART_LLM = HERE.parents[1] / "docs" / "quickstart-llm.md"


def quickstart_tool_agent() -> dict:
    """The `counter` agent JSON from docs/quickstart-llm.md "Letting the model call a tool",
    unedited — the live cassette records exactly what the page tells a reader to run."""
    section = QUICKSTART_LLM.read_text().split("### Letting the model call a tool", 1)[1]
    return json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))


def record_tools_live(cassette_dir: Path = CASSETTE_DIR, env: dict[str, str] | None = None) -> int:
    """Record the quickstart's tool round trip against the REAL server at
    AGENTOS_OPENAI_BASE_URL (temperature 0, seed 42 from the executor's defaults, so a
    re-record on the same model and server is stable). The cassette's `source` names the
    server, the model that answered and the Ollama version. Returns 0 when the model made
    exactly one clean `word_count` call and answered, 1 otherwise (the file is still written
    so the miss can be read)."""
    path = cassette_dir / f"{TOOL_LIVE_CASSETTE}.json"
    if path.exists():
        path.unlink()
    env = {**(os.environ if env is None else env),
           "AGENTOS_OPENAI_CASSETTES": "record", "AGENTOS_OPENAI_CASSETTE_DIR": str(cassette_dir)}
    cfg = from_env(env)
    agent = quickstart_tool_agent()
    declared = frozenset(EffectClass(e) for e in agent["declared_effects"])
    ex = OpenAICompatExecutor(cfg, cassette_name=TOOL_LIVE_CASSETTE,
                              registry=ToolRegistry(BUILTIN_TOOLS))
    req = request_for(TOOL_LIVE_CASSETTE, agent["config"], TOOL_LIVE_INPUTS)
    req = req.model_copy(update={"declared_effects": declared})
    result = ex.execute(req, lambda f, n: None)
    calls = result.output["tool_calls"]
    clean = [c["name"] for c in calls] == ["word_count"] and "rejected" not in calls[0]
    print(f"{TOOL_LIVE_CASSETTE:14s} {'ok  ' if clean else 'MISS'} {result.output['model']} "
          f"{len(calls)} tool call(s), {result.output['turns']} turns: "
          f"{result.output['text'][:60]!r}\n{'':14s}  → {path}")
    data = json.loads(path.read_text())
    data["source"] = (f"{cfg.base_url} model={result.output['model']} "
                      f"ollama={_ollama_version(cfg.base_url)}")
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    return 0 if clean else 1


def _ollama_version(base_url: str) -> str:
    """Ollama's `/api/version` sits beside `/v1`; another server has none → "unknown"."""
    try:
        r = httpx.get(base_url.removesuffix("/v1") + "/api/version", timeout=5)
        r.raise_for_status()
        return str(r.json().get("version", "unknown"))
    except (httpx.HTTPError, ValueError, AttributeError):
        return "unknown"


def record_tools(cassette_dir: Path = CASSETTE_DIR) -> int:
    """Record the function-calling round trip (one `word_count` call, then an answer)
    against the reference server's scripted trajectory. Returns 0 on success."""
    path = cassette_dir / f"{TOOL_CASSETTE}.json"
    if path.exists():
        path.unlink()
    cfg = from_env({"AGENTOS_OPENAI_BASE_URL": "http://reference-server/v1",
                    "AGENTOS_OPENAI_CASSETTES": "record",
                    "AGENTOS_OPENAI_CASSETTE_DIR": str(cassette_dir)})
    transport = openai_reference_server.transport(
        tool_script=openai_reference_server.TOOL_ROUND_TRIP_SCRIPT)
    ex = OpenAICompatExecutor(cfg, transport=transport, cassette_name=TOOL_CASSETTE,
                              registry=ToolRegistry(BUILTIN_TOOLS))
    req = request_for(TOOL_CASSETTE, openai_reference_server.TOOL_ROUND_TRIP_CONFIG,
                      openai_reference_server.TOOL_ROUND_TRIP_INPUTS)
    req = req.model_copy(update={"declared_effects": frozenset({EffectClass.compute})})
    result = ex.execute(req, lambda f, n: None)
    print(f"{TOOL_CASSETTE:14s} ok   {len(result.output['tool_calls'])} tool call(s)\n"
          f"{'':14s}  → {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="record against a real server")
    p.add_argument("--tools", action="store_true",
                   help="record the function-calling cassette: scripted (reference server), "
                        "or the quickstart's agent against the real server with --live")
    p.add_argument("--only", nargs="*", default=None)
    args = p.parse_args(argv)
    if args.tools:
        return record_tools_live() if args.live else record_tools()

    env = dict(os.environ)
    env["AGENTOS_OPENAI_CASSETTES"] = "record"
    env["AGENTOS_OPENAI_CASSETTE_DIR"] = str(CASSETTE_DIR)
    if not args.live:
        env.setdefault("AGENTOS_OPENAI_BASE_URL", "http://reference-server/v1")
    cfg = from_env(env)
    transport = None if args.live else openai_reference_server.transport()
    return record(lambda name: OpenAICompatExecutor(cfg, transport=transport, cassette_name=name),
                  CASSETTE_DIR, args.only)


if __name__ == "__main__":
    raise SystemExit(main())
