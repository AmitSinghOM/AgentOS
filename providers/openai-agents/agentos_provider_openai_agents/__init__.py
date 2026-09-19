"""agentos-provider-openai-agents — run an OpenAI Agents SDK agent as one governed AgentOS
step. The SDK owns the agent loop (model, tools, turns); AgentOS owns the ledger and the
authority: declared effect classes decide which registered tools the model is even
offered, the governor gates the step before dispatch and verifies reported effects after,
token usage is metered as cost, and the whole trajectory lands in the hash-chained log.

Zero-config default: the SDK's chat-completions model against a local Ollama, no API key.
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
from .executor import OpenAIAgentsExecutor
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
    "ModelNotFound", "OpenAIAgentsExecutor", "ProviderConfig", "ProviderError",
    "ProviderRateLimited", "ProviderServerError", "ProviderUnreachable", "TemplateError",
    "ToolRegistry", "ToolSpec", "UnknownTool", "from_env", "load", "load_registry",
]


def load() -> OpenAIAgentsExecutor:
    """Entry-point target for the `agentos.executors` group."""
    return OpenAIAgentsExecutor(from_env())
