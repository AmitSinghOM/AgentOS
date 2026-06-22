"""In-memory store for Phase 0. The method surface is the contract the engine
depends on, so Phase 1 can drop in a Postgres-backed store with no engine changes."""
from __future__ import annotations

from agentos.core.models import Agent, WorkflowDefinition, WorkflowRun


class MemoryStore:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._runs: dict[str, WorkflowRun] = {}

    # agents
    def put_agent(self, agent: Agent) -> None:
        self._agents[agent.name] = agent

    def get_agent(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def list_agents(self) -> list[Agent]:
        return list(self._agents.values())

    # workflows
    def put_workflow(self, wf: WorkflowDefinition) -> None:
        self._workflows[wf.name] = wf

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        return self._workflows.get(name)

    # runs
    def put_run(self, run: WorkflowRun) -> None:
        self._runs[run.id] = run

    def get_run(self, run_id: str) -> WorkflowRun | None:
        return self._runs.get(run_id)


store = MemoryStore()
