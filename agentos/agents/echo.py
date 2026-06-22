"""Deterministic stand-in agent for Phase 0 — proves the pipe without a provider.
Phase 1 adds llm.py (Bedrock/OpenAI) and tool.py behind a common dispatch interface."""
from __future__ import annotations

from agentos.core.models import Agent


def run_echo(agent: Agent, upstream: dict[str, dict]) -> dict:
    message = agent.config.get("message", "hello from agentos")
    return {"agent": agent.name, "message": message, "received": upstream}
