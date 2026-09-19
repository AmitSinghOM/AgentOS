"""Configuration for the PydanticAI inner-harness provider. Every knob is an environment
variable with a documented default (`agentos.providerkit.config`); the zero-config default
drives PydanticAI's OpenAI-compatible chat model against a local Ollama.

    AGENTOS_PYDANTIC_AI_BASE_URL   http://127.0.0.1:11434/v1   any /v1 chat-completions server
    AGENTOS_PYDANTIC_AI_API_KEY    (unset)                      falls back to OPENAI_API_KEY;
                                                                Ollama needs none
    AGENTOS_PYDANTIC_AI_ALIASES    {"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}
    AGENTOS_PYDANTIC_AI_PRICING    (bundled pricing.json)
    AGENTOS_PYDANTIC_AI_TIMEOUT    60

PydanticAI's own `OpenAIProvider` would read `OPENAI_BASE_URL` / `OPENAI_API_KEY` from the
process environment if it built the client; this provider always builds the client itself
from the values above, so the config here is the only source of truth (tested).
"""
from __future__ import annotations

from pathlib import Path

from agentos.providerkit.config import ConfigError, ProviderConfig, config_from_env

__all__ = ["BUNDLED_PRICING", "DEFAULT_ALIASES", "DEFAULT_BASE_URL", "ConfigError",
           "ProviderConfig", "from_env"]

PREFIX = "AGENTOS_PYDANTIC_AI"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_ALIASES: dict[str, str] = {
    "chat.fast": "qwen2.5:0.5b",
    "chat.default": "llama3.2:3b",
}
BUNDLED_PRICING = Path(__file__).with_name("pricing.json")


def from_env(env: dict[str, str] | None = None) -> ProviderConfig:
    return config_from_env(PREFIX, default_base_url=DEFAULT_BASE_URL,
                           default_aliases=DEFAULT_ALIASES, bundled_pricing=BUNDLED_PRICING,
                           key_fallbacks=("OPENAI_API_KEY",), env=env)
