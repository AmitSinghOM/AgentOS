"""Record the cassettes the replay tests use (docs/DEVELOPMENT_STRUCTURE.md §11 A10).

    python scripts/record_cassettes.py --live   # against AGENTOS_ANTHROPIC_BASE_URL
                                                # (default: local Ollama /v1/messages)
    python scripts/record_cassettes.py          # against the in-process reference server

Scenarios are shared by every provider: `dagentos.providerkit.conformance`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))

import anthropic_reference_server
from agentos_provider_anthropic import AnthropicExecutor, from_env

from dagentos.providerkit.conformance import record

CASSETTE_DIR = HERE / "tests" / "cassettes"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="record against a real server")
    p.add_argument("--only", nargs="*", default=None)
    args = p.parse_args(argv)

    env = dict(os.environ)
    env["AGENTOS_ANTHROPIC_CASSETTES"] = "record"
    env["AGENTOS_ANTHROPIC_CASSETTE_DIR"] = str(CASSETTE_DIR)
    if not args.live:
        env.setdefault("AGENTOS_ANTHROPIC_BASE_URL", "http://reference-server")
    cfg = from_env(env)
    transport = None if args.live else anthropic_reference_server.transport()
    return record(lambda name: AnthropicExecutor(cfg, transport=transport, cassette_name=name),
                  CASSETTE_DIR, args.only)


if __name__ == "__main__":
    raise SystemExit(main())
