"""agentos-provider-openai-compat — AgentOS executor for any OpenAI-compatible server.

Zero-config default: a local Ollama at http://127.0.0.1:11434/v1, no API key. See
`config.py` for every environment variable and `executor.py` for the agent config keys.
"""
from .config import ConfigError, ProviderConfig
from .executor import (
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    OpenAICompatExecutor,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    TemplateError,
)

__all__ = [
    "AuthenticationFailed", "BadResponse", "ConfigError", "ModelNotFound",
    "OpenAICompatExecutor", "ProviderConfig", "ProviderError", "ProviderRateLimited",
    "ProviderServerError", "ProviderUnreachable", "TemplateError", "load",
]


def load() -> OpenAICompatExecutor:
    """Entry-point target for the `agentos.executors` group: configured from the
    environment. A bad variable raises ConfigError naming it; the API/worker log that
    and skip this plugin rather than refusing to start."""
    return OpenAICompatExecutor(ProviderConfig.from_env())
