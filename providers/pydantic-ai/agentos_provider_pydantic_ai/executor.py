"""PydanticAI as an AgentOS inner harness.

One AgentOS step = one `Agent.run` on a fresh event loop: the model may loop, call tools
and reason as many times as `max_turns` allows (`UsageLimits.request_limit`), and the whole
trajectory is recorded as ONE `step.completed` — every tool call's name and argument/output
hashes in the output, the run's summed token usage metered as cost, and an `Effect` per
class of tool actually called. The governor's gate ran before this executor was dispatched;
the governor's `_settle` checks the reported effects after. PydanticAI owns the loop;
AgentOS owns the ledger and the authority.

This is the second inner harness (after `agentos-provider-openai-agents`) and it shares the
seam with the first: the same `dagentos.providerkit.tools` registry, the same config keys,
the same output shape — a workflow switches harness by changing `executor` and nothing
downstream moves (tested against the sibling).

Boundaries, each tested:
- Tools are operator-registered Python (`agentos.tools` entry point), named by string in
  agent config. The model is offered ONLY tools whose effect class the AgentOS agent
  declared; the rest are withheld (listed in the output) rather than offered-and-refused.
- Approval / external-execution requests (`DeferredToolRequests`) are NOT auto-resolved:
  this slice cannot suspend mid-step (that needs the message history persisted as a blob
  plus a resume protocol — ROADMAP ⏭). Steps needing approval are gated at STEP
  granularity by the existing tier-2 gate, so a run that comes back deferred is a
  definition error and raises.
- Inputs are delimited data (`wrap_input` / `render_prompt`) and `DATA_BOUNDARY` is in the
  instructions, as in every other provider (C12).
- No SDK telemetry: PydanticAI instrumentation is opt-in and never enabled here.
- Zero-config local default: PydanticAI's OpenAI-compatible chat model against Ollama, no
  key; the client is built from this provider's config only (never `OPENAI_BASE_URL`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

import httpx
import openai
from pydantic_ai import Agent, PromptedOutput, StructuredDict, Tool, UsageLimits
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import DeferredToolRequests

from dagentos.core.models import Effect, EffectClass, Meter, Provenance, StepRequest, StepResult
from dagentos.core.ports import ProgressFn
from dagentos.providerkit.config import ProviderConfig
from dagentos.providerkit.errors import (
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
)
from dagentos.providerkit.pricing import PricingTable
from dagentos.providerkit.prompt import DATA_BOUNDARY, render_prompt, strip_fences, wrap_input
from dagentos.providerkit.schema import OutputSchema
from dagentos.providerkit.tools import ToolRegistry, ToolSpec, load_registry

from .config import from_env

NAME = "pydantic-ai"
try:
    VERSION = _dist_version("agentos-provider-pydantic-ai")
except PackageNotFoundError:  # running from a checkout without install
    VERSION = "0.0.0+dev"

DEFAULT_MAX_TURNS = 6
JSON_INSTRUCTION = "Respond with a single JSON object and nothing else."
# A factory returns the PydanticAI Model, or (Model, client) when it created a client the
# executor must close after the run — the default factory does; test factories do not.
ModelFactory = Callable[[str], "Model | tuple[Model, openai.AsyncOpenAI]"]
RunFn = Callable[..., Awaitable[Any]]


class PydanticAIExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: ProviderConfig | None = None, *,
                 registry: ToolRegistry | None = None,
                 model_factory: ModelFactory | None = None,
                 run: RunFn | None = None) -> None:
        self.config = config or from_env()
        self.pricing = PricingTable(self.config.pricing_path)
        self.registry = registry if registry is not None else load_registry()
        self._model_factory = model_factory or self._default_model
        self._run = run or _run_agent

    # -- optional hooks ------------------------------------------------------------------

    def resolve(self, req: StepRequest) -> str:
        return self.config.resolve(req.agent.config.get("model", "chat.default"))[0]

    def describe(self) -> dict:
        try:
            sdk = _dist_version("pydantic-ai-slim")
        except PackageNotFoundError:
            sdk = "unknown"
        return {
            "base_url": self.config.base_url, "wire_format": "pydantic-ai",
            "sdk_version": sdk, "aliases": dict(self.config.aliases),
            "tools": self.registry.describe(),
            "pricing": {"sha256": self.pricing.sha256, "as_of": self.pricing.data.get("as_of"),
                        "path": str(self.pricing.path)},
            "api_key": "set" if self.config.api_key else "unset",
        }

    def health(self) -> dict:
        try:
            r = httpx.get(f"{self.config.base_url.rstrip('/')}/models", timeout=3.0,
                          headers=self._auth_headers())
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        except httpx.ConnectError as exc:
            return {"reachable": False, "error": str(exc), "hint": self._unreachable_hint()}
        except Exception as exc:  # noqa: BLE001 — health is a report, not a raise
            return {"reachable": False, "error": str(exc)}
        return {"reachable": True, "models": ids[:50],
                "aliases_available": {a: (m in ids) for a, m in self.config.aliases.items()}}

    def pricing_snapshot(self) -> bytes:
        return self.pricing.snapshot()

    # -- the port -----------------------------------------------------------------------

    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        cfg = req.agent.config
        model_id, alias = self.config.resolve(cfg.get("model", "chat.default"))
        schema = OutputSchema.from_config(cfg)          # definition error here, not mid-run
        instructions, user = self._prompt(cfg, req.inputs)
        offered, withheld = self.registry.select(cfg.get("tools", []), req.declared_effects)
        by_name = {s.name: s for s in offered}
        prompt_hash = hashlib.sha256(json.dumps(
            {"instructions": instructions, "input": user, "tools": sorted(by_name),
             "output_schema": schema.prompt_hash_input() if schema else None},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        tools = [Tool(s.fn, name=s.name, description=s.description) for s in offered]
        max_turns = int(cfg.get("max_turns", DEFAULT_MAX_TURNS))
        progress(0.0, f"pydantic-ai run model={model_id} tools={sorted(by_name)} "
                      f"max_turns={max_turns}" + (" typed_output" if schema else ""))
        result = self._run_sdk(req.agent.name, instructions, tools, cfg, user, max_turns,
                               model_id, schema)

        if isinstance(result.output, DeferredToolRequests):
            names = sorted({c.tool_name for c in [*result.output.approvals, *result.output.calls]})
            raise ProviderError(
                f"pydantic-ai run deferred on tools {names} (approval or external execution); "
                f"mid-step approval is not supported — declare the tool's effect class on the "
                f"AgentOS agent so the STEP is gated by the governor before dispatch, and do "
                f"not mark registry tools requires_approval or raise ApprovalRequired/"
                f"CallDeferred from them")

        calls, classes = self._trajectory(result.new_messages(), by_name)
        usage = result.usage
        in_tok, out_tok = int(usage.input_tokens), int(usage.output_tokens)
        cost, priced = self.pricing.cost(
            model_id, in_tok, out_tok,
            extra_meters=[Meter(name="model_requests", quantity=int(usage.requests))])
        progress(1.0, f"{usage.requests} model call(s), {len(calls)} tool call(s), "
                      f"{in_tok}+{out_tok} tokens, {cost.amount} {cost.currency}")

        text = result.output if isinstance(result.output, str) \
            else json.dumps(result.output, default=str, ensure_ascii=False)
        output: dict[str, Any] = {
            "text": text, "model": model_id, "turns": int(usage.requests),
            "tool_calls": calls,
            "tools_offered": sorted(by_name), "tools_withheld": sorted(s.name for s in withheld),
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}, "priced": priced,
        }
        if alias:
            output["alias"] = alias
        if schema is not None:
            # PromptedOutput already parsed the reply into a dict (retrying the model on
            # non-JSON, within max_turns); the CONTRACT is enforced here, once, by the kit.
            value = result.output if isinstance(result.output, dict) \
                else json.loads(strip_fences(text))
            output["json"] = schema.validate(value)
            output["schema_sha256"] = schema.sha256
        elif cfg.get("json_output"):
            try:
                output["json"] = json.loads(strip_fences(text))
            except ValueError as exc:
                raise BadResponse(f"json_output requested but {model_id} returned "
                                  f"non-JSON: {text[:120]!r}") from exc
        effects = [Effect(effect_class=EffectClass.compute,
                          description=f"pydantic-ai run on {model_id}")]
        effects += [Effect(effect_class=ec, description=f"tool {n}") for n, ec in classes]
        return StepResult(
            output=output, effects=effects, cost=cost,
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=model_id, model_alias=alias, prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------

    def _prompt(self, cfg: dict, inputs: dict) -> tuple[str, str]:
        # C12: the agent definition is the only source of instructions; everything
        # interpolated from inputs is delimited data and the instructions say so.
        # `instructions` is PydanticAI's word too; `system` is what the other providers'
        # example agents use — accept both so "change one field" stays true.
        text = cfg.get("instructions") or cfg.get("system")
        parts = [str(text)] if text else []
        if cfg.get("json_output"):
            parts.append(JSON_INSTRUCTION)
        parts.append(DATA_BOUNDARY)
        instructions = "\n\n".join(parts)
        if cfg.get("prompt"):
            user = render_prompt(str(cfg["prompt"]), inputs)
        elif inputs:
            user = wrap_input("inputs", json.dumps(inputs, sort_keys=True, ensure_ascii=False))
        else:
            user = "Hello."
        return instructions, user

    def _run_sdk(self, name: str, instructions: str, tools: list[Tool], cfg: dict, user: str,
                 max_turns: int, model_id: str, schema: OutputSchema | None = None):
        """One fresh event loop per step (`asyncio.run`): the model client is created,
        used and CLOSED inside it, so a long-lived worker thread accumulates no open
        connection pools (same discipline as the openai-agents provider, where a review
        found the SDK's sync helper leaves the thread's loop — and its pools — open).

        With an `output_schema`, the model is told the schema through `PromptedOutput`
        (works on any chat backend, no tool calling needed) and PydanticAI parses the reply
        into a dict, retrying the model on non-JSON within `max_turns`; PydanticAI's
        `StructuredDict` validates only "is an object", so the executor validates the
        contract itself afterwards."""
        if schema is not None:
            typed = StructuredDict(dict(schema.schema))
            output_type: list = [PromptedOutput(typed), DeferredToolRequests]
        else:
            output_type = [str, DeferredToolRequests]

        async def _arun():
            made = self._model_factory(model_id)
            model, client = made if isinstance(made, tuple) else (made, None)
            try:
                agent = Agent(
                    model, name=name, instructions=instructions, tools=tools,
                    output_type=output_type,
                    model_settings={"temperature": cfg.get("temperature", 0)},
                )
                return await self._run(agent, user,
                                       usage_limits=UsageLimits(request_limit=max_turns))
            finally:
                if client is not None:
                    await client.close()

        try:
            return asyncio.run(_arun())
        except UsageLimitExceeded as exc:
            raise BadResponse(f"pydantic-ai run exceeded max_turns={max_turns} on {model_id}: "
                              f"{exc}") from exc
        except ModelHTTPError as exc:
            raise self._map_http(exc, model_id) from exc
        except ModelAPIError as exc:                       # non-HTTP: connection, DNS, timeout
            raise ProviderUnreachable(f"{self.config.base_url} unreachable: {exc}. "
                                      f"{self._unreachable_hint()}") from exc
        except (UnexpectedModelBehavior, AgentRunError) as exc:
            raise BadResponse(f"pydantic-ai run failed on {model_id}: "
                              f"{type(exc).__name__}: {exc}") from exc

    def _map_http(self, exc: ModelHTTPError, model_id: str) -> ProviderError:
        code = exc.status_code
        if code in (401, 403):
            return AuthenticationFailed(f"{self.config.base_url} rejected the API key "
                                        f"(AGENTOS_PYDANTIC_AI_API_KEY / OPENAI_API_KEY)")
        if code == 404:
            return ModelNotFound(f"model {model_id!r} not found at {self.config.base_url}: "
                                 f"{exc}. Local Ollama: `ollama pull {model_id}`")
        if code == 429:
            return ProviderRateLimited(f"{self.config.base_url} rate-limited the request; "
                                       f"retry policy applies: {exc}")
        if code >= 500:
            return ProviderServerError(f"{self.config.base_url} server error {code}: {exc}")
        return BadResponse(f"{self.config.base_url} returned {code} for {model_id}: {exc}")

    @staticmethod
    def _trajectory(messages, by_name: dict[str, ToolSpec]) \
            -> tuple[list[dict[str, Any]], list[tuple[str, EffectClass]]]:
        """Tool calls as (name, arguments hash, output hash) — the trajectory is in the
        log without inlining potentially large tool outputs — plus one (name, class) per
        DISTINCT tool called, for the effects list.

        A call whose arguments failed validation never ran the tool: PydanticAI answers it
        with a `RetryPromptPart` and lets the model try again (bounded by `max_turns`).
        Such a call is recorded with `rejected: true` and the hash of the retry message,
        so the log distinguishes "tool returned X" from "tool refused the arguments"
        (seen live: a 0.5B model called `word_count` with the wrong key). It still counts
        as an attempted effect — reporting the attempt is the conservative direction."""
        calls: list[dict[str, Any]] = []
        outputs: dict[str, str] = {}
        rejected: dict[str, str] = {}
        for msg in messages:
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    outputs[part.tool_call_id] = _sha(part.model_response_str())
                elif isinstance(part, RetryPromptPart) and part.tool_call_id:
                    rejected[part.tool_call_id] = _sha(part.model_response())
        for msg in messages:
            if not isinstance(msg, ModelResponse):
                continue
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    rec: dict[str, Any] = {
                        "name": part.tool_name, "call_id": part.tool_call_id,
                        "arguments_sha256": _sha(part.args_as_json_str()),
                        "output_sha256": outputs.get(part.tool_call_id),
                    }
                    if part.tool_call_id in rejected and part.tool_call_id not in outputs:
                        rec["rejected"] = True
                        rec["output_sha256"] = rejected[part.tool_call_id]
                    calls.append(rec)
        classes = sorted({(c["name"], by_name[c["name"]].effect_class)
                          for c in calls if c["name"] in by_name})
        return calls, classes

    def _default_model(self, model_id: str) -> tuple[Model, openai.AsyncOpenAI]:
        """Called INSIDE the step's event loop so the client binds to it; returned with the
        model so `_run_sdk` closes it when the run ends. The client is built from this
        provider's config, never from `OPENAI_BASE_URL` / `OPENAI_API_KEY` implicitly."""
        client = openai.AsyncOpenAI(base_url=self.config.base_url,
                                    api_key=self.config.api_key or "ollama",
                                    timeout=self.config.timeout_seconds)
        return OpenAIChatModel(model_id, provider=OpenAIProvider(openai_client=client)), client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def _unreachable_hint(self) -> str:
        if "11434" in self.config.base_url:
            return "Is Ollama running? `ollama serve` (then `ollama pull qwen2.5:0.5b`)."
        return "Check AGENTOS_PYDANTIC_AI_BASE_URL and that the server is up."


async def _run_agent(agent: Agent, user: str, **kwargs):
    return await agent.run(user, **kwargs)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
