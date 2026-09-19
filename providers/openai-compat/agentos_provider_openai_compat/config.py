"""Configuration for the OpenAI-compatible provider. Every knob is an environment
variable with a documented default (see `dagentos.providerkit.config`); the zero-config
default talks to a local Ollama.

    AGENTOS_OPENAI_BASE_URL   http://127.0.0.1:11434/v1   any /v1 chat-completions server
    AGENTOS_OPENAI_API_KEY    (unset)                      falls back to OPENAI_API_KEY;
                                                           Ollama needs none
    AGENTOS_OPENAI_ALIASES    {"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}
    AGENTOS_OPENAI_PRICING    (bundled pricing.json)
    AGENTOS_OPENAI_TIMEOUT    60
    AGENTOS_OPENAI_CASSETTES  off | replay | record
    AGENTOS_OPENAI_CASSETTE_DIR / AGENTOS_OPENAI_CASSETTE
"""
from __future__ import annotations

from pathlib import Path

from dagentos.providerkit.config import ConfigError, ProviderConfig, config_from_env

__all__ = ["BUNDLED_PRICING", "DEFAULT_ALIASES", "DEFAULT_BASE_URL", "ConfigError",
           "ProviderConfig", "from_env"]

PREFIX = "AGENTOS_OPENAI"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_ALIASES: dict[str, str] = {
    "chat.fast": "qwen2.5:0.5b",      # ~400 MB, answers in a second on a laptop
    "chat.default": "llama3.2:3b",
}
BUNDLED_PRICING = Path(__file__).with_name("pricing.json")


def from_env(env: dict[str, str] | None = None) -> ProviderConfig:
    return config_from_env(PREFIX, default_base_url=DEFAULT_BASE_URL,
                           default_aliases=DEFAULT_ALIASES, bundled_pricing=BUNDLED_PRICING,
                           key_fallbacks=("OPENAI_API_KEY",), env=env)


def default() -> ProviderConfig:
    return from_env({})
