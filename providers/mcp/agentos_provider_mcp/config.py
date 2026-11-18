"""Configuration for the MCP provider. The server, the tool mapping and the call live in
each AGENT DEFINITION (`config.server` / `config.tools` / `config.call`, see executor.py):
they are the operator's declaration, versioned with the agent. The environment carries
only the worker-wide defaults an agent may tighten or loosen per step:

    AGENTOS_MCP_TIMEOUT     30         Seconds one step (spawn + initialize + tools/list +
                                       tools/call) may take before the server's process group
                                       is killed. An agent's `config.timeout` overrides it.
    AGENTOS_MCP_MAX_BYTES   1048576    Largest single JSON-RPC frame the executor will read
                                       from the server (1 MiB). An agent's `config.max_bytes`
                                       overrides it.

`ConfigError` names the variable to fix. Nothing else in the environment is consulted here;
`${NAME}` references in an agent's `config.server.env` are resolved at call time from the
worker's environment by the executor (and redacted from everything the step records).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dagentos.providerkit.config import ConfigError

__all__ = ["DEFAULT_MAX_BYTES", "DEFAULT_TIMEOUT", "ConfigError", "McpConfig", "from_env"]

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 1 << 20


@dataclass(frozen=True)
class McpConfig:
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        if not (self.timeout_seconds > 0):
            raise ConfigError(f"AGENTOS_MCP_TIMEOUT must be a positive number of seconds, "
                              f"got {self.timeout_seconds!r}")
        if not (self.max_bytes > 0):
            raise ConfigError(f"AGENTOS_MCP_MAX_BYTES must be a positive number of bytes, "
                              f"got {self.max_bytes!r}")


def from_env(env: dict[str, str] | None = None) -> McpConfig:
    e = os.environ if env is None else env
    t_raw = e.get("AGENTOS_MCP_TIMEOUT")
    if t_raw is None:
        timeout = DEFAULT_TIMEOUT
    else:
        try:
            timeout = float(t_raw)
        except ValueError as exc:
            raise ConfigError(f"AGENTOS_MCP_TIMEOUT must be a number of seconds, "
                              f"got {t_raw!r}") from exc
        if not (timeout > 0):
            raise ConfigError(f"AGENTOS_MCP_TIMEOUT must be positive, got {t_raw!r}")

    b_raw = e.get("AGENTOS_MCP_MAX_BYTES")
    if b_raw is None:
        max_bytes = DEFAULT_MAX_BYTES
    else:
        try:
            max_bytes = int(b_raw)
        except ValueError as exc:
            raise ConfigError(f"AGENTOS_MCP_MAX_BYTES must be an integer number of bytes, "
                              f"got {b_raw!r}") from exc
        if max_bytes <= 0:
            raise ConfigError(f"AGENTOS_MCP_MAX_BYTES must be positive, got {b_raw!r}")

    return McpConfig(timeout_seconds=timeout, max_bytes=max_bytes)
