"""Deterministic stand-in executor for Phase 0 — proves the pipe without a provider.
Implements `agentos.core.ports.Executor`. Phase 1 adds a `tool` executor (HTTP/subprocess)
here and moves model-vendor executors into separate `agentos-provider-*` distributions."""
from __future__ import annotations

from agentos.core.models import Agent


class EchoExecutor:
    def execute(self, agent: Agent, upstream: dict[str, dict]) -> dict:
        message = agent.config.get("message", "hello from agentos")
        return {"agent": agent.name, "message": message, "received": upstream}


def run_echo(agent: Agent, upstream: dict[str, dict]) -> dict:
    """Function form kept for callers that predate the Executor port."""
    return EchoExecutor().execute(agent, upstream)
