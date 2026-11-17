# AgentOS — Design Document

> A control plane for orchestrating multi-step LLM agent workflows with
> **durable execution**, **human-in-the-loop approvals**, and **full observability**.

Status: written at Phase 0 (`v0.1.0-skeleton`); current release `v0.18.0` (Phase 11, `ROADMAP.md`).
This document drives the build. Its architecture prose is kept as written; where a decision
was later superseded, a dated banner at the top of the section points at the current source
rather than rewriting the history.
How the build is structured to outlive its dependencies and model generations is in
[docs/DEVELOPMENT_STRUCTURE.md](./docs/DEVELOPMENT_STRUCTURE.md).

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
AgentOS executes it durably, survives crashes, commits exactly one completion per step (a
step interrupted before its commit is re-run — effects rely on downstream idempotency; see
`docs/FAIL_MODES.md`), pauses for human approval, and emits a full trace + cost breakdown
for every run.

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

> **Superseded (2026-11-16).** The diagram below is the Phase 0 plan. Since Phase 1 there is no
> Redis: the run queue and the per-run lease are ports (`Queue`, `Lease` in
> `dagentos/core/coordination.py`) with SQLite, Postgres and Memory adapters in the store, so
> the event log, queue and leases share one system of record (ROADMAP Phase 1 "ADR note",
> Phase 6 "dependencies removed"; `docker-compose.yml` has no redis service). Model vendors are
> not in the core: the core ships `echo` and `tool` (`dagentos/agents/`); LLM executors are
> `agentos-provider-*` plugins found through the `agentos.executors` entry point
> (`docs/DEVELOPMENT_STRUCTURE.md` §2.2, ROADMAP Phase 4). Telemetry is as drawn.

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

**State replay, not code replay (C14).** What is replayed is the *fold* — the scheduler's
"which nodes are ready" question over the log — never the agent code. Steps are opaque;
their outputs, effects and cost are recorded facts. Agent code may therefore be
non-deterministic (a model call, `random()`, threads) and may change between attempts
without stranding an in-flight run; a step that rolled a random number replays to the
recorded roll. This is the core decision that separates AgentOS from Temporal-style
durable execution and it is written up with its acceptance test in
[docs/REPLAY.md](docs/REPLAY.md). The log is tamper-evident ([docs/TRUST_BOUNDARY.md](docs/TRUST_BOUNDARY.md) §2).

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

> **Superseded (2026-11-16).** Redis was removed before it was ever required (ROADMAP Phase 1
> "ADR note"; dependency dropped in Phase 6). Coordination state — the run queue, per-run
> leases with fencing tokens, retry delays — lives in the same store as the log, behind the
> `Queue` and `Lease` ports (`dagentos/core/coordination.py`); SQLite is the default,
> Postgres the production adapter (`dagentos/store/factory.py`). The principle below ("one
> source of truth; the log wins") is what survived.

**Decision:** All durable state lives in Postgres. Redis holds only ephemeral
coordination: per-run execution locks (exactly-one-worker), retry backoff timers, and a
read cache. Redis can be flushed without data loss.

**Why:** Clear consistency story. We never have to reconcile two sources of truth — if
Redis and Postgres disagree, Postgres wins, always.

**Tradeoff:** A run cannot make progress if Postgres is down (Redis-only would be
faster but lossy). For a correctness-first control plane, that is the right call.

### 4.4 Human approval as a first-class suspended state

> **Superseded in detail (2026-11-16).** As built (ROADMAP Phase 3): the gate suspends the run
> *before* dispatch of a step whose declared effect class needs approval; the route is
> `POST /runs/{run_id}/approvals/{approval_id}/approve` (and `…/reject`,
> `dagentos/api/main.py`), with a typed `Principal` on every decision
> (`docs/TRUST_BOUNDARY.md` §1); approvals live in the log, not a side table. The model
> below — suspend, hold nothing, resume on an external decision — is unchanged.

**Decision:** An approval node emits `ApprovalRequested` and the run transitions to
`SUSPENDED` — the worker releases its lock and moves on. No thread or resource is held
while waiting. An external `POST /runs/{id}/approve` appends `ApprovalGranted` and
re-enqueues the run.

**Why:** Approvals can take hours or days. Holding execution context open that long does
not scale and does not survive restarts. Suspend-and-resume is the only correct model.

**Tradeoff:** Requires the durable-execution machinery from 4.1 to exist first — which
is why approvals are Phase 3, not Phase 1.

### 4.5 Single worker now, N workers by design

> **Superseded in detail (2026-11-16).** The lock is the `Lease` port with fencing tokens
> (`dagentos/core/coordination.py`; SQL adapters, no Redis), proven against a second worker
> over Toxiproxy (ROADMAP Phase 1–2 chaos rows). The conclusion — more workers is a
> deployment change — stands.

**Decision:** v1 runs one worker, but coordination uses Redis execution locks keyed by
`run_id`, so a run is only ever advanced by one worker at a time.

**Why:** Scaling to a worker pool later requires no redesign — just run more workers.
The hard part (exactly-once advancement) is solved from day one; horizontal scale is a
deployment change, not an architecture change.

## 5. Data Model (PostgreSQL)

> **Superseded (2026-11-16).** The tables as built, identical in SQLite and Postgres
> (`dagentos/store/sqlite.py`, `dagentos/store/postgres.py`, `dagentos/store/migrations.py`):
> `agent_versions`, `workflows`, `runs`, `leases`, `queue`, `run_events`, `blobs`,
> `run_snapshots`, `scheduler_state`, `schema_migrations`. Approvals and per-step state are
> folded from `run_events`, not stored in side tables (`dagentos/core/fold.py`); there are no
> Redis keys. The one claim below that still holds is the one that matters: `run_events` is
> the append-only source of truth.

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

> **Superseded (2026-11-16).** The authoritative table is `docs/FAIL_MODES.md`: every
> chokepoint, which way it fails, and the test that pins it. Differences from the sketch
> below: there is no Redis, so lease expiry and retry backoff are store facts (`leases`,
> `queue.visible_at`); the client-side idempotency key is the `Idempotency-Key` header on
> `POST /workflows/{name}/runs` (`dagentos/api/main.py`), not a `request_id` body field; a
> poison step is `step.dead_lettered` with a human retry path
> (`POST /runs/{id}/steps/{step}/retry`, ROADMAP Phase 2).

| Failure | Behaviour |
|---------|-----------|
| Worker crashes mid-step | Lock TTL expires → another worker (or restart) replays events, resumes from last `StepSucceeded`. In-flight step re-runs; idempotency key prevents double side-effect. |
| LLM call times out | Step fails → retry with exponential backoff (Redis-tracked). After N attempts → step `FAILED`, run → `FAILED`, event recorded. |
| Poison step (always fails) | After max retries, routed to a dead-letter state; run marked failed with the failing node + last error in the event log. |
| Postgres unavailable | Engine pauses; no progress, no corruption. Resumes when DB returns. |
| Duplicate run start (client retry) | Run-start is idempotent on a client-supplied `request_id`. |

## 7. Tech Stack

Python 3.11+ · FastAPI · Pydantic v2 · PostgreSQL (psycopg 3) or SQLite behind one `Store`
port · OpenTelemetry · Prometheus · pytest · Docker Compose · MIT. Model providers are
separate plugins (`agentos-provider-openai-compat`, `agentos-provider-anthropic`) over plain
HTTP; the core imports no vendor SDK. Redis and SQLAlchemy were in the original plan and
were removed once the queue and leases lived in the store (Phase 1) — one system of record.

(Stack chosen deliberately to match production patterns I use day-to-day, so the design
decisions are battle-tested rather than tutorial-deep.)

## 8. What I would do next with more time

> **Superseded (2026-11-16).** Two of the four shipped: time-travel debugging over the log is
> the Phase 9 UI (`GET /runs/{id}?at=k`, `docs/UI.md` "Timeline and time travel", `v0.10.0`),
> and cost ceilings that suspend a run for approval are Phase 3 (`Budget.max_run_cost`,
> ROADMAP "Cost-ceiling suspension", `v0.4.0`). The worker pool and a code DSL remain ideas;
> the current open list is ROADMAP "Open items".

- Distributed worker pool + leader-less run claiming.
- Workflow definition as code (decorator DSL) in addition to JSON.
- Replay-based "time travel" debugging UI over the event log.
- Budget guardrails: hard cost ceilings that suspend a run for approval.
