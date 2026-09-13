"""Ports: the only surfaces the core depends on.

Everything outside `agentos.core` (stores, executors, transports, model vendors) is an
adapter that implements one of these Protocols and is injected into the engine. The core
imports nothing from those packages; `import-linter` enforces that in CI.
See docs/DEVELOPMENT_STRUCTURE.md §1-2.
"""
from __future__ import annotations

from typing import Protocol

from agentos.core.models import Agent, WorkflowDefinition, WorkflowRun


class Store(Protocol):
    """Persistence port. Phase 0: in-memory. Phase 1: Postgres + SQLite adapters
    that pass the same contract suite."""

    def put_agent(self, agent: Agent) -> None: ...
    def get_agent(self, name: str) -> Agent | None: ...
    def list_agents(self) -> list[Agent]: ...
    def put_workflow(self, wf: WorkflowDefinition) -> None: ...
    def get_workflow(self, name: str) -> WorkflowDefinition | None: ...
    def put_run(self, run: WorkflowRun) -> None: ...
    def get_run(self, run_id: str) -> WorkflowRun | None: ...


class Executor(Protocol):
    """Step-execution port. The core hands an executor an agent definition and the
    upstream outputs, and records whatever comes back. It never inspects the payload
    beyond hashing it, so a model generation change is an adapter change.

    Phase 1 widens this to StepRequest/StepResult with effects, cost and provenance
    (docs/DEVELOPMENT_STRUCTURE.md §2.1); the shape here is the Phase 0 subset."""

    def execute(self, agent: Agent, upstream: dict[str, dict]) -> dict: ...
