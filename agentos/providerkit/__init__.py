"""agentos.providerkit — the parts every provider plugin needs, kept once.

Not part of `agentos.core` (the import contracts forbid the core from touching this) and
not a provider either: a small toolkit the `agentos-provider-*` distributions build on so
that a second wire format costs one executor module, not a second copy of cassettes,
pricing, error vocabulary and prompt templating. Requires the `agentos[providerkit]`
extra (httpx).

    cassette   record/replay HTTP transport (§11 A10)
    pricing    content-addressed pricing table → metered Cost (§11 A6)
    errors     the shared error vocabulary — every message says what to do
    prompt     `{run.topic}` / `{step.field}` templates over step inputs
    config     env → ProviderConfig with a per-provider prefix
"""
from agentos.providerkit.config import ConfigError, ProviderConfig, config_from_env
from agentos.providerkit.errors import (
    AuthenticationFailed,
    BadResponse,
    ModelNotFound,
    ProviderError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderUnreachable,
    TemplateError,
    server_message,
)
from agentos.providerkit.prompt import render_prompt

__all__ = [
    "AuthenticationFailed", "BadResponse", "ConfigError", "ModelNotFound", "ProviderConfig",
    "ProviderError", "ProviderRateLimited", "ProviderServerError", "ProviderUnreachable",
    "TemplateError", "config_from_env", "render_prompt", "server_message",
]
