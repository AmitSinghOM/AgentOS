"""Configuration for the Anthropic Messages provider (see `agentos.providerkit.config`).
Zero-config default: a local Ollama, which serves the same wire format at /v1/messages.

    AGENTOS_ANTHROPIC_BASE_URL   http://127.0.0.1:11434     NO /v1 suffix: the API path is
                                                            /v1/messages on every server
                                                            (Anthropic: https://api.anthropic.com)
    AGENTOS_ANTHROPIC_API_KEY    (unset)                    falls back to ANTHROPIC_API_KEY;
                                                            Ollama needs none
    AGENTOS_ANTHROPIC_VERSION    2023-06-01                 the `anthropic-version` header
    AGENTOS_ANTHROPIC_ALIASES    {"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}
    AGENTOS_ANTHROPIC_PRICING / _TIMEOUT / _CASSETTES / _CASSETTE_DIR / _CASSETTE
"""
from __future__ import annotations

import os
from pathlib import Path

from agentos.providerkit.config import ConfigError, ProviderConfig, config_from_env

__all__ = ["BUNDLED_PRICING", "DEFAULT_ALIASES", "DEFAULT_BASE_URL", "ConfigError",
           "ProviderConfig", "api_version", "from_env"]

PREFIX = "AGENTOS_ANTHROPIC"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_ALIASES: dict[str, str] = {
    "chat.fast": "qwen2.5:0.5b",
    "chat.default": "llama3.2:3b",
}
DEFAULT_VERSION = "2023-06-01"
BUNDLED_PRICING = Path(__file__).with_name("pricing.json")


def from_env(env: dict[str, str] | None = None) -> ProviderConfig:
    return config_from_env(PREFIX, default_base_url=DEFAULT_BASE_URL,
                           default_aliases=DEFAULT_ALIASES, bundled_pricing=BUNDLED_PRICING,
                           key_fallbacks=("ANTHROPIC_API_KEY",), env=env)


def api_version(env: dict[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    return e.get(f"{PREFIX}_VERSION", DEFAULT_VERSION)
