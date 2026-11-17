"""The executor: one `POST {base_url}/chat/completions` per step, over plain HTTP.

No vendor SDK on purpose (docs/DEVELOPMENT_STRUCTURE.md §11 A10): the OpenAI
chat-completions wire format is the de facto protocol that Ollama, vLLM, LM Studio,
OpenRouter and OpenAI itself all speak, and a documented JSON shape outlives any client
library. Everything the core needs — output, metered cost, provenance — is derived from
the response body.

Agent `config` keys (all optional):
    model         capability alias ("chat.fast") or concrete id ("gpt-4o-mini");
                  default "chat.default"
    system        system prompt
    prompt        user-message template; `{run.topic}` reads run inputs, `{summarise.text}`
                  reads the upstream step "summarise"'s output field "text". Without a
                  template the inputs are sent as JSON.
    temperature   default 0 — deterministic by default so cassettes replay
    seed          default 42 (honoured by Ollama and OpenAI; ignored by servers without it)
    max_tokens    server default when unset
    json_output   true → ask for a JSON object and parse it into output["json"]
    tools         registered tool names (`agentos.tools` entry point, providerkit/tools.py);
                  see "Function calling" below
    tool_choice   sent as-is with the offered tools ("auto" | "none" | "required" | {...})
    max_tool_rounds  default 4: how many rounds of tool calls the model may ask for

Function calling (audit E-17, the A8 half): the model is offered, as OpenAI `tools`, ONLY
the named tools whose effect class the step declared — the same `ToolRegistry.select` rule
the SDK harnesses apply; the rest are withheld. When a response carries `tool_calls`, each
call is checked again before anything runs: a call to a tool that was not offered
(withheld, or a name nobody registered) is REJECTED, never executed, and the refusal is
sent back as that call's tool result so the model can still finish; arguments are
validated against the function's signature (a failure is rejected the same way and the
model may retry). Executed results go back as `role: tool` messages, delimited with
`wrap_input` like every other input (C12). A model still asking for tools after
`max_tool_rounds` rounds fails the step with `ToolError`. Usage is summed across rounds.
Each call lands in `output.tool_calls` in the SDK harnesses' shape: name, call id, SHA-256
of the arguments and of the output (or of the refusal, with `rejected: true`).
Without `tools` in the config the request and the output are exactly what they were.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import typing
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from dagentos.core.models import (
    Cost,
    Effect,
    EffectClass,
    Meter,
    Provenance,
    StepRequest,
    StepResult,
)
from dagentos.core.ports import ProgressFn
from dagentos.providerkit.cassette import Cassette, CassetteTransport
from dagentos.providerkit.config import ProviderConfig
from dagentos.providerkit.errors import (
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    server_message,
)
from dagentos.providerkit.pricing import PricingTable
from dagentos.providerkit.prompt import DATA_BOUNDARY, render_prompt, wrap_input
from dagentos.providerkit.tools import ToolRegistry, ToolSpec, load_registry

from .config import from_env

NAME = "openai-compat"
try:
    VERSION = _dist_version("agentos-provider-openai-compat")
except PackageNotFoundError:  # running from a source tree without install
    VERSION = "0.0.0+src"

DEFAULT_MAX_TOOL_ROUNDS = 4
# Appended to the system prompt only when tools are offered. Measured live on llama3.2:3b
# (Ollama 0.40.16, temperature 0, seed 42, the quickstart's counter agent over 10 topics):
# without it the model copied the `<input …>` tags into the tool argument on 6 of 10 topics,
# which Ollama cannot parse as a tool call, so the call degraded to plain text and the step
# ended with no tool run (4/10 clean calls). With this sentence: 8/10. The two misses are
# unchanged either way. `scripts/probe_tool_boundary.py` reproduces the measurement.
TOOL_NOTE = ("When you call a tool, pass only the data between the tags as the argument, "
             "never the <input> tags themselves.")


class ToolError(ProviderError):
    """A function-calling round trip could not finish: the round cap was reached, or a
    registered tool raised. The message names which and what to change."""


# ------------------------------------------------------------------ the executor

class OpenAICompatExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: ProviderConfig | None = None, *,
                 transport: httpx.BaseTransport | None = None, cassette_name: str | None = None,
                 registry: ToolRegistry | None = None):
        self.config = config or from_env()
        self.pricing = PricingTable(self.config.pricing_path)
        self.registry = registry if registry is not None else load_registry()
        self._transport = transport
        self._cassette_name = cassette_name or self.config.cassette_name

    # -- hooks the composition root and engine look for (all optional in the port) -----

    def resolve(self, req: StepRequest) -> str:
        """§11 A3: the concrete model this request would run on. Pure table lookup — no
        network — so the engine can record a substitution before dispatch."""
        return self.config.resolve(req.agent.config.get("model", "chat.default"))[0]

    def describe(self) -> dict:
        return {
            "base_url": self.config.base_url,
            "aliases": dict(self.config.aliases),
            "pricing": {"sha256": self.pricing.sha256, "as_of": self.pricing.data.get("as_of"),
                        "path": str(self.pricing.path)},
            "api_key": "set" if self.config.api_key else "unset",
            "cassettes": self.config.cassettes,
            "tools": self.registry.describe(),
        }

    def health(self) -> dict:
        """Can we reach the server, and does it have the models our aliases point at?"""
        try:
            with self._client(timeout=3.0) as c:
                r = c.get("/models")
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        except httpx.ConnectError as exc:
            return {"reachable": False, "error": str(exc),
                    "hint": self._unreachable_hint()}
        except Exception as exc:  # noqa: BLE001 — health is a report, not a raise
            return {"reachable": False, "error": str(exc)}
        return {
            "reachable": True,
            "models": ids[:50],
            "aliases_available": {a: (m in ids) for a, m in self.config.aliases.items()},
        }

    def pricing_snapshot(self) -> bytes:
        return self.pricing.snapshot()

    # -- the port -----------------------------------------------------------------------

    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        cfg = req.agent.config
        model_id, alias = self.config.resolve(cfg.get("model", "chat.default"))
        tool_mode = "tools" in cfg
        offered, withheld = self.registry.select(cfg.get("tools") or [], req.declared_effects)
        by_name = {s.name: s for s in offered}
        arg_models = {s.name: _arguments_model(s) for s in offered}
        messages = self._messages(cfg, req.inputs, tools=bool(offered))
        body: dict[str, Any] = {
            "model": model_id,
            "messages": list(messages),
            "temperature": cfg.get("temperature", 0),
            "seed": cfg.get("seed", 42),
        }
        if cfg.get("max_tokens") is not None:
            body["max_tokens"] = cfg["max_tokens"]
        if cfg.get("json_output"):
            body["response_format"] = {"type": "json_object"}
        if offered:
            body["tools"] = [_function_schema(s, arg_models[s.name]) for s in offered]
            if cfg.get("tool_choice") is not None:
                body["tool_choice"] = cfg["tool_choice"]
        # The offered schemas change what the model is told, so they are part of the hash.
        hashed: Any = {"messages": messages, "tools": body.get("tools"),
                       "tool_choice": body.get("tool_choice")} if tool_mode else messages
        prompt_hash = hashlib.sha256(
            json.dumps(hashed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        max_rounds = max(0, int(cfg.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)))
        calls: list[dict[str, Any]] = []
        in_tok = out_tok = requests = 0
        while True:
            progress(requests / (max_rounds + 2),
                     f"POST {self.config.base_url}/chat/completions model={model_id}"
                     + (f" (tool round {requests})" if requests else ""))
            data = self._post_chat(body, model_id)
            requests += 1
            try:
                choice = data["choices"][0]
                message = choice["message"]
                text = message.get("content") or ""
                finish = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError) as exc:
                raise BadResponse(f"{self.config.base_url} returned a chat completion without "
                                  f"choices[0].message: {str(data)[:200]}") from exc
            usage = data.get("usage") or {}
            in_tok += int(usage.get("prompt_tokens", 0))
            out_tok += int(usage.get("completion_tokens", 0))
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                break
            if not isinstance(tool_calls, list) or not all(isinstance(c, dict) for c in tool_calls):
                raise BadResponse(f"{self.config.base_url} returned malformed tool_calls: "
                                  f"{str(tool_calls)[:200]}")
            if requests > max_rounds:
                raise ToolError(
                    f"the model asked for tool round {requests} but max_tool_rounds={max_rounds} "
                    f"for agent {req.agent.name!r}; raise config.max_tool_rounds or tell the "
                    f"model in config.system when to stop calling tools")
            body["messages"].append({"role": "assistant", "content": message.get("content"),
                                     "tool_calls": tool_calls})
            for i, call in enumerate(tool_calls):
                rec, content = self._call_tool(call, by_name, arg_models, f"call_{requests}_{i}")
                calls.append(rec)
                body["messages"].append({"role": "tool", "tool_call_id": rec["call_id"],
                                         "content": content})

        served_model = data.get("model") or model_id
        extra = [Meter(name="model_requests", quantity=requests)] if tool_mode else None
        cost, priced = self.pricing.cost(served_model, in_tok, out_tok, extra_meters=extra)
        progress(1.0, (f"{len(calls)} tool call(s), " if tool_mode else "")
                 + f"{in_tok}+{out_tok} tokens, {cost.amount} {cost.currency}")

        output: dict[str, Any] = {
            "text": text, "model": served_model, "finish_reason": finish,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}, "priced": priced,
        }
        if tool_mode:
            output.update({"turns": requests, "tool_calls": calls,
                           "tools_offered": sorted(by_name),
                           "tools_withheld": sorted(s.name for s in withheld)})
        if alias:
            output["alias"] = alias
        if cfg.get("json_output"):
            try:
                output["json"] = json.loads(text)
            except ValueError as exc:
                raise BadResponse(f"json_output requested but {served_model} returned "
                                  f"non-JSON: {text[:120]!r}") from exc
        effects = [Effect(effect_class=EffectClass.compute,
                          description=f"chat completion on {served_model}")]
        # One effect per DISTINCT offered tool the model called (run, or refused for bad
        # arguments — reporting the attempt is the conservative direction), as the SDK
        # harnesses report. A call to a tool that was not offered never ran: no effect.
        effects += [Effect(effect_class=ec, description=f"tool {n}") for n, ec in sorted(
            {(c["name"], by_name[c["name"]].effect_class) for c in calls if c["name"] in by_name})]
        return StepResult(
            output=output,
            effects=effects,
            cost=cost if isinstance(cost, Cost) else Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=served_model, model_alias=alias,
                                  prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------

    def _call_tool(self, call: dict, by_name: dict[str, ToolSpec],
                   arg_models: dict[str, type[BaseModel]], fallback_id: str) \
            -> tuple[dict[str, Any], str]:
        """(record, content of the `role: tool` reply) for one call the model made. Nothing
        runs unless the tool was offered AND the arguments fit its signature; otherwise the
        record says `rejected: true` and the model is told why, so it can still answer."""
        fn = call.get("function") or {}
        name = str(fn.get("name") or "?")
        raw = fn.get("arguments")
        args_text = raw if isinstance(raw, str) else json.dumps(raw or {}, sort_keys=True)
        rec: dict[str, Any] = {"name": name, "call_id": call.get("id") or fallback_id,
                               "arguments_sha256": _sha(args_text), "output_sha256": None}
        spec = by_name.get(name)
        if spec is None:
            if name in self.registry.names():
                why = (f"its effect class {self.registry.get(name).effect_class.value!r} is not "
                       f"declared by this step")
            else:
                why = "it is not offered to this step"
            return _rejected(rec, f"tool {name!r} was not run: {why}. Offered tools: "
                                  f"[{', '.join(sorted(by_name)) or 'none'}]. Answer without it.")
        try:
            args = json.loads(args_text or "{}")
            if not isinstance(args, dict):
                raise ValueError(f"arguments must be a JSON object, got {type(args).__name__}")
            kwargs = dict(arg_models[name].model_validate(args))
        except ValidationError as exc:
            why = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                            for e in exc.errors())
            return _rejected(rec, f"tool {name!r} was not run: invalid arguments ({why}).")
        except ValueError as exc:
            return _rejected(rec, f"tool {name!r} was not run: invalid arguments ({exc}).")
        try:
            result = spec.fn(**kwargs)
        except Exception as exc:
            raise ToolError(f"tool {name!r} raised {type(exc).__name__}: {exc}; fix the tool "
                            f"or its input (the step's retry policy applies)") from exc
        out = result if isinstance(result, str) \
            else json.dumps(result, default=str, ensure_ascii=False)
        rec["output_sha256"] = _sha(out)
        return rec, wrap_input(f"tool.{name}", out)        # C12: a tool result is data

    def _messages(self, cfg: dict, inputs: dict, *, tools: bool = False) -> list[dict]:
        # C12: the agent definition is the only source of instructions; everything
        # interpolated from inputs is delimited data and the system prompt says so. With
        # tools offered, TOOL_NOTE follows (see its comment for the live measurement).
        system = f"{cfg['system']}\n\n{DATA_BOUNDARY}" if cfg.get("system") else DATA_BOUNDARY
        if tools:
            system = f"{system} {TOOL_NOTE}"
        msgs = [{"role": "system", "content": system}]
        if cfg.get("prompt"):
            user = render_prompt(str(cfg["prompt"]), inputs)
        elif inputs:
            user = wrap_input("inputs", json.dumps(inputs, sort_keys=True, ensure_ascii=False))
        else:
            user = "Hello."
        msgs.append({"role": "user", "content": user})
        return msgs

    def _client(self, timeout: float | None = None) -> httpx.Client:
        headers = {"content-type": "application/json", "user-agent": f"agentos-{NAME}/{VERSION}"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        transport = self._transport
        if self.config.cassettes != "off":
            cassette = Cassette(self.config.cassette_dir / f"{self._cassette_name}.json",
                                source=self.config.base_url)
            transport = CassetteTransport(cassette, self.config.cassettes, inner=transport,
                                          record_var="AGENTOS_OPENAI_CASSETTES")
        return httpx.Client(base_url=self.config.base_url, headers=headers,
                            timeout=timeout or self.config.timeout_seconds, transport=transport)

    def _post_chat(self, body: dict, model_id: str) -> dict:
        try:
            with self._client() as c:
                r = c.post("/chat/completions", json=body)
        except httpx.ConnectError as exc:
            raise ProviderUnreachable(
                f"cannot reach {self.config.base_url} ({exc}). {self._unreachable_hint()}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderServerError(
                f"{self.config.base_url} did not answer within {self.config.timeout_seconds}s "
                f"for model {model_id!r}; raise AGENTOS_OPENAI_TIMEOUT or pick a smaller model "
                f"(the step's retry policy applies)") from exc
        if r.status_code == 401 or r.status_code == 403:
            raise AuthenticationFailed(
                f"{self.config.base_url} rejected the credentials (HTTP {r.status_code}). "
                f"Set AGENTOS_OPENAI_API_KEY (or OPENAI_API_KEY); key is currently "
                f"{'set' if self.config.api_key else 'unset'}. {server_message(r)}")
        if r.status_code == 404:
            raise ModelNotFound(
                f"{self.config.base_url} has no model {model_id!r}. If this is Ollama: "
                f"`ollama pull {model_id}`; otherwise fix the alias in AGENTOS_OPENAI_ALIASES "
                f"or the agent's config.model. {server_message(r)}")
        if r.status_code == 429:
            raise ProviderRateLimited(
                f"{self.config.base_url} rate-limited the request (HTTP 429); the step's retry "
                f"policy applies. {server_message(r)}")
        if r.status_code >= 500:
            raise ProviderServerError(
                f"{self.config.base_url} failed (HTTP {r.status_code}); the step's retry "
                f"policy applies. {server_message(r)}")
        if r.status_code >= 400:
            raise BadResponse(f"{self.config.base_url} rejected the request (HTTP "
                              f"{r.status_code}). {server_message(r)}")
        try:
            return r.json()
        except ValueError as exc:
            raise BadResponse(f"{self.config.base_url} returned non-JSON: "
                              f"{r.text[:200]!r}") from exc

    def _unreachable_hint(self) -> str:
        if "11434" in self.config.base_url:
            return ("Is Ollama running? Start it with `ollama serve` (install: "
                    "https://ollama.com/download), or point AGENTOS_OPENAI_BASE_URL at another "
                    "OpenAI-compatible server.")
        return "Check AGENTOS_OPENAI_BASE_URL (include the /v1 suffix) and that the server is up."


def _arguments_model(spec: ToolSpec) -> type[BaseModel]:
    """The tool's signature as a pydantic model: the JSON Schema the model is shown and the
    validator its arguments must pass before the function runs. Unknown keys are refused
    (a small model inventing an argument gets told, not silently ignored)."""
    hints = typing.get_type_hints(spec.fn)
    fields: dict[str, Any] = {}
    for p in inspect.signature(spec.fn).parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise ToolError(f"tool {spec.name!r} takes *args/**kwargs, which cannot be described "
                            f"to the model; give it named, type-annotated parameters")
        fields[p.name] = (hints.get(p.name, Any), ... if p.default is p.empty else p.default)
    return create_model(f"{spec.name}_arguments", __config__=ConfigDict(extra="forbid"),
                        **fields)


def _function_schema(spec: ToolSpec, model: type[BaseModel]) -> dict[str, Any]:
    params = model.model_json_schema()
    params.pop("title", None)
    return {"type": "function", "function": {
        "name": spec.name,
        "description": spec.description or inspect.getdoc(spec.fn) or spec.name,
        "parameters": params}}


def _rejected(rec: dict[str, Any], reason: str) -> tuple[dict[str, Any], str]:
    rec["rejected"] = True
    rec["output_sha256"] = _sha(reason)
    return rec, reason


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
