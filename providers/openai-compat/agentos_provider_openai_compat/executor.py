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
"""
from __future__ import annotations

import hashlib
import json
import string
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

import httpx

from agentos.core.models import Cost, Effect, EffectClass, Provenance, StepRequest, StepResult
from agentos.core.ports import ProgressFn

from .cassette import Cassette, CassetteTransport
from .config import ProviderConfig
from .pricing import PricingTable

NAME = "openai-compat"
try:
    VERSION = _dist_version("agentos-provider-openai-compat")
except PackageNotFoundError:  # running from a source tree without install
    VERSION = "0.0.0+src"


# ------------------------------------------------------------------ errors that explain

class ProviderError(RuntimeError):
    """Base: the message always says what happened AND what to do about it."""


class ProviderUnreachable(ProviderError):
    pass


class AuthenticationFailed(ProviderError):
    pass


class ModelNotFound(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class BadResponse(ProviderError):
    pass


class TemplateError(ProviderError):
    pass


# ------------------------------------------------------------------ prompt templates

class _Dotted(string.Formatter):
    """`{run.topic}` / `{step.text}` lookups over the nested inputs map."""

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        obj: Any = kwargs
        for part in field_name.split("."):
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                raise TemplateError(
                    f"prompt template references {{{field_name}}} but the step inputs have "
                    f"no such field; available top-level keys: "
                    f"{sorted(kwargs) or 'none (add depends_on or pass run inputs)'}")
        return obj, field_name


def render_prompt(template: str, inputs: dict) -> str:
    return _Dotted().vformat(template, (), inputs)


# ------------------------------------------------------------------ the executor

class OpenAICompatExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: ProviderConfig | None = None, *,
                 transport: httpx.BaseTransport | None = None, cassette_name: str | None = None):
        self.config = config or ProviderConfig.from_env()
        self.pricing = PricingTable(self.config.pricing_path)
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
        messages = self._messages(cfg, req.inputs)
        body: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": cfg.get("temperature", 0),
            "seed": cfg.get("seed", 42),
        }
        if cfg.get("max_tokens") is not None:
            body["max_tokens"] = cfg["max_tokens"]
        if cfg.get("json_output"):
            body["response_format"] = {"type": "json_object"}
        prompt_hash = hashlib.sha256(
            json.dumps(messages, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        progress(0.0, f"POST {self.config.base_url}/chat/completions model={model_id}")
        data = self._post_chat(body, model_id)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise BadResponse(f"{self.config.base_url} returned a chat completion without "
                              f"choices[0].message: {str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        served_model = data.get("model") or model_id
        cost, priced = self.pricing.cost(served_model, in_tok, out_tok)
        progress(1.0, f"{in_tok}+{out_tok} tokens, {cost.amount} {cost.currency}")

        output: dict[str, Any] = {
            "text": text, "model": served_model, "finish_reason": finish,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}, "priced": priced,
        }
        if alias:
            output["alias"] = alias
        if cfg.get("json_output"):
            try:
                output["json"] = json.loads(text)
            except ValueError as exc:
                raise BadResponse(f"json_output requested but {served_model} returned "
                                  f"non-JSON: {text[:120]!r}") from exc
        return StepResult(
            output=output,
            effects=[Effect(effect_class=EffectClass.compute,
                            description=f"chat completion on {served_model}")],
            cost=cost if isinstance(cost, Cost) else Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=served_model, model_alias=alias,
                                  prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------

    def _messages(self, cfg: dict, inputs: dict) -> list[dict]:
        msgs = []
        if cfg.get("system"):
            msgs.append({"role": "system", "content": str(cfg["system"])})
        if cfg.get("prompt"):
            user = render_prompt(str(cfg["prompt"]), inputs)
        else:
            user = json.dumps(inputs, sort_keys=True, ensure_ascii=False) if inputs else \
                "Hello."
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
            transport = CassetteTransport(cassette, self.config.cassettes, inner=transport)
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
                f"{'set' if self.config.api_key else 'unset'}. {_server_message(r)}")
        if r.status_code == 404:
            raise ModelNotFound(
                f"{self.config.base_url} has no model {model_id!r}. If this is Ollama: "
                f"`ollama pull {model_id}`; otherwise fix the alias in AGENTOS_OPENAI_ALIASES "
                f"or the agent's config.model. {_server_message(r)}")
        if r.status_code == 429:
            raise ProviderRateLimited(
                f"{self.config.base_url} rate-limited the request (HTTP 429); the step's retry "
                f"policy applies. {_server_message(r)}")
        if r.status_code >= 500:
            raise ProviderServerError(
                f"{self.config.base_url} failed (HTTP {r.status_code}); the step's retry "
                f"policy applies. {_server_message(r)}")
        if r.status_code >= 400:
            raise BadResponse(f"{self.config.base_url} rejected the request (HTTP "
                              f"{r.status_code}). {_server_message(r)}")
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


def _server_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return f"Server said: {msg}" if msg else ""
    except ValueError:
        return f"Server said: {r.text[:160]!r}" if r.text else ""
