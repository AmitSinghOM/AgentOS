"""agentos-provider-mcp — run one Model Context Protocol (MCP) tool call on a stdio server
as one governed AgentOS step. The agent definition names the server, maps each tool it may
call to an effect class (the operator's mapping, never the server's annotations) and fixes
the one call; the step is gated before the server is spawned, a call outside the mapping or
the declaration is rejected before it is sent, the server's tool list is hash-pinned, and the
result enters the run as data.

Standard library only on top of `dagentos[providerkit]`: MCP is newline-delimited JSON-RPC
2.0 over the server's stdio (spec 2025-11-25). Streamable-HTTP servers are out of scope.
"""
from dagentos.providerkit import (
    BadResponse,
    ConfigError,
    ProviderError,
    ProviderUnreachable,
)

from .config import DEFAULT_MAX_BYTES, DEFAULT_TIMEOUT, McpConfig, from_env
from .executor import McpExecutor, tool_sha256, tools_sha256
from .protocol import (
    CLIENT_CAPABILITIES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpClient,
)

__all__ = [
    "CLIENT_CAPABILITIES", "DEFAULT_MAX_BYTES", "DEFAULT_TIMEOUT", "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS", "BadResponse", "ConfigError", "McpClient", "McpConfig",
    "McpExecutor", "ProviderError", "ProviderUnreachable", "from_env", "load", "tool_sha256",
    "tools_sha256",
]


def load() -> McpExecutor:
    """Entry-point target for the `agentos.executors` group."""
    return McpExecutor(from_env())
