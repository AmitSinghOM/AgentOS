"""dagentos.providerkit — the parts every provider plugin needs, kept once.

Not part of `dagentos.core` (the import contracts forbid the core from touching this) and
not a provider either: a small toolkit the `agentos-provider-*` distributions build on so
that a second wire format costs one executor module, not a second copy of cassettes,
pricing, error vocabulary and prompt templating. Requires the `agentos[providerkit]`
extra (httpx).

    cassette   record/replay HTTP transport (§11 A10)
    pricing    content-addressed pricing table → metered Cost (§11 A6)
    errors     the shared error vocabulary — every message says what to do
    prompt     `{run.topic}` / `{step.field}` templates over step inputs
    config     env → ProviderConfig with a per-provider prefix
    tools      the operator tool registry every inner harness offers from (`agentos.tools`)
    schema     typed structured output: `config.output_schema` validated once for every harness
               (imported lazily — it needs `jsonschema`, and `dagentos.agents.tool` imports
               `providerkit.prompt` on a bare `agentos` install; a worker without the
               providerkit extra must still start — caught by the chaos kill-9 test in CI)
"""
from dagentos.providerkit.config import ConfigError, ProviderConfig, config_from_env
from dagentos.providerkit.errors import (
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
from dagentos.providerkit.prompt import DATA_BOUNDARY, render_prompt, strip_fences, wrap_input
from dagentos.providerkit.tools import (
    BUILTIN_TOOLS,
    ENTRY_POINT_GROUP,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    load_registry,
)

__all__ = [
    "BUILTIN_TOOLS",
    "DATA_BOUNDARY",
    "ENTRY_POINT_GROUP",
    "AuthenticationFailed",
    "BadResponse",
    "ConfigError",
    "InvalidOutputSchema",
    "ModelNotFound",
    "OutputSchema",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderServerError",
    "ProviderUnreachable",
    "SchemaViolation",
    "TemplateError",
    "ToolRegistry",
    "ToolSpec",
    "UnknownTool",
    "config_from_env",
    "load_registry",
    "render_prompt",
    "server_message",
    "strip_fences",
    "wrap_input",
]

_LAZY = {"InvalidOutputSchema", "OutputSchema", "SchemaViolation"}


def __getattr__(name: str):
    """`dagentos.providerkit.OutputSchema` etc. resolve on first use so importing the kit
    (which the built-in `tool` executor does for `render_prompt`) never requires `jsonschema`."""
    if name in _LAZY:
        from dagentos.providerkit import schema as _schema
        return getattr(_schema, name)
    raise AttributeError(f"module 'dagentos.providerkit' has no attribute {name!r}")
