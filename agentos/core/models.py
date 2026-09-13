"""Core domain types.

Phase 0 keeps these in memory; the store layer abstracts persistence so
Phase 1 can move state to Postgres without touching the engine.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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
    """Immutable versioned agent definition. `(name, version)` is the identity; the store
    refuses to overwrite an existing version with a different body — bump `version`.
    Runs pin the version of every agent they use at start (DESIGN §5 `agent_versions`),
    so redeploying an agent never changes a running workflow's behaviour."""

    name: str
    version: int = 1
    type: AgentType
    config: dict = Field(default_factory=dict)
    # Declare-then-do (§11 A1): the effect classes this agent is allowed to cause. Fixed
    # here, checked against the workflow budget BEFORE dispatch, and enforced against
    # what the executor actually reports. Defaults to pure computation.
    declared_effects: list[EffectClass] = Field(
        default_factory=lambda: [EffectClass.compute])


class RetryPolicy(BaseModel):
    """Per-node retry with exponential backoff. `max_attempts=1` means no retry. After the
    last attempt fails the step is dead-lettered (C11) and the run fails with the cause."""

    max_attempts: int = 1
    backoff_seconds: float = 0.0          # delay before attempt 2
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 300.0

    def delay_before(self, attempt: int) -> float:
        """Seconds to wait before `attempt` (2-based: attempt 2 waits backoff_seconds)."""
        if attempt <= 1:
            return 0.0
        return min(self.max_backoff_seconds,
                   self.backoff_seconds * (self.backoff_multiplier ** (attempt - 2)))


class WorkflowNode(BaseModel):
    id: str
    agent: str                       # agent name
    depends_on: list[str] = Field(default_factory=list)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class WorkflowDefinition(BaseModel):
    name: str
    version: int = 1                 # bumped on any change; runs pin it (C3)
    nodes: list[WorkflowNode]
    budget: Budget = Field(default_factory=lambda: Budget())
    max_parallelism: int = 4         # independent branches run concurrently up to this

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
    # Phase 2 (C5): operator control. `paused` is not terminal; `cancelled` is.
    paused = "paused"
    cancelled = "cancelled"
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


class Meter(BaseModel):
    """One metered quantity: tokens, seconds, images, requests… (§11 A6)."""

    name: str                  # e.g. "input_tokens", "gpu_seconds", "requests"
    quantity: float


class Cost(BaseModel):
    """Generic metered cost. `amount` is a decimal string to avoid float drift in a
    ledger; `pricing_snapshot_hash` points at the pricing table used, so a 2033 reader
    can explain a 2026 charge after prices have changed."""

    units: list[Meter] = Field(default_factory=list)
    amount: str = "0"          # decimal string, e.g. "0.0123"
    currency: str = "USD"
    pricing_snapshot_hash: str | None = None

    def decimal(self) -> Decimal:
        return Decimal(self.amount)


class Provenance(BaseModel):
    """Who/what produced a step's output. Mandatory on every completed step (§2.1)."""

    executor: str              # executor name, e.g. "echo", "openai"
    executor_version: str      # plugin/package version
    model_id: str | None = None
    prompt_hash: str | None = None


class Effect(BaseModel):
    """A side effect a step reports having caused. Its class must be within the step's
    DECLARED effects or the step is dead-lettered (§11 A1)."""

    effect_class: EffectClass
    description: str = ""
    external_ref: str | None = None   # e.g. message id, payment id, PR URL


class Budget(BaseModel):
    """Limits the core enforces — never trusted to the executor (§2.1).

    `allowed_effect_classes` is the declare-then-do gate: an agent whose declared effects
    exceed it is refused BEFORE dispatch. Phase 3 turns that refusal into SUSPENDED for
    approval; today it dead-letters the step and fails the run."""

    allowed_effect_classes: set[EffectClass] = Field(
        default_factory=lambda: {EffectClass.read, EffectClass.compute})
    max_step_cost: str | None = None        # decimal string
    max_run_cost: str | None = None         # decimal string; rolling total across steps
    max_step_wall_seconds: float | None = None


class StepRequest(BaseModel):
    """What an executor receives. `inputs` is the hydrated upstream map; `inputs_ref` is
    what the log records. `declared_effects` is fixed from the agent definition before
    dispatch — the executor cannot widen it."""

    run_id: str
    step_id: str
    attempt: int
    idempotency_key: str
    agent: Agent
    inputs: dict
    inputs_ref: BlobRef
    declared_effects: frozenset[EffectClass]
    budget: Budget
    deadline: datetime | None = None


class StepResult(BaseModel):
    """What an executor returns. `output` is opaque to the core beyond hashing."""

    output: dict
    effects: list[Effect] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    provenance: Provenance


class StepRecord(BaseModel):
    """Folded per-step view: what the log recorded about a completed step."""

    node_id: str
    attempt: int = 1
    output_ref: BlobRef | None = None     # what the log records (§11 A4)
    output: dict = Field(default_factory=dict)  # hydrated from the BlobStore for callers
    effects: list[Effect] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    provenance: Provenance | None = None
    finished_at: datetime = Field(default_factory=_now)


class StepState(str, Enum):
    dead_lettered = "dead_lettered"       # C11: poison step; cause in the log


class WorkflowRun(BaseModel):
    """Folded view of a run's event log. Never persisted directly — always derived
    by `agentos.core.fold.fold` from `run_events`."""

    id: str = Field(default_factory=_id)
    workflow: str
    workflow_version: int = 1
    request_id: str = Field(default_factory=_id)
    agent_versions: dict[str, int] = Field(default_factory=dict)  # name → pinned version
    status: RunStatus = RunStatus.pending
    steps: list[StepRecord] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)   # step_id → latest attempt
    progress: dict[str, float] = Field(default_factory=dict)  # step_id → last reported fraction
    dead_lettered: dict[str, str] = Field(default_factory=dict)  # step_id → cause
    failed_steps: dict[str, str] = Field(default_factory=dict)   # step_id → last error (terminal)
    pending_retries: dict[str, datetime] = Field(default_factory=dict)  # step_id → not before
    cancelled_steps: list[str] = Field(default_factory=list)     # steps interrupted by a cancel
    cancel_requested: bool = False                            # request persisted, not yet finalized
    pause_requested: bool = False
    total_cost: str = "0"                                     # decimal string, rolled up
    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None
    error: str | None = None
    parent_run_id: str | None = None
    last_seq: int = 0


# Several models above reference types defined later in this module (e.g. `Agent` →
# `EffectClass`, `WorkflowDefinition` → `Budget`). Pydantic defers building those until
# first use; complete them here so a framework that snapshots a model's serializer at
# import time (FastAPI's response fields) never sees a half-built model.
for _model in (Agent, WorkflowNode, WorkflowDefinition, StepRequest, StepResult, StepRecord,
               WorkflowRun):
    _model.model_rebuild()
