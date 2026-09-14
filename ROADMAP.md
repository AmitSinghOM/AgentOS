# AgentOS — Build Roadmap

Built in weekend-sized phases. Each phase ends with a **tagged release**, a **demo you
can record in 60 seconds**, and a **blog post**. Ship small, iterate visibly — the commit
history should read like a real project, because it is one.

> Rule: do not start phase N+1 until phase N is demoable and tagged. Earn scope by
> finishing.

---

## Phase 0 — Walking Skeleton  ·  ~1 weekend  ·  ✅ shipped `v0.1.0-skeleton`
**Goal:** the whole pipe is connected end-to-end, even if it does almost nothing.

- [x] Repo scaffold, `pyproject.toml`, ruff + pytest config, CI green on push and PR
- [x] `docker compose up` brings up Postgres + Redis (CI `compose` job proves healthchecks)
- [x] FastAPI app boots; `/health` returns OK
- [x] Agent registry: `POST /agents`, `GET /agents` (in-memory)
- [x] One workflow definition with a single "echo" agent node (`examples/`)
- [x] `POST /workflows/{name}/runs` runs it synchronously, `GET /runs/{id}` shows result
- [x] README quick start runs as a test (`tests/test_quickstart.py`)
- [x] **C13** — MIT, compose is the whole product, no hosted tier assumed ([#13](https://github.com/AmitSinghOM/AgentOS/issues/13))

**Demo:** register echo agent → define workflow → start run → see output.
**Tag:** `v0.1.0-skeleton`. **Post:** "Designing AgentOS: a control plane for agent workflows."

**What I'd do differently:** the README `curl -d @file` lines shipped without a
`Content-Type` header and returned 422; the quick start is now a test so it cannot drift.

---

### Landscape cons (C1–C15)

The Phase 1–3 scope below is anchored to a dated survey of what the popular agent
runtimes get wrong — LangGraph, agno, Microsoft Agent Framework, crewAI, OpenAI Agents,
Google ADK, mastra, Temporal, DBOS, Hatchet — with each con cited to an open issue or a
documented rule. Survey: `Study/AGENTOS_LANDSCAPE.md` §4. Each con is a GitHub issue
labelled `landscape-con`; an issue closes only when its acceptance test is in the suite.

## Phase 1 — Durable Execution  ·  ~2 weekends   ← the flagship  ·  ✅ shipped `v0.2.0-durable` (2026-09-13)
**Goal:** a run survives a crash and resumes. No step runs twice. This is the demo that
wins interviews. **Met:** `tests/chaos/test_kill9_real_process.py` + five in-process fault
points + the Toxiproxy lease-expiry race, all in CI. Items below marked ⏭ were carried
forward — see the dated decision at the end of this phase.

- [x] Event-sourced run state: `run_events` table, fold-to-state replay (`core/events.py`, `core/fold.py`)
- [ ] **Longevity structure** (`docs/DEVELOPMENT_STRUCTURE.md`): ports + injected adapters, import-linter contract, CI matrix 3.11/3.14 — **shipped at Phase 0 close**; remaining Phase 1 items:
  - [x] `schema_version`, `event_type`, `parent_run_id` on every event; upcaster registry (`core/upcast.py`)
  - [x] `StepRequest`/`StepResult` with `effects`, `cost`, `provenance` (§2.1); budget enforced by the core — **done in Phase 2 slice 1** (`core/engine.py` gate → dispatch → verify → record)
  - [ ] ⏭ Model-vendor executors as `agentos-provider-*` plugins via entry points; core ships `echo` + `tool` only — the Phase 1 demo needs no vendor key
  - [x] SQLite (`store/sqlite.py`, stdlib), Postgres (`store/postgres.py`, psycopg 3) and Memory adapters all pass `tests/contract/` (store + coordination suites; Postgres leg runs in CI against a service container)
  - [~] golden corpus mechanism live (`tests/golden/`, `scripts/record_golden.py`, `v0.2.0-dev.json`); record `v0.2.0.json` at release
  - [ ] ⏭ ADR 0006 core-depends-on-nothing, 0007 log-is-the-API, 0008 providers-are-plugins, 0009 queue-and-lease-are-ports
  - [ ] **AI-engineering pass (§11)** — `EffectClass`, `Principal`, `BlobRef`, `BlobStore` port **shipped at Phase 0 close**; Phase 1 items:
    - [x] A1 declare-then-do: `Agent.declared_effects` checked against `WorkflowDefinition.budget.allowed_effect_classes` BEFORE dispatch; undeclared reported effect → `step.dead_lettered` + run failed (Phase 3 turns the failure into SUSPENDED-for-approval)
    - [x] A2 `Principal` mandatory on every decision (422 without one); `spend`/`write_external` require `kind == human` unless `Budget.allow_agent_approval` (403 otherwise); other gated classes may be agent-approved
    - [ ] ⏭ A3 capability aliases (`chat.fast`, …) resolved by provider plugins; `ExecutorSubstituted` event on change
    - [~] A4 events carry `BlobRef`; SQLite + Memory `BlobStore` adapters; filesystem adapter still to add
    - [x] A5 `progress(fraction, note)` renews the lease on every call and appends `step.progress` at most once per second; lease loss mid-step raises `LeaseLost` before any write (proven over Toxiproxy alongside the fence)
    - [~] A6 metered `Cost{units: [Meter], amount (decimal string), currency, pricing_snapshot_hash}`; per-step and rolling run ceilings enforced; dead-lettered cost still counted. Pricing table as a blob: with the first provider plugin
    - [ ] ⏭ A10 provider plugins tested against recorded cassettes; live re-record is a nightly opt-in job
- [x] Worker process consuming a run queue, decoupled from the API (`agentos/worker/`; queue is a `Queue` port with SQLite/Postgres/Memory adapters — Redis is now an optional adapter, not a requirement: see ADR note below)
- [ ] ⏭ Real tool agent (HTTP/subprocess) in core; LLM executors live in provider plugins (see longevity structure)
- [x] Idempotency keys on step execution (`run_id:step_id:sha256(inputs)`); completed steps replayed from the log, never re-executed
- [x] Per-run lease with **fencing tokens** (`Lease` port; a stale holder cannot append) — SQLite/Postgres/Memory adapters, one contract suite
- [ ] ⏭ State snapshots to bound replay cost (log stays the source of truth; replay of tens of steps is sub-millisecond today, so this is an optimization not a correctness gap)
- [x] **Chaos suite** (`tests/chaos/`): deterministic fault points in the worker, run in CI on every PR — see *Chaos engineering plan* below. The `kill -9` demo is `tests/chaos/test_kill9_real_process.py` (real subprocess, exit 137).
  - [x] `FaultInjector` port consulted at named points; production binding is a no-op (`core/faults.py`)
  - [x] `before_effect_commit` → step re-runs, exactly one completion (C1)
  - [x] `after_effect_commit` → step replayed, executor not called again (C1)
  - [x] `after_run_commit_before_ack` → redelivery is a no-op, nothing appended (C2)
  - [x] stall after lock acquire past TTL + 2nd worker → exactly one advancement per step, stale worker fenced (C6)
  - [x] definition changed between crash and resume → refused with version error (C3)
  - [ ] ⏭ `coordination_reset_mid_run` → run completes from the log alone (leases/queue rebuilt by the recovery sweep)
  - [ ] ⏭ `pg_connection_drop_mid_txn` → no partial event written; worker reconnects and resumes
  - [ ] ⏭ Property test over the event log (Hypothesis): random fault schedule × random 1–5 step workflow → invariants hold (see plan)
- [x] **C1** — effects recorded before ack; replay never re-executes user code ([#1](https://github.com/AmitSinghOM/AgentOS/issues/1))
- [x] **C2** — idempotent enqueue and append, `UNIQUE(run_id, seq)` ([#2](https://github.com/AmitSinghOM/AgentOS/issues/2))
- [x] **C3** — replay by `step_id` + `workflow_version`, never by position ([#3](https://github.com/AmitSinghOM/AgentOS/issues/3))
- [x] **C6** — separate API/worker, per-run lease, no process-local live state ([#6](https://github.com/AmitSinghOM/AgentOS/issues/6))
- [ ] **C10** — persistence behind a port; typed serialization round-trips ([#10](https://github.com/AmitSinghOM/AgentOS/issues/10))
- [ ] **C14** — event-sourced *state* replay, not replay-the-code determinism; written into DESIGN.md ([#14](https://github.com/AmitSinghOM/AgentOS/issues/14))
- [ ] **C15** — append-only log; snapshots bounded, never the source of truth ([#15](https://github.com/AmitSinghOM/AgentOS/issues/15))

**Demo:** start a 3-step run, `kill -9` the worker after step 2, restart → it finishes
without repeating step 2. Show the event log. **Now a test:** `pytest tests/chaos/test_kill9_real_process.py`.

> ADR note (to become ADR 0009): DESIGN.md §3 names Redis as the queue/lock layer. Phase 1
> ships queue and lease as *ports* with SQL adapters so `pip install agentos` needs no
> infrastructure (docs/DEVELOPMENT_STRUCTURE.md §7). Redis becomes an optional adapter for
> deployments that want lower queue latency; correctness never depends on it.
**Tag:** `v0.2.0-durable`. **Post:** "Durable Execution: Resuming Agent Workflows After a Crash."

**Carry-forward decision (2026-09-13).** Phase 1 is tagged with its goal met and proven by
tests, and with the items marked ⏭ above *not* done. They are not silently dropped: each
stays a checkbox here and moves to the top of Phase 2, because they are all the same
change — widening the `Executor` port to `StepRequest`/`StepResult` (declared effects,
metered cost, provenance, progress heartbeat), which is also what Phase 2's retries,
dead-lettering and cancel need. Doing it once, with those consumers, beats doing it twice.
Snapshots are deferred because replay is sub-millisecond at Phase 2 scale; the golden
corpus keeps the log-is-truth invariant honest meanwhile. Provider plugins are deferred
because no vendor key is needed for anything Phase 2 proves.

**What I'd do differently:** record the fence at `acquire()` from the start. Recording it on
first write left a takeover window that only designing the Toxiproxy test made visible.

## Phase 2 — DAG Orchestration  ·  ~2-3 weekends  ·  ✅ shipped `v0.3.0-dag` (2026-09-13)
**Goal:** real multi-agent workflows, not just sequences. **Met:** fan-out/fan-in with a
flaky branch retrying (`tests/test_scheduler.py`), the governor, dead-letter + human retry,
cancel/pause, agent versioning. Items marked ⏭ carried forward — see the dated decision.

- [x] Workflow = DAG (nodes + dependency edges); validate acyclic (`WorkflowDefinition.topological_order()`, single implementation since Phase 0 close)
- [x] Topological scheduler: run all ready nodes, parallelize independent branches — wave scheduler in `core/engine.py`, bounded by `WorkflowDefinition.max_parallelism` (default 4); appends serialized through one `_Log` per `advance()`
- [x] Agent versioning: `Agent.version`, immutable per `(name, version)` in every store (409 on a changed body — bump the version); `run.started.agent_versions` pins every agent at start and each attempt resolves against the pin (`step.started.agent_version`), so redeploying an agent never changes a running workflow; `GET /agents/{name}?version=N`
- [x] Retry with exponential backoff + max attempts — `WorkflowNode.retry: RetryPolicy{max_attempts, backoff_seconds, backoff_multiplier, max_backoff_seconds}`; `step.failed(terminal=False, retry_at)`; the worker re-pushes with `delay_seconds` (queue-tracked, no Redis needed)
- [x] Dead-letter state for poison steps; run fails cleanly with cause in the log — after the last attempt a step is dead-lettered with `failed after N attempt(s): <error>`; `POST /runs/{id}/steps/{step}/retry` appends `step.retry_requested` with the `Principal` and reopens the run (409 for a healthy step)
- [x] Pass outputs along edges (step N output → step M input) — `StepRequest.inputs` is the map of upstream outputs; fan-in sees all three branches
- [x] **C4** — event per step transition (started/progress/completed/failed/dead_lettered/cancelled); a completed sibling of a dead-lettered or cancelled wave-mate is always recorded; cancellation is `run.cancel_requested` → `run.cancelled` in the log ([#4](https://github.com/AmitSinghOM/AgentOS/issues/4))
- [x] **C5** — `POST /runs/{id}/cancel|pause|resume` as persisted requests finalized at the worker's next boundary (or immediately, under a lease, when the run is idle/paused); the in-flight step's `progress()` raises `Cancelled` (cooperative token); pause finishes the current wave, leaves the queue, and is skipped by the recovery sweep; `Principal` on every control event; nothing ties a run to a client connection ([#5](https://github.com/AmitSinghOM/AgentOS/issues/5))
- [x] **C9** — one execution model: every DAG node is a durable step — the wave scheduler and the single-step path are the same loop ([#9](https://github.com/AmitSinghOM/AgentOS/issues/9))
- [x] **C11** — `DEAD_LETTERED` step state with cause and a retry path ([#11](https://github.com/AmitSinghOM/AgentOS/issues/11))
- [~] **Chaos, network class:** Toxiproxy between worker ↔ Postgres (`tests/chaos/network/`, CI job `chaos-network` with Postgres + Toxiproxy service containers)
  - [x] `lease_expiry_race` — 2 s latency on worker A's link so its lease lapses mid-step; worker B finishes; A's late write is rejected **by the fence** (asserted on the rejection reason, not just seq) (C6 split-brain). Also fixed: fence now recorded at `acquire`, not first write, closing the window between takeover and B's first append
  - [ ] ⏭ `coordination_partition_mid_run` → run resumes via recovery sweep once the link returns
  - [ ] ⏭ `pg_latency_under_fanout` → fan-in waits, no branch output lost (C4)
- [ ] ⏭ **A7** crypto-shredding: per-run data key in a `KeyStore` port; erasure = destroy key + `RunErased` event; log stays append-only
- [ ] ⏭ **A8** `agentos/protocols/`: tools described by JSON Schema; MCP / A2A / vendor function-calling are adapters; tool results are data, never prompt
- [ ] ⏭ **A12** global pause: `agentos pause --all` / `resume --all` as `SchedulerPaused` / `SchedulerResumed` events; workers finish in-flight steps, dispatch nothing new

**Demo:** a fan-out/fan-in workflow (1 → [2,3,4 parallel] → 5) with one branch failing
and retrying.
**Tag:** `v0.3.0-dag`. **Post:** "Retry Without Data Corruption" + "CQRS & Event Sourcing, for real."

**Carry-forward decision (2026-09-13).** Phase 2 is tagged with its goal met and the ⏭ items
not done. A7 (crypto-shredding) and A8 (protocol adapters) have no consumer until real
provider plugins and real payloads exist, which is Phase 3+; building them against the echo
executor would be speculative. A12 (global pause) is one event type on top of the per-run
control shipped here and is deferred to land with the operator surface in Phase 3. The two
Toxiproxy scenarios are deferred because both need the network-partition toxic, which the
current fixture does not model yet; they stay as checkboxes, not deletions.

**What I'd do differently:** design the Toxiproxy test before the fencing code (it found two
windows the code had); and set `progress()` as the cancellation token from the start rather
than considering a signature change first.

## Phase 3 — Human-in-the-Loop + Observability  ·  ~2-3 weekends  ·  ✅ shipped `v0.4.0-observable` (2026-09-14)
**Goal:** production-shaped — pausable, traceable, costed. **Met:** approval as a run state
(suspend before dispatch, principal-gated decisions, cost-ceiling suspension), OpenTelemetry
+ Prometheus derived from the log, Grafana dashboard. Items marked ⏭ carried forward — see
the dated decision.

- [x] Approval as a run state: the governor's tier-2 gate (`Budget.approval_required_for`, default write_external/spend/send_message/execute_code) appends `approval.requested` + `run.suspended` BEFORE dispatch — no `step.started`, no attempt consumed; lease released, run leaves the queue, sweep skips it. Approvals live in the log (`WorkflowRun.approvals`), not a side table
- [x] `POST /runs/{id}/approvals/{aid}/approve|reject` (+ `GET /approvals` inbox, `GET /runs/{id}/approvals`): approve → `approval.granted`, running once no gate is pending, re-enqueued; reject → dead-letter naming the decider + run failed; `…/steps/{step}/retry` reopens and re-asks. `Budget.approval_timeout_seconds` → rejected by the `system` principal on the worker's sweep
- [x] OpenTelemetry spans per run/step → Jaeger (docker compose) — `agentos/observability/otel.py` derives spans FROM THE LOG (`Observer` port; core imports no SDK); timestamps from `occurred_at`; `gen_ai.*` attributes pinned and checked against the installed semconv; replaying a log rebuilds identical spans (tested)
- [x] Prometheus metrics: run latency, throughput, error rate, retries, dead-letters, cost, meters, approvals, suspended gauge — all from events (`agentos/observability/prometheus.py`, `GET /metrics`); queue depth via a scrape-time `QueueDepthCollector` over `Store.queue_depth()` (contract-tested on all adapters)
- [x] Token + cost accounting per step, rolled up per run (`step.completed.cost`, `WorkflowRun.total_cost`); Grafana dashboard provisioned (`deploy/grafana/dashboards/agentos-runs.json`: runs/min, error rate, awaiting approval, cost, latency p50/95/99, step outcomes, retries & dead-letters, tokens by agent, approval wait, cost/min)
- [~] Grafana + Jaeger + Prometheus added to compose with provisioning; compose CI job checks all three are up; ⏭ screenshots in README (needs a run against a real provider — with the first provider plugin)
- [x] **Cost-ceiling suspension** (DESIGN §8 budget guardrails, A6): exceeding `max_run_cost` records the tripping step, then suspends with a `kind=cost` approval whose grant raises the effective ceiling to `total + max_run_cost` (`run.cost_ceiling`, in the log); human-only unless `allow_agent_approval`; rejection fails the run (money already spent, nothing to reopen); trips again at the raised ceiling
- [x] **C7** — approval is a run state; the gated step's `step.started.seq > approval.granted.seq` is asserted; it runs exactly once, tagged with its `approval_id` ([#7](https://github.com/AmitSinghOM/AgentOS/issues/7))
- [x] **C8** — per-step cost on `step.completed`; run cost = sum of step costs (Decimal); Prometheus + OTel only, no SaaS anywhere ([#8](https://github.com/AmitSinghOM/AgentOS/issues/8))
- [ ] ⏭ **C12** — trust boundary on resume payloads and replayed events ([#12](https://github.com/AmitSinghOM/AgentOS/issues/12))
- [x] **A9** OpenTelemetry GenAI semantic conventions (`gen_ai.*`) for spans; AgentOS-specific attributes under `agentos.*`; semconv version recorded on every span's resource
- [ ] ⏭ **A11** `agentos export-run --format jsonl`: inputs/outputs by hash + provenance for external evaluators (opik); AgentOS records, never scores

**Demo:** run pauses at approval, approve from another terminal, run resumes; show the
Jaeger trace and the cost breakdown.
**Tag:** `v0.4.0-observable`. **Post:** "Suspended Workflows: Human Approval as a First-Class State."

**Carry-forward decision (2026-09-14).** Phase 3 is tagged with its goal met and the ⏭ items
not done. C12 (trust boundary on resume payloads) is genuinely Phase 3 work that is not
finished: resume/approve payloads are already schema-validated by the API models and the
scheduler only dispatches steps it derives from the workflow definition, but the *executor
input* trust boundary (tool results as data, never prompt) has no consumer until a real
provider plugin exists, so it moves with the provider work. A11 (export for evaluators) and
the README screenshots likewise need a real provider to be meaningful. A12 (global pause)
is deferred once more because per-run pause covers every operational need so far; it will
be one event type when the operator surface grows. Every ⏭ item across Phases 1–3 remains
a checkbox, not a deletion, and the accumulated list is now the shape of Phase 4's first
slice: **the first provider plugin**, which unblocks A3, A7, A8, A10, A11, C12 and the
screenshots together.

**What I'd do differently:** ship the `Observer` port with Phase 1. Deriving telemetry from
the log turned out to need zero engine changes beyond the fan-out, and having spans earlier
would have made the Toxiproxy investigations faster to read.

## Phase 4 — UI (optional)  ·  ~2 weekends
**Goal:** a visual the recruiter screenshot remembers. Plays to frontend strength.

- [ ] React + (Cloudscape or shadcn) app
- [ ] Live workflow run graph: nodes light up as steps complete
- [ ] Event-log timeline view (time-travel debugging over the log)
- [ ] Cost + latency panel per run

**Tag:** `v0.5.0-ui`. **Post:** "Building a Time-Travel Debugger over an Event Log."

---

## Chaos engineering plan

Netflix's Chaos Monkey is the right *idea* and the wrong *tool* for this project. The tool
randomly terminates instances in a production fleet so engineers are forced to tolerate
instance loss; it needs a fleet to hunt in and knows nothing about where a worker's commit
boundaries are. AgentOS is one API, one worker, Postgres, Redis. What carries over is the
discipline underneath it — the Principles of Chaos Engineering: state a steady-state
hypothesis, inject a real-world fault, observe whether the hypothesis held, bound the blast
radius. Applied here, the hypotheses are the C1–C15 invariants and the faults are crashes
and partitions at the exact boundaries where a durable-execution engine can go wrong.

Three layers, each earned by the phase that needs it.

### Layer 1 — Deterministic fault points (Phase 1, in-process, runs in CI)

The worker consults a `FaultInjector` port at named points. Tests bind a fixture that
raises or `os._exit`s at a chosen point; production binds a no-op. Named points:

| Fault point | Where | Invariant it tests | Issue |
| --- | --- | --- | --- |
| `crash_before_effect_commit` | after the agent produced output, before `effect` event commit | step re-runs; exactly one `effect` per `(run_id, step_id)` | #1 |
| `crash_after_commit_before_ack` | after `effect` commit, before queue ack | redelivered message is a no-op via `UNIQUE(run_id, seq)` | #2 |
| `crash_after_lock_acquire` | after lease taken, before first event | a 2nd worker takes over after expiry; one advancement per step | #6 |
| `crash_mid_snapshot` | while writing a snapshot | replay from log still correct; snapshot is never the source of truth | #15 |
| `redis_flush_mid_run` | between two steps | run completes from the Postgres log alone | README claim |
| `pg_connection_drop_mid_txn` | inside the event-append transaction | no partial event; worker reconnects and resumes | #10 |
| `definition_changed_between_crash_and_resume` | swap the workflow definition on disk before restart | resume refused with a version error, never positional | #3 |

Steady-state invariants asserted after every fault (also the Hypothesis property test):

1. `seq` is dense and monotonic per run with no gaps.
2. Exactly one `effect` event per `(run_id, step_id)`; effect counter in the fake agent = 1.
3. Every `completed` step's `started` seq precedes its `effect` seq.
4. Terminal run state is `COMPLETED`, `FAILED`, or `DEAD_LETTERED` — never stuck `RUNNING`
   with no lease holder.
5. Folding the log from seq 0 equals folding from the latest snapshot.

The property test (`hypothesis`) draws a random 1–5 step workflow and a random schedule of
fault points, runs it to termination with automatic restarts, and checks the five
invariants. This is the Python-sized version of what Jepsen does: Jepsen is the gold
standard for exactly-once and no-lost-write claims, but it is Clojure and heavyweight;
a property test over our own event log gets most of the value at a fraction of the cost.
If AgentOS ever claims linearizable semantics publicly, that is when Jepsen earns its keep.

### Layer 2 — Network faults via Toxiproxy (Phase 2, compose service, runs in CI)

Some faults cannot be produced by a code hook because they are about *time*, not order.
Toxiproxy (Shopify, ~12k★, actively maintained) sits between the worker and Postgres/Redis
in `docker-compose.yml` and injects latency, connection resets, bandwidth limits and full
partitions without touching AgentOS code. Scenarios:

| Toxic | Target | Invariant | Issue |
| --- | --- | --- | --- |
| `latency 2000ms` | Postgres | a live worker's lease lapses; a 2nd worker takes over; the stale worker's write is **rejected** (fencing token), not merged | #6 |
| `reset_peer` | Redis, mid-run | retry timers and locks are rebuilt from Postgres; no step lost | README claim |
| `timeout` (blackhole) | Redis, during approval wait | `SUSPENDED` run resumes correctly when Redis returns (Phase 3) | #7 |
| `latency 500ms` | Postgres, during fan-out | fan-in waits; no branch output lost | #4 |
| `bandwidth 1KB/s` | Postgres | large step outputs commit atomically or not at all | #10 |

Toxics are toggled via Toxiproxy's HTTP API from the test, so each scenario is a normal
pytest case. Add a `chaos-network` CI job that brings up compose with the proxy and runs
`tests/chaos/network/`.

### Layer 3 — Fleet-level chaos (only if AgentOS ever runs on Kubernetes; probably never)

LitmusChaos and kube-monkey randomly kill pods and are the true descendants of Chaos
Monkey. They become relevant only with multiple worker replicas on a cluster, which is
Phase 2 at the earliest and unlikely for a portfolio project. Not planned; recorded here
so the omission is deliberate rather than forgotten. If it happens: one experiment,
`pod-delete` on the worker deployment during a 100-run soak, invariants 1–5 checked
against the log afterwards.

### What we deliberately do not do

- No random chaos in a "production" environment — there is none, and randomness only
  pays off with many instances and many hours of operation.
- No Netflix `chaosmonkey` binary: it requires Spinnaker and a fleet.
- No chaos tooling before Phase 1's event log exists; there is nothing to assert against.

### Definition of done for the chaos work

- Every Layer 1 fault point has a test, and the `kill -9` demo in Phase 1 is
  `tests/chaos/test_crash_after_step_2.py` recorded on video, not a shell script.
- The blog post claims exactly what CI proves: "every fault point in the commit path is
  exercised on every merge."

---

## Definition of done (every phase)
1. Tests pass, CI green.
2. README roadmap table updated (status + a screenshot/gif if visual).
3. Tagged release with notes.
4. Blog post drafted (even if published later).
5. One honest "what I'd do differently" note in the PR.
