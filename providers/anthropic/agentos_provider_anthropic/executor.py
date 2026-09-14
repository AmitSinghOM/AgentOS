"""The executor: one `POST {base_url}/v1/messages` per step, over plain HTTP.

The Anthropic Messages API is the second wire format AgentOS speaks, and the point of this
package is to prove the plugin seam: a different request/response shape, different error
envelope, different auth header — and zero changes to the core or to the OpenAI provider.
Ollama serves this format too, so the zero-config local default still needs no key.

Agent `config` keys (all optional) — the same vocabulary as the OpenAI provider, so an
agent moves between them by changing `executor`:
    model         capability alias ("chat.fast") or concrete id ("claude-3-5-haiku-latest");
                  default "chat.default"
    system        system prompt (sent as the top-level `system` field)
    prompt        user-message template; `{run.topic}` / `{step.field}`; default: inputs as JSON
    temperature   default 0
    max_tokens    default 1024 (the Messages API requires it)
    json_output   true → instruct for a single JSON object and parse into output["json"]
                  (the Messages API has no response_format; the instruction is the contract)
"""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

import httpx

from agentos.core.models import Effect, EffectClass, Meter, Provenance, StepRequest, StepResult
from agentos.core.ports import ProgressFn
from agentos.providerkit.cassette import Cassette, CassetteTransport
from agentos.providerkit.config import ProviderConfig
from agentos.providerkit.errors import (
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    server_message,
)
from agentos.providerkit.pricing import PricingTable
from agentos.providerkit.prompt import DATA_BOUNDARY, render_prompt, wrap_input

from .config import api_version, from_env

NAME = "anthropic"
try:
    VERSION = _dist_version("agentos-provider-anthropic")
except PackageNotFoundError:
    VERSION = "0.0.0+src"

JSON_INSTRUCTION = "Respond with a single JSON object and nothing else."


class AnthropicExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: ProviderConfig | None = None, *, version_header: str | None = None,
                 transport: httpx.BaseTransport | None = None, cassette_name: str | None = None):
        self.config = config or from_env()
        self.version_header = version_header or api_version()
        self.pricing = PricingTable(self.config.pricing_path)
        self._transport = transport
        self._cassette_name = cassette_name or self.config.cassette_name

    # -- optional hooks ------------------------------------------------------------------

    def resolve(self, req: StepRequest) -> str:
        return self.config.resolve(req.agent.config.get("model", "chat.default"))[0]

    def describe(self) -> dict:
        return {
            "base_url": self.config.base_url, "wire_format": "anthropic-messages",
            "anthropic_version": self.version_header, "aliases": dict(self.config.aliases),
            "pricing": {"sha256": self.pricing.sha256, "as_of": self.pricing.data.get("as_of"),
                        "path": str(self.pricing.path)},
            "api_key": "set" if self.config.api_key else "unset",
            "cassettes": self.config.cassettes,
        }

    def health(self) -> dict:
        try:
            with self._client(timeout=3.0) as c:
                r = c.get("/v1/models")
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
        system, messages = self._messages(cfg, req.inputs)
        body: dict[str, Any] = {
            "model": model_id,
            "max_tokens": int(cfg.get("max_tokens", 1024)),
            "messages": messages,
            "temperature": cfg.get("temperature", 0),
        }
        if system:
            body["system"] = system
        prompt_hash = hashlib.sha256(json.dumps(
            {"system": system, "messages": messages}, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

        progress(0.0, f"POST {self.config.base_url}/v1/messages model={model_id}")
        data = self._post(body, model_id)
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            stop = data.get("stop_reason")
        except (KeyError, TypeError) as exc:
            raise BadResponse(f"{self.config.base_url} returned a message without a content "
                              f"list: {str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        # Anthropic (and Ollama's implementation) report cache reads/writes separately from
        # `input_tokens`. The meter counts every prompt token at the full input rate —
        # conservative for Anthropic's 10 % cache-read price — and records the cached
        # portion as its own meter so the discount is recoverable from the log.
        cached = int(usage.get("cache_read_input_tokens", 0) or 0) + \
            int(usage.get("cache_creation_input_tokens", 0) or 0)
        in_tok = int(usage.get("input_tokens", 0)) + cached
        out_tok = int(usage.get("output_tokens", 0))
        served_model = data.get("model") or model_id
        cost, priced = self.pricing.cost(
            served_model, in_tok, out_tok,
            extra_meters=[Meter(name="cached_input_tokens", quantity=cached)] if cached else None)
        progress(1.0, f"{in_tok}+{out_tok} tokens, {cost.amount} {cost.currency}")

        output: dict[str, Any] = {
            "text": text, "model": served_model, "finish_reason": stop,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}, "priced": priced,
        }
        if alias:
            output["alias"] = alias
        if cfg.get("json_output"):
            try:
                output["json"] = json.loads(_strip_fences(text))
            except ValueError as exc:
                raise BadResponse(f"json_output requested but {served_model} returned "
                                  f"non-JSON: {text[:120]!r}") from exc
        return StepResult(
            output=output,
            effects=[Effect(effect_class=EffectClass.compute,
                            description=f"message on {served_model}")],
            cost=cost,
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=served_model, model_alias=alias,
                                  prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------

    def _messages(self, cfg: dict, inputs: dict) -> tuple[str | None, list[dict]]:
        # C12: the agent definition is the only source of instructions; everything
        # interpolated from inputs is delimited data and the system prompt says so.
        parts = [str(cfg["system"])] if cfg.get("system") else []
        if cfg.get("json_output"):
            parts.append(JSON_INSTRUCTION)
        parts.append(DATA_BOUNDARY)
        system = "\n\n".join(parts)
        if cfg.get("prompt"):
            user = render_prompt(str(cfg["prompt"]), inputs)
        elif inputs:
            user = wrap_input("inputs", json.dumps(inputs, sort_keys=True, ensure_ascii=False))
        else:
            user = "Hello."
        return system, [{"role": "user", "content": user}]

    def _client(self, timeout: float | None = None) -> httpx.Client:
        headers = {"content-type": "application/json",
                   "anthropic-version": self.version_header,
                   "user-agent": f"agentos-{NAME}/{VERSION}"}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        transport = self._transport
        if self.config.cassettes != "off":
            cassette = Cassette(self.config.cassette_dir / f"{self._cassette_name}.json",
                                source=self.config.base_url)
            transport = CassetteTransport(cassette, self.config.cassettes, inner=transport,
                                          record_var="AGENTOS_ANTHROPIC_CASSETTES")
        return httpx.Client(base_url=self.config.base_url, headers=headers,
                            timeout=timeout or self.config.timeout_seconds, transport=transport)

    def _post(self, body: dict, model_id: str) -> dict:
        try:
            with self._client() as c:
                r = c.post("/v1/messages", json=body)
        except httpx.ConnectError as exc:
            raise ProviderUnreachable(
                f"cannot reach {self.config.base_url} ({exc}). {self._unreachable_hint()}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderServerError(
                f"{self.config.base_url} did not answer within {self.config.timeout_seconds}s "
                f"for model {model_id!r}; raise AGENTOS_ANTHROPIC_TIMEOUT or pick a smaller "
                f"model (the step's retry policy applies)") from exc
        if r.status_code in (401, 403):
            raise AuthenticationFailed(
                f"{self.config.base_url} rejected the credentials (HTTP {r.status_code}). Set "
                f"AGENTOS_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY); key is currently "
                f"{'set' if self.config.api_key else 'unset'}. {server_message(r)}")
        if r.status_code == 404:
            raise ModelNotFound(
                f"{self.config.base_url} has no model {model_id!r}. If this is Ollama: "
                f"`ollama pull {model_id}`; otherwise fix the alias in AGENTOS_ANTHROPIC_ALIASES "
                f"or the agent's config.model. {server_message(r)}")
        if r.status_code == 429:
            raise ProviderRateLimited(
                f"{self.config.base_url} rate-limited the request (HTTP 429); the step's retry "
                f"policy applies. {server_message(r)}")
        if r.status_code >= 500:      # includes Anthropic's 529 overloaded_error
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
                    "https://ollama.com/download), or set AGENTOS_ANTHROPIC_BASE_URL to "
                    "https://api.anthropic.com with ANTHROPIC_API_KEY.")
        return ("Check AGENTOS_ANTHROPIC_BASE_URL (no /v1 suffix — the path is /v1/messages) "
                "and that the server is up.")


def _strip_fences(text: str) -> str:
    """Small models wrap JSON in ```json fences even when told not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
