"""Typed configuration for the OpenAI-compatible provider. Every knob is an environment
variable with a documented default; the zero-config default talks to a local Ollama.

    AGENTOS_OPENAI_BASE_URL   http://127.0.0.1:11434/v1   any /v1 chat-completions server
    AGENTOS_OPENAI_API_KEY    (unset)                      falls back to OPENAI_API_KEY;
                                                           Ollama needs none
    AGENTOS_OPENAI_ALIASES    {"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}
                                                           capability alias → model id (JSON)
    AGENTOS_OPENAI_PRICING    (bundled pricing.json)       path to a pricing table
    AGENTOS_OPENAI_TIMEOUT    60                           seconds per request
    AGENTOS_OPENAI_CASSETTES  off | replay | record        HTTP record/replay (§11 A10)
    AGENTOS_OPENAI_CASSETTE_DIR  ./cassettes               where cassettes live
    AGENTOS_OPENAI_CASSETTE   default                      which cassette file (name.json)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_ALIASES: dict[str, str] = {
    "chat.fast": "qwen2.5:0.5b",      # ~400 MB, answers in a second on a laptop
    "chat.default": "llama3.2:3b",
}
BUNDLED_PRICING = Path(__file__).with_name("pricing.json")


class ConfigError(ValueError):
    """Bad provider configuration. Raised at load time, naming the variable to fix."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    pricing_path: Path = BUNDLED_PRICING
    timeout_seconds: float = 60.0
    cassettes: str = "off"                       # off | replay | record
    cassette_dir: Path = Path("cassettes")
    cassette_name: str = "default"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProviderConfig:
        e = os.environ if env is None else env
        aliases = dict(DEFAULT_ALIASES)
        raw = e.get("AGENTOS_OPENAI_ALIASES")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"AGENTOS_OPENAI_ALIASES is not valid JSON ({exc.msg} at column "
                    f"{exc.colno}); expected e.g. "
                    f'{{"chat.fast": "qwen2.5:0.5b"}}') from exc
            if not isinstance(parsed, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
                raise ConfigError("AGENTOS_OPENAI_ALIASES must be a JSON object of "
                                  "string alias → string model id")
            aliases.update(parsed)
        mode = e.get("AGENTOS_OPENAI_CASSETTES", "off").lower()
        if mode not in ("off", "replay", "record"):
            raise ConfigError(f"AGENTOS_OPENAI_CASSETTES={mode!r}; expected off | replay | record")
        pricing = Path(e["AGENTOS_OPENAI_PRICING"]) if e.get("AGENTOS_OPENAI_PRICING") \
            else BUNDLED_PRICING
        if not pricing.exists():
            raise ConfigError(f"AGENTOS_OPENAI_PRICING points at {pricing}, which does not exist")
        try:
            timeout = float(e.get("AGENTOS_OPENAI_TIMEOUT", "60"))
        except ValueError as exc:
            raise ConfigError("AGENTOS_OPENAI_TIMEOUT must be a number of seconds") from exc
        return cls(
            base_url=e.get("AGENTOS_OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=e.get("AGENTOS_OPENAI_API_KEY") or e.get("OPENAI_API_KEY") or None,
            aliases=aliases,
            pricing_path=pricing,
            timeout_seconds=timeout,
            cassettes=mode,
            cassette_dir=Path(e.get("AGENTOS_OPENAI_CASSETTE_DIR", "cassettes")),
            cassette_name=e.get("AGENTOS_OPENAI_CASSETTE", "default"),
        )

    def resolve(self, model: str) -> tuple[str, str | None]:
        """(concrete model id, alias used or None). Anything not in the alias table is
        taken as a concrete id, so `"gpt-4o-mini"` and `"chat.fast"` both work."""
        if model in self.aliases:
            return self.aliases[model], model
        return model, None
