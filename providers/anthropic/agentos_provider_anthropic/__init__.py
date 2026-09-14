"""agentos-provider-anthropic — AgentOS executor for the Anthropic Messages wire format.

Zero-config default: a local Ollama at http://127.0.0.1:11434 (it serves /v1/messages),
no API key. Point AGENTOS_ANTHROPIC_BASE_URL at https://api.anthropic.com with
ANTHROPIC_API_KEY for Anthropic's models.
"""
from agentos.providerkit import (
    AuthenticationFailed,
    BadResponse,
    ConfigError,
    ModelNotFound,
    ProviderConfig,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    TemplateError,
)

from .config import from_env
from .executor import AnthropicExecutor

__all__ = [
    "AnthropicExecutor", "AuthenticationFailed", "BadResponse", "ConfigError",
    "ModelNotFound", "ProviderConfig", "ProviderError", "ProviderRateLimited",
    "ProviderServerError", "ProviderUnreachable", "TemplateError", "from_env", "load",
]


def load() -> AnthropicExecutor:
    """Entry-point target for the `agentos.executors` group."""
    return AnthropicExecutor(from_env())
