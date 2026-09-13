"""Core domain types.

Phase 0 keeps these in memory; the store layer abstracts persistence so
Phase 1 can move state to Postgres without touching the engine.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


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

    def topological_order(self) -> list[str]:
        """Return node IDs in dependency order (Kahn's algorithm).

        Raises ValueError on a dangling dependency or a cycle. This is the single
        implementation of DAG validity; `validate_dag()` is a thin alias so the
        API boundary and the engine can never disagree about what a valid DAG is.
        """
        ids = {n.id for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in ids:
                    raise ValueError(f"node {n.id!r} depends on unknown node {dep!r}")

        indegree = {n.id: len(n.depends_on) for n in self.nodes}
        dependents: dict[str, list[str]] = {nid: [] for nid in ids}
        for n in self.nodes:
            for dep in n.depends_on:
                dependents[dep].append(n.id)

        # Deterministic order: sort ready nodes so replays are reproducible.
        ready = sorted(nid for nid, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for child in sorted(dependents[nid]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(self.nodes):
            stuck = sorted(nid for nid, d in indegree.items() if d > 0)
            raise ValueError(f"workflow {self.name!r} has a cycle at {stuck[0]!r}")
        return order

    def validate_dag(self) -> None:
        """Reject cycles and dangling dependencies before a run ever starts."""
        self.topological_order()


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    # Phase 3: awaiting human approval.
    suspended = "suspended"
    completed = "completed"
    failed = "failed"


class EffectClass(str, Enum):
    """Closed vocabulary of side-effect classes a step may DECLARE before it runs.

    Owned by the core so the governor (budget + approval policy) can refuse a step
    before dispatch rather than audit it afterwards. An executor reporting an effect
    outside its declaration is dead-lettered. See docs/DEVELOPMENT_STRUCTURE.md §11 A1.
    Values are part of the event-log contract: add, never rename or remove.
    """

    read = "read"                     # observes external state only
    compute = "compute"               # pure transformation, model inference included
    write_external = "write_external"  # mutates a system outside AgentOS
    spend = "spend"                   # commits money beyond the step's own inference cost
    send_message = "send_message"     # email, chat, webhook to a human or system
    execute_code = "execute_code"     # runs generated code
    spawn_run = "spawn_run"           # requests a child run (dynamic DAG)


class PrincipalKind(str, Enum):
    human = "human"
    agent = "agent"
    system = "system"


class Principal(BaseModel):
    """Who performed a governance action (approve, reject, cancel, resume, pause).

    Recorded on the event so a 2033 reader can tell whether a human or another agent
    approved a spend. Gates on `spend` / `write_external` require `kind == human` unless
    the workflow explicitly opts out (docs/DEVELOPMENT_STRUCTURE.md §11 A2).
    """

    kind: PrincipalKind
    id: str
    attestation: str | None = None    # e.g. OIDC subject, signature, or session ref


class BlobRef(BaseModel):
    """Content-addressed reference to a payload held in the BlobStore, so events stay
    small and the log stays replayable without the bytes (§11 A4)."""

    sha256: str
    size: int
    media_type: str = "application/json"


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
