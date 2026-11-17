"""Ports: the only surfaces the core depends on.

Everything outside `dagentos.core` (stores, executors, transports, model vendors) is an
adapter that implements one of these Protocols and is injected into the engine. The core
imports nothing from those packages; `import-linter` enforces that in CI.
See docs/DEVELOPMENT_STRUCTURE.md §1-2.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from dagentos.core.events import Event, SchedulerState
from dagentos.core.models import (
    Agent,
    BlobRef,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
    WorkflowRun,
)
from dagentos.core.run_index import RunPage


class ConflictError(Exception):
    """Optimistic-concurrency failure on append: another writer advanced the run.
    Callers re-read the log and decide; they never retry blindly (C2, C6)."""


class Store(Protocol):
    """Persistence port: definitions, the append-only run log, and run lookup.

    Contract (enforced by tests/contract/ against every adapter):
    - `append_events` assigns `seq` = expected_seq+1..n atomically, or raises
      ConflictError if the run's current last seq != expected_seq. Nothing is written
      on conflict. `UNIQUE(run_id, seq)` is the physical guarantee behind C2.
    - `fence` (optional): the appender's lease fence. The store remembers the highest
      fence seen per run and rejects a lower one with ConflictError — a stalled worker
      whose lease lapsed cannot write even though it is still alive (C6).
    - `read_events(run_id, after_seq)` returns events with seq > after_seq in seq order.
    - `run_id_for_request(request_id)` makes run start idempotent (DESIGN §6).
    """

    # definitions. Agents are immutable per (name, version): put_agent with an existing
    # version and a different body raises ConflictError; same body is a no-op.
    def put_agent(self, agent: Agent) -> None: ...
    def get_agent(self, name: str, version: int | None = None) -> Agent | None: ...
    def list_agents(self) -> list[Agent]: ...              # latest version of each name
    def list_agent_versions(self, name: str) -> list[int]: ...
    def put_workflow(self, wf: WorkflowDefinition) -> None: ...
    def get_workflow(self, name: str) -> WorkflowDefinition | None: ...

    # run log
    def append_events(self, run_id: str, expected_seq: int,
                      events: Sequence[Event], *, fence: int | None = None) -> list[Event]: ...
    def read_events(self, run_id: str, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        """Events with seq > after_seq in seq order; at most `limit` when given. The fold
        reads unbounded; `GET /runs/{id}/events` pages with it."""
        ...
    def ping(self, timeout: float) -> None:
        """One real round-trip to the backing store, or raise. Must give up within `timeout`
        seconds — a readiness probe has a window and a pool's default acquire wait (30 s for
        psycopg_pool) is longer than any probe. Reads nothing; writes nothing."""
        ...
    def list_run_ids(self) -> list[str]:
        """Every run id, oldest first (creation order)."""
        ...
    def run_id_for_request(self, request_id: str) -> str | None: ...

    # Run index (audit E-07, dagentos.core.run_index): a derived row per run, written by
    # `append_events` in the same transaction as the events from `index_events`, so it never
    # disagrees with the fold at a commit boundary. Nothing that decides reads it — callers
    # fold each run it returns and act on the fold.
    def list_runs(self, *, status: Sequence[RunStatus] | None = None,
                  workflow: str | None = None, cursor: str | None = None,
                  limit: int | None = 50) -> RunPage:
        """Runs newest-created first (created_at, then run_id, descending), filtered on the
        index. `cursor` is a previous page's `next_cursor`; ValueError if it is not one.
        `limit=None` returns every match in one page (the sweep and the inbox)."""
        ...
    def run_status_counts(self, *, workflow: str | None = None) -> dict[str, int]:
        """Runs per indexed status; a row not indexed yet is not counted (doctor reports it)."""
        ...

    # Snapshots (C15): a folded WorkflowRun at `seq`, so a resume reads only the events
    # after it. A bounded OPTIMIZATION of the fold, never the source of truth: the engine
    # re-verifies the chain link (first event after the snapshot must chain to
    # `last_hash`), and `GET /runs/{id}/integrity` always folds the whole log.
    def put_snapshot(self, run_id: str, seq: int, last_hash: str | None, state: dict) -> None: ...
    def get_snapshot(self, run_id: str) -> tuple[int, str | None, dict] | None: ...

    # Archive (audit E-08, `agentos archive`): the ONE way rows leave a store, and only after
    # the run's record has been written to a file and read back (dagentos.export.archive_runs).
    # Not an edit to a log: every row of the run (events, snapshot, queue and lease rows, the
    # run row — so its request_id is released) goes in one transaction, or none does.
    def purge_run(self, run_id: str, *, expected_seq: int,
                  blobs: Sequence[str] = ()) -> list[str]:
        """Remove every row of `run_id`, refusing with ConflictError when the log's last seq
        is not `expected_seq` (something appended since the caller exported it — a retry on a
        failed run) or the run is unknown; nothing is written on refusal. Then, for each sha256
        in `blobs`, delete the blob only if no REMAINING run's stored record names it
        (content-addressed blobs are shared: a pricing table, identical inputs). Returns the
        sha256 of every blob removed. Blobs are never touched unless named."""
        ...

    # Scheduler-wide pause (ROADMAP A12 / audit E-03). One fact for the whole deployment, so it
    # cannot live in any run's log (replay is per run). The store keeps an append-only history
    # and `get` returns the newest row — a default, never-paused state on a fresh store. Written
    # only by the operator CLI; read by the engine before every wave and by the worker before
    # every pull and sweep. A `set` without `changed_at` is stamped by the store. A `get` that
    # RAISES is treated as paused by every reader (fail-closed, docs/FAIL_MODES.md).
    def get_scheduler_state(self) -> SchedulerState: ...
    def set_scheduler_state(self, state: SchedulerState) -> None: ...


class BlobStore(Protocol):
    """Content-addressed payload storage. Events carry a BlobRef; bytes live here.
    Adapters: SQLite, filesystem, S3-compatible. Phase 2 wraps `put` in per-run
    encryption so erasure = key destruction (§11 A7) while the log stays append-only."""

    def put(self, data: bytes, media_type: str = "application/json") -> BlobRef: ...
    def get(self, ref: BlobRef) -> bytes: ...
    def exists(self, ref: BlobRef) -> bool: ...


ProgressFn = Callable[[float, str], None]


class Executor(Protocol):
    """Step-execution port (docs/DEVELOPMENT_STRUCTURE.md §2.1, §11).

    The core hands an executor a `StepRequest` — hydrated inputs, the effects the agent
    DECLARED, the budget and deadline — and a `progress(fraction, note)` callback that
    renews the worker's lease and may append a rate-limited `step.progress` event. The
    executor returns a `StepResult`: opaque output, the effects it actually caused, its
    metered cost, and provenance. The core never inspects the output beyond hashing it,
    so a model generation change is an adapter change.

    Contract the core enforces (never trusts): reported effect classes ⊆ declared;
    cost ≤ budget; wall time ≤ budget. Violations dead-letter the step."""

    name: str
    version: str

    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult: ...


class Observer(Protocol):
    """Telemetry port. Receives every event the engine appends, in seq order, after the
    store has committed it. Observability is a CONSUMER of the log — spans and metrics
    are derived from events, so the core imports no telemetry SDK, timings come from
    `occurred_at` rather than export-time wall clock, and anything that can read the log
    (including a later replay) can rebuild the same telemetry
    (docs/DEVELOPMENT_STRUCTURE.md §11 A9). Must never raise into the engine."""

    def observe(self, event: Event, run: WorkflowRun | None = None) -> None: ...
