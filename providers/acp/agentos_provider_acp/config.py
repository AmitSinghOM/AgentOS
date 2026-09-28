"""Configuration for the ACP provider. Every knob is an environment variable with a
documented default; the zero-config default drives `kiro-cli acp` in the worker's current
directory.

    AGENTOS_ACP_COMMAND   ["kiro-cli", "acp"]   JSON list: the agent program and its arguments.
                                                Any ACP agent works (kiro-cli, Gemini CLI,
                                                Claude Code via its ACP adapter, ...). Do NOT
                                                pass `--trust-all-tools`: permission requests
                                                are answered from the step's declaration.
    AGENTOS_ACP_CWD       (worker's cwd)        Absolute path the session runs in (ACP
                                                requires absolute). The agent's own tools
                                                read and write here — pick a checkout.
    AGENTOS_ACP_TIMEOUT   600                   Seconds one prompt turn may take before the
                                                executor cancels and kills the agent.

`ConfigError` names the variable to fix. Nothing else in the environment is consulted;
the agent process inherits the worker's environment (it needs its own login, e.g.
`kiro-cli login`), plus any `env` overrides given programmatically.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dagentos.providerkit.config import ConfigError

__all__ = ["DEFAULT_COMMAND", "DEFAULT_TIMEOUT", "AcpConfig", "ConfigError", "from_env"]

DEFAULT_COMMAND: tuple[str, ...] = ("kiro-cli", "acp")
DEFAULT_TIMEOUT = 600.0


@dataclass(frozen=True)
class AcpConfig:
    command: list[str]
    cwd: Path
    timeout_seconds: float = DEFAULT_TIMEOUT
    env: dict[str, str] = field(default_factory=dict)   # overrides merged onto os.environ

    def __post_init__(self) -> None:
        if not self.command:
            raise ConfigError("AGENTOS_ACP_COMMAND must name a program (non-empty list)")
        if not self.cwd.is_absolute():
            raise ConfigError(f"AGENTOS_ACP_CWD must be an absolute path (ACP requires it), "
                              f"got {str(self.cwd)!r}")
        if not (self.timeout_seconds > 0):
            raise ConfigError(f"AGENTOS_ACP_TIMEOUT must be a positive number of seconds, "
                              f"got {self.timeout_seconds!r}")


def from_env(env: dict[str, str] | None = None) -> AcpConfig:
    e = os.environ if env is None else env
    raw = e.get("AGENTOS_ACP_COMMAND")
    if raw is None:
        command = list(DEFAULT_COMMAND)
    else:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"AGENTOS_ACP_COMMAND must be a JSON list of strings, e.g. "
                              f'["kiro-cli", "acp"]; got {raw!r}') from exc
        if not isinstance(parsed, list) or not parsed or \
                not all(isinstance(p, str) and p for p in parsed):
            raise ConfigError(f"AGENTOS_ACP_COMMAND must be a non-empty JSON list of strings, "
                              f'e.g. ["kiro-cli", "acp"]; got {raw!r}')
        command = parsed

    cwd_raw = e.get("AGENTOS_ACP_CWD")
    cwd = Path(cwd_raw) if cwd_raw else Path.cwd()
    if cwd_raw and not cwd.is_absolute():
        raise ConfigError(f"AGENTOS_ACP_CWD must be an absolute path, got {cwd_raw!r}")

    t_raw = e.get("AGENTOS_ACP_TIMEOUT")
    if t_raw is None:
        timeout = DEFAULT_TIMEOUT
    else:
        try:
            timeout = float(t_raw)
        except ValueError as exc:
            raise ConfigError(f"AGENTOS_ACP_TIMEOUT must be a number of seconds, "
                              f"got {t_raw!r}") from exc
        if not (timeout > 0):
            raise ConfigError(f"AGENTOS_ACP_TIMEOUT must be positive, got {t_raw!r}")

    return AcpConfig(command=command, cwd=cwd, timeout_seconds=timeout)
