"""Record the cassettes the replay tests use (docs/DEVELOPMENT_STRUCTURE.md §11 A10).

    python scripts/record_cassettes.py --live   # against AGENTOS_OPENAI_BASE_URL (default:
                                                # local Ollama) — what the committed
                                                # cassettes came from
    python scripts/record_cassettes.py          # against the in-process reference server
    python scripts/record_cassettes.py --tools  # tests/cassettes/tool_round_trip.json, always
                                                # against the reference server: it replays a
                                                # SCRIPTED function-calling trajectory, which a
                                                # live model would not follow

Scenarios are shared by every provider: `dagentos.providerkit.conformance`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))

import openai_reference_server
from agentos_provider_openai_compat import OpenAICompatExecutor, from_env

from dagentos.core.models import EffectClass
from dagentos.providerkit.conformance import record, request_for
from dagentos.providerkit.tools import BUILTIN_TOOLS, ToolRegistry

CASSETTE_DIR = HERE / "tests" / "cassettes"
TOOL_CASSETTE = "tool_round_trip"


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
                   help="record the function-calling cassette (reference server only)")
    p.add_argument("--only", nargs="*", default=None)
    args = p.parse_args(argv)
    if args.tools:
        return record_tools()

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
