"""OpenAI Agents SDK as an AgentOS inner harness.

One AgentOS step = one SDK run (`Runner.run` on a fresh event loop): the model may loop, call tools and
reason as many times as `max_turns` allows, and the whole trajectory is recorded as ONE
`step.completed` — with every tool call's name and argument/output hashes in the output,
the SDK's summed token usage metered as cost, and an `Effect` per class of tool actually
called. The governor's gate ran before this executor was dispatched; the governor's
`_settle` checks the reported effects after. The inner harness owns the loop; AgentOS
owns the ledger and the authority. (Temporal's Agent Harness calls the same shape a
"turn"; the seam it describes — between the model choosing a capability and the
capability executing — is the tool registry below plus the core's declared-effects gate.)

Boundaries, each tested:
- Tools are operator-registered Python (`tools.py`), named by string in agent config. The
  model is offered ONLY tools whose effect class the AgentOS agent declared; the rest are
  withheld (listed in the output) rather than offered-and-refused.
- `needs_approval` interruptions are NOT auto-approved: this slice cannot suspend
  mid-step (that needs the SDK `RunState` persisted as a blob + a resume protocol — see
  ROADMAP ⏭). Steps needing approval are gated at STEP granularity by the existing tier-2
  gate, so a run that comes back with interruptions is a definition error and raises.
- Inputs are delimited data (`wrap_input` / `render_prompt`) and `DATA_BOUNDARY` is in the
  instructions, as in every other provider (C12).
- Zero-config local default: the SDK's chat-completions model against Ollama, no key.
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
from agents import (
    Agent,
    AgentsException,
    MaxTurnsExceeded,
    Model,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunConfig,
    Runner,
    function_tool,
)
from agents.items import ToolCallItem, ToolCallOutputItem

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

from .config import from_env
from .output_schema import KitOutputSchema
from .tools import ToolRegistry, ToolSpec, load_registry

NAME = "openai-agents"
try:
    VERSION = _dist_version("agentos-provider-openai-agents")
except PackageNotFoundError:  # running from a checkout without install
    VERSION = "0.0.0+dev"

DEFAULT_MAX_TURNS = 6
JSON_INSTRUCTION = "Respond with a single JSON object and nothing else."
# A factory returns the SDK Model, or (Model, client) when it created a client the
# executor must close after the run — the default factory does; test factories do not.
ModelFactory = Callable[[str], "Model | tuple[Model, openai.AsyncOpenAI]"]
RunFn = Callable[..., Awaitable[Any]]


class OpenAIAgentsExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: ProviderConfig | None = None, *,
                 registry: ToolRegistry | None = None,
                 model_factory: ModelFactory | None = None,
                 run: RunFn = Runner.run) -> None:
        self.config = config or from_env()
        self.pricing = PricingTable(self.config.pricing_path)
        self.registry = registry if registry is not None else load_registry()
        self._model_factory = model_factory or self._default_model
        self._run = run

    # -- optional hooks ------------------------------------------------------------------

    def resolve(self, req: StepRequest) -> str:
        return self.config.resolve(req.agent.config.get("model", "chat.default"))[0]

    def describe(self) -> dict:
        try:
            sdk = _dist_version("openai-agents")
        except PackageNotFoundError:
            sdk = "unknown"
        return {
            "base_url": self.config.base_url, "wire_format": "openai-agents-sdk",
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

        tools = [function_tool(s.fn, name_override=s.name, description_override=s.description)
                 for s in offered]
        max_turns = int(cfg.get("max_turns", DEFAULT_MAX_TURNS))
        progress(0.0, f"agents-sdk run model={model_id} tools={sorted(by_name)} "
                      f"max_turns={max_turns}" + (" typed_output" if schema else ""))
        result = self._run_sdk(req.agent.name, instructions, tools, cfg, user, max_turns,
                               model_id, schema)

        if result.interruptions:
            names = sorted({i.raw_item.name for i in result.interruptions
                            if hasattr(i.raw_item, "name")})
            raise ProviderError(
                f"SDK run paused for tool approval on {names}; mid-step approval is not "
                f"supported — declare the tool's effect class on the AgentOS agent so the "
                f"STEP is gated by the governor before dispatch, and do not set "
                f"needs_approval on registry tools")

        calls, classes = self._trajectory(result.new_items, by_name)
        usage = result.context_wrapper.usage
        in_tok, out_tok = int(usage.input_tokens), int(usage.output_tokens)
        cost, priced = self.pricing.cost(
            model_id, in_tok, out_tok,
            extra_meters=[Meter(name="model_requests", quantity=int(usage.requests))])
        progress(1.0, f"{usage.requests} model call(s), {len(calls)} tool call(s), "
                      f"{in_tok}+{out_tok} tokens, {cost.amount} {cost.currency}")

        text = result.final_output if isinstance(result.final_output, str) \
            else json.dumps(result.final_output, default=str, ensure_ascii=False)
        output: dict[str, Any] = {
            "text": text, "model": model_id, "turns": int(usage.requests),
            "tool_calls": calls,
            "tools_offered": sorted(by_name), "tools_withheld": sorted(s.name for s in withheld),
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}, "priced": priced,
        }
        if alias:
            output["alias"] = alias
        if schema is not None:
            # The SDK already ran the reply through `KitOutputSchema.validate_json` (a
            # violation surfaced as ModelBehaviorError → BadResponse); final_output IS the
            # validated dict. Validate once more here so the value stored is provably the
            # one the contract accepted, whatever the run function was.
            value = result.final_output if isinstance(result.final_output, dict) \
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
                          description=f"agents-sdk run on {model_id}")]
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
        # `instructions` is the SDK's word; `system` is what the other providers' example
        # agents use — accept both so "change one field" stays true for the quickstart.
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

    def _run_sdk(self, name: str, instructions: str, tools: list, cfg: dict, user: str,
                 max_turns: int, model_id: str, schema: OutputSchema | None = None):
        """One fresh event loop per step (`asyncio.run`): the model client is created,
        used and CLOSED inside it, so a long-lived worker thread accumulates no open
        connection pools (a review found `Runner.run_sync` deliberately leaves the
        thread's loop — and anything bound to it — open after the run).

        With an `output_schema`, the SDK sends it to the model as `response_format` and
        validates the reply through `KitOutputSchema` (the kit's validator), so the
        contract is one implementation on both harnesses."""
        output_type = KitOutputSchema(schema) if schema is not None else None

        async def _arun():
            made = self._model_factory(model_id)
            model, client = made if isinstance(made, tuple) else (made, None)
            try:
                sdk_agent = Agent(
                    name=name, instructions=instructions, model=model, tools=tools,
                    output_type=output_type,
                    model_settings=ModelSettings(temperature=cfg.get("temperature", 0)),
                )
                return await self._run(sdk_agent, user, max_turns=max_turns,
                                       run_config=RunConfig(tracing_disabled=True))
            finally:
                if client is not None:
                    await client.close()

        try:
            return asyncio.run(_arun())
        except MaxTurnsExceeded as exc:
            raise BadResponse(f"agents-sdk run exceeded max_turns={max_turns} on {model_id}: "
                              f"{exc}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderUnreachable(f"{self.config.base_url} unreachable: {exc}. "
                                      f"{self._unreachable_hint()}") from exc
        except openai.AuthenticationError as exc:
            raise AuthenticationFailed(f"{self.config.base_url} rejected the API key "
                                       f"(AGENTOS_OPENAI_AGENTS_API_KEY / OPENAI_API_KEY)") from exc
        except openai.NotFoundError as exc:
            raise ModelNotFound(f"model {model_id!r} not found at {self.config.base_url}: "
                                f"{exc}. Local Ollama: `ollama pull {model_id}`") from exc
        except openai.RateLimitError as exc:
            raise ProviderRateLimited(f"{self.config.base_url} rate-limited the request; "
                                      f"retry policy applies: {exc}") from exc
        except openai.InternalServerError as exc:
            raise ProviderServerError(f"{self.config.base_url} server error: {exc}") from exc
        except AgentsException as exc:
            raise BadResponse(f"agents-sdk run failed on {model_id}: "
                              f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _trajectory(items, by_name: dict[str, ToolSpec]) \
            -> tuple[list[dict[str, Any]], list[tuple[str, EffectClass]]]:
        """Tool calls as (name, arguments hash, output hash) — the trajectory is in the
        log without inlining potentially large tool outputs — plus one (name, class) per
        DISTINCT tool called, for the effects list."""
        calls: list[dict[str, Any]] = []
        outputs: dict[str, str] = {}
        for item in items:
            if isinstance(item, ToolCallOutputItem):
                raw = item.raw_item if isinstance(item.raw_item, dict) else {}
                outputs[str(raw.get("call_id", ""))] = _sha(str(raw.get("output", item.output)))
        for item in items:
            if isinstance(item, ToolCallItem):
                raw = item.raw_item
                name = getattr(raw, "name", None) or "?"
                calls.append({
                    "name": name, "call_id": getattr(raw, "call_id", None),
                    "arguments_sha256": _sha(str(getattr(raw, "arguments", ""))),
                    "output_sha256": outputs.get(str(getattr(raw, "call_id", ""))),
                })
        classes = sorted({(c["name"], by_name[c["name"]].effect_class)
                          for c in calls if c["name"] in by_name})
        return calls, classes

    def _default_model(self, model_id: str) -> tuple[Model, openai.AsyncOpenAI]:
        """Called INSIDE the step's event loop so the client binds to it; returned with the
        model so `_run_sdk` closes it when the run ends."""
        client = openai.AsyncOpenAI(base_url=self.config.base_url,
                                    api_key=self.config.api_key or "ollama",
                                    timeout=self.config.timeout_seconds)
        return OpenAIChatCompletionsModel(model=model_id, openai_client=client), client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def _unreachable_hint(self) -> str:
        if "11434" in self.config.base_url:
            return "Is Ollama running? `ollama serve` (then `ollama pull qwen2.5:0.5b`)."
        return "Check AGENTOS_OPENAI_AGENTS_BASE_URL and that the server is up."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

