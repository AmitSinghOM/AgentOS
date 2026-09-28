"""agentos-provider-acp — run one Agent Client Protocol (ACP) agent turn as one governed
AgentOS step. `kiro-cli acp` by default; any ACP agent by configuration. The agent owns its
loop and its tools; AgentOS owns the ledger and the authority: the step is gated before the
agent is spawned, the agent's permission requests are answered from the step's declared
effect classes (allow-once or reject, never allow-always), every tool call lands in the
hash-chained log by title/kind/hashes, and an undeclared tool class dead-letters the step.

Standard library only on top of `dagentos[providerkit]`: ACP is newline-delimited JSON-RPC
2.0 over the agent's stdio.
"""
from dagentos.providerkit import (
    BadResponse,
    ConfigError,
    ProviderError,
    ProviderUnreachable,
)

from .config import DEFAULT_COMMAND, DEFAULT_TIMEOUT, AcpConfig, from_env
from .executor import (
    KIND_TO_EFFECT,
    AcpExecutor,
    effect_class_for_kind,
    permission_decision,
)
from .protocol import CLIENT_CAPABILITIES, PROTOCOL_VERSION, AcpClient

__all__ = [
    "CLIENT_CAPABILITIES", "DEFAULT_COMMAND", "DEFAULT_TIMEOUT", "KIND_TO_EFFECT",
    "PROTOCOL_VERSION", "AcpClient", "AcpConfig", "AcpExecutor", "BadResponse",
    "ConfigError", "ProviderError", "ProviderUnreachable", "effect_class_for_kind",
    "from_env", "load", "permission_decision",
]


def load() -> AcpExecutor:
    """Entry-point target for the `agentos.executors` group."""
    return AcpExecutor(from_env())
