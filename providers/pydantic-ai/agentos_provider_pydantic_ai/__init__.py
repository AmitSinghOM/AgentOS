"""agentos-provider-pydantic-ai — run a PydanticAI agent as one governed AgentOS step. The
SDK owns the agent loop (model, tools, turns); AgentOS owns the ledger and the authority:
declared effect classes decide which registered tools the model is even offered, the
governor gates the step before dispatch and verifies reported effects after, token usage is
metered as cost, and the whole trajectory lands in the hash-chained log.

Second inner harness after `agentos-provider-openai-agents`; same tool registry
(`dagentos.providerkit.tools`), same config keys, same output shape. Zero-config default:
PydanticAI's OpenAI-compatible chat model against a local Ollama, no API key.
"""
from dagentos.providerkit import (
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
from .executor import PydanticAIExecutor
from .tools import (
    BUILTIN_TOOLS,
    ENTRY_POINT_GROUP,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    load_registry,
)

__all__ = [
    "BUILTIN_TOOLS", "ENTRY_POINT_GROUP", "AuthenticationFailed", "BadResponse", "ConfigError",
    "ModelNotFound", "ProviderConfig", "ProviderError", "ProviderRateLimited",
    "ProviderServerError", "ProviderUnreachable", "PydanticAIExecutor", "TemplateError",
    "ToolRegistry", "ToolSpec", "UnknownTool", "from_env", "load", "load_registry",
]


def load() -> PydanticAIExecutor:
    """Entry-point target for the `agentos.executors` group."""
    return PydanticAIExecutor(from_env())
