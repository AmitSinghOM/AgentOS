# AgentOS — Design Document

> A control plane for orchestrating multi-step LLM agent workflows with
> **durable execution**, **human-in-the-loop approvals**, and **full observability**.

Status: Phase 0 (walking skeleton). This document drives the build; it is updated as decisions change.

---

## 1. Problem

LLM agent workflows are easy to prototype and hard to run. A notebook that chains
three agent calls works on the happy path and falls apart the moment something real
happens: the process crashes mid-run, a model call times out, a step needs human
sign-off before proceeding, or you need to answer "why did run #4821 cost $2.10 and
take 40 seconds?"

The hard parts are not the LLM calls — they are the **distributed-systems problems
around them**: durability, idempotency, retries without double-charging, suspended
execution waiting on humans, and observability across an async pipeline.

AgentOS is the missing control plane. You define a workflow as a DAG of agent steps;
AgentOS executes it durably, survives crashes, never double-runs a step, pauses for
human approval, and emits a full trace + cost breakdown for every run.

## 2. Goals / Non-Goals

### Goals
- **Durable execution** — a run survives a worker crash and resumes from the last
  completed step. No work is lost, no step runs twice.
- **DAG orchestration** — workflows are directed acyclic graphs; independent branches
  run in parallel, dependents wait.
- **Idempotent steps** — every step has a dedup key; re-execution after a crash is a
  no-op, so a model call is never paid for twice.
- **Human-in-the-loop** — an approval node suspends the run (persisted, zero resources
  held) until an external approve/reject resumes it.
- **Observability** — per-run and per-step traces (OpenTelemetry), latency/throughput/
  error metrics (Prometheus), and token + cost accounting.

### Non-Goals (deliberately out of scope for v1)
- Not a model-serving / inference platform — we *call* models, we don't host them.
- Not a general workflow engine (Temporal/Airflow) — purpose-built for agent workflows,
  small enough to read in an afternoon.
- No multi-tenant auth/billing — single-operator deployment.
- No distributed worker fleet in v1 — single worker with a clear path to N workers
  (the locking model already supports it).

## 3. Architecture

```
                ┌──────────────┐
   client ────▶ │  FastAPI API │  define workflows, start runs, approve, query
                └──────┬───────┘
                       │ enqueue run
                       ▼
                ┌──────────────┐      ┌─────────────────────┐
                │  Run Queue   │◀────▶│  Redis              │
                │  (Redis)     │      │  - execution locks  │
                └──────┬───────┘      │  - retry backoff    │
                       │              │  - run cache        │
                       ▼              └─────────────────────┘
                ┌──────────────┐
                │ Workflow     │  topological scheduler: picks ready DAG nodes,
                │ Engine       │  executes steps, appends events, handles retries
                └──────┬───────┘
                       │ dispatch step
                       ▼
                ┌──────────────┐
                │ Agent Router │  resolves agent + version, invokes provider
                └──────┬───────┘
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
     ┌────────────┐        ┌────────────┐
     │ LLM agents │        │ tool agents│   (Bedrock / OpenAI / HTTP tools)
     └────────────┘        └────────────┘

  source of truth:  PostgreSQL  (event-sourced run state + metadata)
  telemetry:        OpenTelemetry ──▶ Jaeger   |   Prometheus ──▶ Grafana
```

## 4. Key Design Decisions (with tradeoffs)

### 4.1 Durable execution via event sourcing
**Decision:** A run's state is not stored as a mutable row. It is the **fold over an
append-only event log** (`StepStarted`, `StepSucceeded`, `StepFailed`,
`ApprovalRequested`, `ApprovalGranted`, `RunCompleted`). Current state = replay events.

**Why:** Crash recovery becomes trivial — on restart, replay the log and you know
exactly where the run was. It also gives a free, complete audit trail (a stated
requirement) and makes "why did this run do X" answerable by reading events.

**Tradeoff:** More storage and a replay cost on resume. Mitigated with periodic state
snapshots (replay only events after the last snapshot). For agent workflows — tens of
steps, not millions — this is comfortably within budget. We accept write amplification
in exchange for bulletproof recovery and auditability.

**Rejected alternative:** mutable `run_state` row with a status column. Simpler, but a
crash between "update row" and "commit side effect" leaves you unable to tell whether
the step actually ran — which is exactly the bug class we are trying to eliminate.

### 4.2 Idempotent steps with dedup keys
**Decision:** Each step execution carries an idempotency key
(`run_id : node_id : attempt_input_hash`). Before invoking an agent, the engine checks
for an existing `StepSucceeded` event with that key; if present, it reuses the result.

**Why:** After a crash-resume or a retry, we must never pay for / re-trigger a model
call that already succeeded. This is the difference between "resumable" and "resumable
without corruption."

**Tradeoff:** A hash + lookup on the hot path. Negligible vs. an LLM call's latency.

### 4.3 Postgres = truth, Redis = speed
**Decision:** All durable state lives in Postgres. Redis holds only ephemeral
coordination: per-run execution locks (exactly-one-worker), retry backoff timers, and a
read cache. Redis can be flushed without data loss.

**Why:** Clear consistency story. We never have to reconcile two sources of truth — if
Redis and Postgres disagree, Postgres wins, always.

**Tradeoff:** A run cannot make progress if Postgres is down (Redis-only would be
faster but lossy). For a correctness-first control plane, that is the right call.

### 4.4 Human approval as a first-class suspended state
**Decision:** An approval node emits `ApprovalRequested` and the run transitions to
`SUSPENDED` — the worker releases its lock and moves on. No thread or resource is held
while waiting. An external `POST /runs/{id}/approve` appends `ApprovalGranted` and
re-enqueues the run.

**Why:** Approvals can take hours or days. Holding execution context open that long does
not scale and does not survive restarts. Suspend-and-resume is the only correct model.

**Tradeoff:** Requires the durable-execution machinery from 4.1 to exist first — which
is why approvals are Phase 3, not Phase 1.

### 4.5 Single worker now, N workers by design
**Decision:** v1 runs one worker, but coordination uses Redis execution locks keyed by
`run_id`, so a run is only ever advanced by one worker at a time.

**Why:** Scaling to a worker pool later requires no redesign — just run more workers.
The hard part (exactly-once advancement) is solved from day one; horizontal scale is a
deployment change, not an architecture change.

## 5. Data Model (PostgreSQL)

| Table | Purpose |
|-------|---------|
| `agents` | registered agents (name, type: llm/tool, provider config) |
| `agent_versions` | immutable versioned agent definitions (prompt, model, params) |
| `workflow_definitions` | versioned DAG: nodes (agent refs) + edges (dependencies) |
| `workflow_runs` | one row per run: status, definition ref, started/ended, cost total |
| `run_events` | **append-only event log** — the source of truth for run state |
| `approval_requests` | open approval gates awaiting human decision |
| `step_executions` | denormalized per-step record (for fast queries + cost rollup) |

**Redis keys:** `lock:run:{id}` (execution lock), `retry:{run}:{node}` (backoff state),
`runcache:{id}` (folded-state cache).

## 6. Failure Modes & Handling

| Failure | Behaviour |
|---------|-----------|
| Worker crashes mid-step | Lock TTL expires → another worker (or restart) replays events, resumes from last `StepSucceeded`. In-flight step re-runs; idempotency key prevents double side-effect. |
| LLM call times out | Step fails → retry with exponential backoff (Redis-tracked). After N attempts → step `FAILED`, run → `FAILED`, event recorded. |
| Poison step (always fails) | After max retries, routed to a dead-letter state; run marked failed with the failing node + last error in the event log. |
| Postgres unavailable | Engine pauses; no progress, no corruption. Resumes when DB returns. |
| Duplicate run start (client retry) | Run-start is idempotent on a client-supplied `request_id`. |

## 7. Tech Stack

Python 3.11 · FastAPI · SQLAlchemy 2.x + PostgreSQL · Redis · Pydantic v2 ·
OpenTelemetry · Prometheus · pytest · Docker Compose. Bedrock + OpenAI agent providers.

(Stack chosen deliberately to match production patterns I use day-to-day, so the design
decisions are battle-tested rather than tutorial-deep.)

## 8. What I would do next with more time
- Distributed worker pool + leader-less run claiming.
- Workflow definition as code (decorator DSL) in addition to JSON.
- Replay-based "time travel" debugging UI over the event log.
- Budget guardrails: hard cost ceilings that suspend a run for approval.
