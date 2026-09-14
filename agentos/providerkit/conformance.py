"""Conformance scenarios every provider records and replays (§11 A10).

The scenarios are the repository's own example agents (`examples/*.json`) — the same
definitions the quickstart uses — so a provider that passes them runs the quickstart.
Each provider's `scripts/record_cassettes.py` calls `record()` with its executor; its
tests call `quickstart_requests()` / `SCENARIOS` to replay.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from agentos.core.models import Agent, AgentType, BlobRef, Budget, StepRequest
from agentos.core.ports import Executor

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# Single-request scenarios: name → (agent config, step inputs). Cassette file = name.
SCENARIOS: dict[str, tuple[dict, dict]] = {
    "plain": ({"model": "chat.fast"}, {"a": {"n": 1}}),
    "missing_model": ({"model": "no-such-model:1b", "prompt": "hi"}, {}),
}
# The quickstart chain (docs/quickstart-llm.md): poet → critic, recorded into ONE cassette
# so the API-level quickstart test replays it end to end.
QUICKSTART = "quickstart"
QUICKSTART_INPUTS = {"run": {"topic": "event logs"}}


def example_config(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text())["config"]


def request_for(name: str, config: dict, inputs: dict, executor: str = "test") -> StepRequest:
    return StepRequest(
        run_id="rec", step_id=name, attempt=1, idempotency_key=f"rec:{name}",
        agent=Agent(name=name, type=AgentType.llm, executor=executor, config=config),
        inputs=inputs, inputs_ref=BlobRef(sha256="0" * 64, size=0),
        declared_effects=frozenset(), budget=Budget(),
    )


def quickstart_requests(poet_text: str | None = None) -> list[StepRequest]:
    """The poet request, and — once the poet's text is known — the critic's."""
    reqs = [request_for("write", example_config("poet_agent"), QUICKSTART_INPUTS)]
    if poet_text is not None:
        reqs.append(request_for("review", example_config("critic_agent"),
                                {"write": {"text": poet_text}, **QUICKSTART_INPUTS}))
    return reqs


def record(make_executor: Callable[[str], Executor], cassette_dir: Path,
           only: list[str] | None = None, echo: Callable[[str], None] = print) -> int:
    """Record every scenario. `make_executor(cassette_name)` returns an executor already
    configured for `record` mode into `cassette_dir`. Error scenarios are recorded on
    purpose. Returns the number of scenarios that raised unexpectedly (missing_model is
    expected to raise)."""
    unexpected = 0

    def run(label: str, req: StepRequest, ex: Executor):
        nonlocal unexpected
        try:
            result = ex.execute(req, lambda f, n: None)
            echo(f"{label:14s} ok   {result.provenance.model_id} "
                 f"{result.cost.amount} {result.cost.currency}")
            return result
        except Exception as exc:  # noqa: BLE001 — recorded on purpose
            echo(f"{label:14s} err  {type(exc).__name__}: {str(exc)[:90]}")
            if label != "missing_model":
                unexpected += 1
            return None

    for name in only or [*SCENARIOS, QUICKSTART]:
        path = cassette_dir / f"{name}.json"
        if path.exists():
            path.unlink()
        ex = make_executor(name)
        if name == QUICKSTART:
            poet = run("write", quickstart_requests()[0], ex)
            if poet is not None:
                run("review", quickstart_requests(poet.output["text"])[1], ex)
        else:
            cfg, inputs = SCENARIOS[name]
            run(name, request_for(name, cfg, inputs), ex)
        echo(f"{'':14s}  → {path}")
    return unexpected
