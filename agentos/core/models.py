"""Core domain types. Phase 0 keeps these in-memory; the store layer abstracts
persistence so Phase 1 can move state to Postgres without touching the engine."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid4().hex


class AgentType(str, Enum):
    echo = "echo"      # Phase 0 stand-in; deterministic, no provider needed
    llm = "llm"        # Phase 1
    tool = "tool"      # Phase 1


class Agent(BaseModel):
    name: str
    type: AgentType
    config: dict = Field(default_factory=dict)


class WorkflowNode(BaseModel):
    id: str
    agent: str                       # agent name
    depends_on: list[str] = Field(default_factory=list)


class WorkflowDefinition(BaseModel):
    name: str
    nodes: list[WorkflowNode]

    def validate_dag(self) -> None:
        """Reject cycles and dangling dependencies before a run ever starts."""
        ids = {n.id for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in ids:
                    raise ValueError(f"node {n.id!r} depends on unknown node {dep!r}")
        # cycle check via DFS
        edges = {n.id: n.depends_on for n in self.nodes}
        WHITE, GREY, BLACK = 0, 1, 2
        color = dict.fromkeys(ids, WHITE)

        def visit(node: str) -> None:
            color[node] = GREY
            for dep in edges[node]:
                if color[dep] == GREY:
                    raise ValueError(f"workflow {self.name!r} has a cycle at {node!r}")
                if color[dep] == WHITE:
                    visit(dep)
            color[node] = BLACK

        for node in ids:
            if color[node] == WHITE:
                visit(node)


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    suspended = "suspended"   # Phase 3: awaiting human approval
    completed = "completed"
    failed = "failed"


class StepResult(BaseModel):
    node_id: str
    output: dict
    finished_at: datetime = Field(default_factory=_now)


class WorkflowRun(BaseModel):
    id: str = Field(default_factory=_id)
    workflow: str
    status: RunStatus = RunStatus.pending
    steps: list[StepResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None
    error: str | None = None
