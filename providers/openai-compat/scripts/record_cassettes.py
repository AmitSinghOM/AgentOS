"""Record the cassettes the replay tests use (docs/DEVELOPMENT_STRUCTURE.md §11 A10).

    python scripts/record_cassettes.py --live          # against AGENTOS_OPENAI_BASE_URL
                                                       # (default: local Ollama) — what the
                                                       # committed cassettes came from
    python scripts/record_cassettes.py                 # against the in-process reference
                                                       # server, when no model is available

The scenarios are the single source of truth shared with `tests/test_executor.py`; every
scenario is one agent config + inputs, and the cassette file is named after it. Each
cassette records its `source` URL so a reader can tell live from reference at a glance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))

import reference_server
from agentos_provider_openai_compat import OpenAICompatExecutor, ProviderConfig

from agentos.core.models import Agent, AgentType, BlobRef, Budget, StepRequest

CASSETTE_DIR = HERE / "tests" / "cassettes"
EXAMPLES = HERE.parent.parent / "examples"


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text())["config"]


# Single-request scenarios: name → (agent config, step inputs). Cassette file = name.
SCENARIOS: dict[str, tuple[dict, dict]] = {
    "plain": ({"model": "qwen2.5:0.5b"}, {"a": {"n": 1}}),
    "missing_model": ({"model": "no-such-model:1b", "prompt": "hi"}, {}),
}
# The quickstart chain (docs/quickstart-llm.md): the exact example agents, poet → critic,
# recorded into ONE cassette so the API-level quickstart test replays it end to end.
QUICKSTART = "quickstart"
QUICKSTART_INPUTS = {"run": {"topic": "event logs"}}


def request_for(name: str, config: dict, inputs: dict) -> StepRequest:
    return StepRequest(
        run_id="rec", step_id=name, attempt=1, idempotency_key=f"rec:{name}",
        agent=Agent(name=name, type=AgentType.llm, executor="openai-compat", config=config),
        inputs=inputs, inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(), budget=Budget(),
    )


def quickstart_requests(poet_text: str | None = None) -> list[StepRequest]:
    """The poet request, and — once the poet's text is known — the critic's."""
    reqs = [request_for("write", _example("poet_agent"), QUICKSTART_INPUTS)]
    if poet_text is not None:
        reqs.append(request_for("review", _example("critic_agent"),
                                {"write": {"text": poet_text}, **QUICKSTART_INPUTS}))
    return reqs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="record against a real server")
    p.add_argument("--only", nargs="*", default=None)
    args = p.parse_args(argv)

    env = dict(os.environ)
    env["AGENTOS_OPENAI_CASSETTES"] = "record"
    env["AGENTOS_OPENAI_CASSETTE_DIR"] = str(CASSETTE_DIR)
    if not args.live:
        env.setdefault("AGENTOS_OPENAI_BASE_URL", "http://reference-server/v1")
    cfg = ProviderConfig.from_env(env)
    transport = None if args.live else reference_server.transport()

    def run(name: str, req: StepRequest, ex: OpenAICompatExecutor):
        try:
            result = ex.execute(req, lambda f, n: None)
            print(f"{name:14s} ok   {result.provenance.model_id} "
                  f"{result.cost.amount} {result.cost.currency}")
            return result
        except Exception as exc:  # noqa: BLE001 — error scenarios are recorded on purpose
            print(f"{name:14s} err  {type(exc).__name__}: {str(exc)[:90]}")
            return None

    wanted = args.only or [*SCENARIOS, QUICKSTART]
    for name in wanted:
        path = CASSETTE_DIR / f"{name}.json"
        if path.exists():
            path.unlink()
        ex = OpenAICompatExecutor(cfg, transport=transport, cassette_name=name)
        if name == QUICKSTART:
            poet = run("write", quickstart_requests()[0], ex)
            if poet is not None:
                run("review", quickstart_requests(poet.output["text"])[1], ex)
        else:
            agent_cfg, inputs = SCENARIOS[name]
            run(name, request_for(name, agent_cfg, inputs), ex)
        print(f"{'':14s}  → {path.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
