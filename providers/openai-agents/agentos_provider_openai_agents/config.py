"""Configuration for the OpenAI Agents SDK inner-harness provider. Every knob is an
environment variable with a documented default (`dagentos.providerkit.config`); the
zero-config default drives the SDK against a local Ollama's OpenAI-compatible endpoint.

    AGENTOS_OPENAI_AGENTS_BASE_URL   http://127.0.0.1:11434/v1   any /v1 chat-completions server
    AGENTOS_OPENAI_AGENTS_API_KEY    (unset)                      falls back to OPENAI_API_KEY;
                                                                  Ollama needs none
    AGENTOS_OPENAI_AGENTS_ALIASES    {"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}
    AGENTOS_OPENAI_AGENTS_PRICING    (bundled pricing.json)
    AGENTOS_OPENAI_AGENTS_TIMEOUT    60
"""
from __future__ import annotations

from pathlib import Path

from dagentos.providerkit.config import ConfigError, ProviderConfig, config_from_env

__all__ = ["BUNDLED_PRICING", "DEFAULT_ALIASES", "DEFAULT_BASE_URL", "ConfigError",
           "ProviderConfig", "from_env"]

PREFIX = "AGENTOS_OPENAI_AGENTS"
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
