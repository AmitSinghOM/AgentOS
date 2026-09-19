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
- [x] Real tool agent (HTTP/subprocess) in core — landed Phase 6 (`agentos/agents/tool.py`); LLM executors live in provider plugins
- [x] Idempotency keys on step execution (`run_id:step_id:sha256(inputs)`); completed steps replayed from the log, never re-executed
- [x] Per-run lease with **fencing tokens** (`Lease` port; a stale holder cannot append) — SQLite/Postgres/Memory adapters, one contract suite
- [x] State snapshots to bound replay cost — landed in Phase 6 (`v0.7.0`), log stays the source of truth
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
- [x] **C10** — persistence behind a port; typed serialization round-trips — closed Phase 6 ([#10](https://github.com/AmitSinghOM/AgentOS/issues/10))
- [x] **C14** — event-sourced *state* replay, not replay-the-code determinism; `docs/REPLAY.md` — closed Phase 6 ([#14](https://github.com/AmitSinghOM/AgentOS/issues/14))
- [x] **C15** — append-only log; snapshots bounded, never the source of truth — closed Phase 6 ([#15](https://github.com/AmitSinghOM/AgentOS/issues/15))

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
- [x] Grafana + Jaeger + Prometheus added to compose with provisioning; compose CI job checks all three are up; screenshots in README landed in Phase 5 (2026-09-14) once a real provider existed
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

## Phase 4 — Providers + developer experience  ·  ~2 weekends  ·  ✅ shipped `v0.5.0-providers` (2026-09-14)
**Goal:** a real model in five minutes with no API key, from a package a stranger would
adopt for a pilot. Providers are plugins (§2.2); the core still ships `echo` only. **Met:**
two providers, two wire formats, zero core changes between them; quickstart executed by
test and run live for both.

- [x] **First provider plugin** `agentos-provider-openai-compat` (`providers/openai-compat/`, its own distribution, MIT): any OpenAI-compatible chat-completions server over plain `httpx` — Ollama zero-config default, vLLM, LM Studio, OpenRouter, OpenAI. No vendor SDK: the wire format is the contract
- [x] **Entry-point discovery** (`agentos.executors` group) in the composition roots (`agentos/plugins.py`); a broken plugin is skipped with a warning naming it, never fatal; `Agent.executor` routes by name; a missing executor fails the run with the install hint. `GET /executors` shows each plugin's `describe()` + `health()` (server reachable? aliases available?)
- [x] **A3 capability aliases + `executor.substituted`**: `chat.fast` → concrete id via the plugin's `resolve(req)`; a changed resolution mid-run is recorded before the next step, with principal `system:<executor>`; completed steps never re-run
- [x] **A6 closed**: metered `Cost` from real token usage; Decimal amounts; pricing table content-addressed, stored in the BlobStore at startup, fetchable at `GET /blobs/{sha256}`; local open-weight families priced 0, unpriced models marked `priced=false`
- [x] **A10 cassettes**: dependency-free JSON record/replay transport; committed cassettes recorded from a live Ollama (`source` in each file); nightly opt-in `provider-live` job installs Ollama on a runner, re-records, re-runs the replay tests, uploads the artifact
- [x] **Run inputs**: `POST /workflows/{name}/runs {"inputs": …}` stored by hash (`run.started.inputs_ref`), visible to every step as `{run.…}`; workflows without inputs are byte-for-byte unchanged (golden logs still fold)
- [x] **DX bar**: `docs/quickstart-llm.md` (five minutes, no key) executed by `tests/test_quickstart_llm.py` against the recorded cassette AND run end-to-end against live Ollama with separate API + worker; `scripts/quickstart_llm.py` does steps 3–5 in one command; every provider error message says what to do (`ollama serve`, `ollama pull x`, set `AGENTOS_OPENAI_API_KEY`, available template keys); typed config with documented env defaults; provider README
- [x] README screenshots (Jaeger span with `gen_ai.*`, Grafana) — done in Phase 5
- [ ] ⏭ C12 executor-input trust boundary: tool results are data, never prompt (`agentos/protocols/`, A8)
- [ ] ⏭ A11 `agentos export-run --format jsonl`
- [x] **Second provider** `agentos-provider-anthropic` (Anthropic Messages wire format; Ollama's `/v1/messages` as the free default) — proved the seam: different request shape, auth header, error envelope (529), no `response_format`; zero changes to the core or the first provider. Shared pieces extracted to `agentos.providerkit` (cassettes, pricing, errors, templates, env config, conformance scenarios); the OpenAI provider refactored onto it
- [x] Real `tool` agent (HTTP/subprocess) in core — landed after Phase 5 (v0.7.0 slice)

**Tag:** `v0.5.0-providers`. **Post:** "Providers Are Plugins: Surviving Model Churn With Aliases, Cassettes and a Pricing Hash."

**Carry-forward decision (2026-09-14).** Phase 4 is tagged with its goal met. The
executor-input trust boundary (C12/A8) now has a consumer — two of them — and is the
natural first slice of whatever comes next, ahead of the optional UI; it was not started
here because the phase's DX target was already large and the boundary deserves its own
design note (tool results as data, never prompt; a `protocols/` adapter layer). README
screenshots wait for a moment with Grafana open and a real run behind it — they are
recordable now, which they were not before this phase. The `tool` agent, A11 export and
A12 global pause carry unchanged; none is blocking anyone. Every ⏭ item remains a
checkbox, not a deletion.

**What I'd do differently:** extract `providerkit` *before* writing the first provider,
not after the second. The refactor was cheap, but designing the shared layer first would
have made the first provider smaller on day one and the conformance scenarios the
starting point rather than a by-product.

## Phase 5 — Trust boundary + polish  ·  ~1 weekend  ·  ✅ shipped `v0.6.0-trusted` (2026-09-14)
**Goal:** close the last structural landscape con and make the README show what the
system looks like running. **Met:** C12 closed with three tested boundaries; screenshots
from a real two-process run, which also exposed and fixed cross-process observability.

- [x] **C12 trust boundary** ([#12](https://github.com/AmitSinghOM/AgentOS/issues/12), `docs/TRUST_BOUNDARY.md`): control payloads are `principal` + `reason` and nothing else (`extra="forbid"`, 422 + logged, no event, no step); every appended event carries `prev_hash`/`hash` computed in the core, `fold()` verifies the chain (edit/insert/remove → `FoldError`, `GET /runs/{id}` 500, `GET /runs/{id}/integrity`), pre-v0.6.0 logs fold as before; model inputs delimited as `<input name=…>` with `DATA_BOUNDARY` in every system prompt (both providers); structural test that a step output cannot choose the next step, executor or effect class
- [x] README screenshots: Jaeger span with `gen_ai.*`, Grafana dashboard, from a real two-process run (`docs/images/`). Taking them exposed and fixed a real defect: the worker's observers never saw `run.started`, so Prometheus labelled everything `workflow="unknown"` and Jaeger got orphan step spans. Now: trace/run-span ids derive from the run id, observers resolve run facts from the store, run-level happenings are child spans, and the worker serves `/metrics` on `:8001`

- [x] Real `tool` agent (HTTP/subprocess) in core (`agentos/agents/tool.py`, v0.7.0 slice): operator-fixed url/argv, inputs only as query/json values or stdin JSON, `${ENV}` secrets redacted, effects by method/kind so the existing gate governs it, 1 MiB cap, egress guard (link-local never; private opt-in), process-group kill on timeout; results flow through `wrap_input`. Reviewed by the code-reviewer + security-reviewer pipeline (WARNING → fixed). ⏭ `protocols/` function-calling round trip (A8) still open
- [ ] ⏭ A11 `agentos export-run --format jsonl`
- [ ] ⏭ A12 global pause

**Tag:** `v0.6.0-trusted`. **Post:** "Three Doors: Where Untrusted Bytes Meet an Agent Engine."

**Carry-forward decision (2026-09-14).** Phase 5 is tagged with its goal met. The `tool`
agent, A11 export and A12 global pause carry unchanged — none blocks a user of the two
providers, and each is a feature rather than a gap in an invariant. Signing the chain tail
(so the C12 chain becomes a signature) waits for the KeyStore port (A7), where it belongs.
Every ⏭ item remains a checkbox, not a deletion.

**What I'd do differently:** run the two-process deployment as part of CI from Phase 3.
The compose job proved the *services* came up; nothing proved the *telemetry* stitched
across processes, and a single test that feeds one observer `run.started` and another the
rest would have caught it a release earlier.

## Phase 6 — Tools, snapshots, survey close-out  ·  ~1 weekend  ·  ✅ shipped `v0.7.0-complete` (2026-09-17)
**Goal:** close every remaining `landscape-con` issue with an acceptance test, and ship the
one carried-forward item that had become a real cost — bounded replay. **Met:** 15/15
survey issues closed; replay is O(events since snapshot) with the snapshot verified against
the chain; the change set went through a four-seat review and every accepted finding has a
locking test.

- [x] Real `tool` agent (HTTP/subprocess) in core ([#38](https://github.com/AmitSinghOM/AgentOS/pull/38)) — see Phase 5 line for the design; governed by the existing gate with no new mechanism
- [x] **C13** — MIT + open-source compose, now an executable test (`tests/test_c13_mit_and_compose.py`) rather than a claim ([#13](https://github.com/AmitSinghOM/AgentOS/issues/13))
- [x] **C14** — state replay, not code replay: `docs/REPLAY.md` + the random-roll crash/resume acceptance test ([#14](https://github.com/AmitSinghOM/AgentOS/issues/14))
- [x] **C10** — every event type round-trips through every store adapter with equality; bounded reads ([#10](https://github.com/AmitSinghOM/AgentOS/issues/10))
- [x] **C15** — `WorkflowRun` snapshots ([#15](https://github.com/AmitSinghOM/AgentOS/issues/15), [#39](https://github.com/AmitSinghOM/AgentOS/pull/39)): the engine reads a snapshot and folds only the tail; `fold_from` verifies the chain link across the boundary and the snapshot's anchor event against the log even when the tail is empty; `put_snapshot` is monotonic per run on all three adapters so a fenced-out stale worker cannot regress it; a failing snapshot write never fails a committed advance; `AGENTOS_SNAPSHOT_EVERY` (default 200, 0 disables); `fold_from == fold` at every cut point of every golden log. Snapshots are derived state — deleting them is safe; `GET /runs/{id}/integrity` never reads them
- [x] Versioned schema migrations (`agentos/store/migrations.py`) for SQLite + Postgres; every released migration's SQL is SHA-256-pinned per dialect (editing one fails with "add migration N+1"); a pre-ledger v0.6 database adopts the ledger without losing rows
- [x] Unused `sqlalchemy` + `redis` dependencies and the compose redis service removed (queue and lease have been SQL adapters since Phase 1); import-linter still forbids them in the core
- [x] Four-seat review (Staff / Product / Security / CTO) + code-reviewer skill + cqa-analyzer on the snapshot PR: 10 findings, 9 fixed each with a test, 1 cosmetic declined with reason; Design score 8.96 → 8.98
- [ ] ⏭ A8 `protocols/` function-calling round trip
- [ ] ⏭ A11 `agentos export-run --format jsonl`
- [ ] ⏭ A12 global pause
- [ ] ⏭ A7 KeyStore + signed chain tail
- [ ] ⏭ `advance()` cognitive complexity (cqa PY-MAINT-002) — a refactor PR, behaviour pinned by the golden corpus and chaos suite

**Tag:** `v0.7.0-complete`. **Post:** "Snapshots That Are Never the Truth."

**Carry-forward decision (2026-09-17).** Phase 6 is tagged with its goal met: the survey
that scoped this project is fully answered, each answer an acceptance test in CI. The four
⏭ features are unchanged from Phase 5 — none blocks a user of the two providers or the
tool agent. New this phase: `advance()` has grown through five phases of gate → dispatch →
verify → record plus cancel, pause, approvals and now snapshots; cqa flags its complexity
and the review agreed. It is carried as the *next* PR rather than folded into this tag so
the refactor is judged on its own diff against a pinned behaviour set.

**What I'd do differently:** take snapshots in Phase 1 as the design said, even though
replay was sub-millisecond then. The deferral was correct on cost, but the *interface*
(where a snapshot sits relative to the chain, who may write it, what verifies it) is what
the review found gaps in — and interfaces are cheaper to get right before four phases of
callers exist.

## Landscape check — 2026-09-17 (after `v0.7.0-complete`)

The survey that scoped Phases 1–6 (`Study/AGENTOS_LANDSCAPE.md`, 2026-09-13) was re-checked
against primary sources on 2026-09-17. Three layers are all called "the production LLM
handler for agents"; AgentOS competes in exactly one of them.

| Layer | Examples | What was verified today | AgentOS |
| --- | --- | --- | --- |
| Hosted agent runtime | Bedrock AgentCore, Azure Foundry, Vertex Agent Engine | AgentCore docs: sessions are microVMs for up to 8 h and "session state is ephemeral and should not be used for long-term durability" | Not this. AgentOS is what you would run *inside* one. |
| Agent SDK / inner harness | LangGraph, OpenAI Agents SDK, PydanticAI, ADK, crewAI, Strands | Temporal (Aug 2026): "these things are commoditized at this point" | Deliberately not this. Core ships `echo` + `tool`; models are plugins. |
| Durable execution / outer harness | Temporal (+ Agent Harness, pre-preview Aug 20 2026), Restate, DBOS, Hatchet, Inngest, LangGraph checkpointers | See table below | **This is the game.** |

**Layer-3 comparison, verified 2026-09-17.**

| Property | AgentOS `v0.7.0` | Temporal + Agent Harness | LangGraph 1.2 | DBOS / Hatchet |
| --- | --- | --- | --- | --- |
| Source of truth | Append-only hash-chained log; snapshots derived and anchor-verified | Event history, replay-based | Per-superstep checkpoints; docs: they "grow unboundedly … add a cron job to delete" | Postgres step checkpoints |
| Exactly-once step | Idempotency key + fence; real kill-9 test in CI | Yes (activity replay) | User code before `interrupt()` re-runs; side effects must be idempotent (documented rule) | Yes |
| Constraint on step code | None — the *log* is the contract (`docs/REPLAY.md`) | Workflow code must be deterministic | Node re-execution rules | Light |
| Approval | Run state, gated **before dispatch**, `Principal`-typed, human-only for spend/write | Harness: "a seam between the model deciding to use a capability and that capability executing" — same seam, pre-preview | `interrupt()` pattern; #8026 still asks for an approval node | Not first-class |
| Cost | Metered Decimal, pricing-snapshot hash, ceiling → suspension | No | No (LangSmith, commercial) | No |
| Tamper evidence | Chain verified on fold; `GET /runs/{id}/integrity` | No | No | No |
| Cancel | Persisted event + cooperative token | Yes | `RunControl.request_drain()` is process-local, never preempts a running node | Partial |
| **Streaming** | **None** (no SSE/WebSocket endpoint) | Yes | Yes | Yes |
| Users / proof | 0 stars, 1 maintainer, 72 commits | $12.55B valuation on this thesis | 41k★ | Production |

**What this changes in the plan.** Two gaps are disqualifying for anyone evaluating AgentOS
for a pilot, and both are cheap relative to what is built: there is no way to *watch* a run
(every production runtime streams), and there is no way to bring the agent loop you already
use (Temporal's harness wraps the OpenAI Agents SDK / PydanticAI / Gemini; AgentOS asks you
to model steps as its DAG). The optional UI moved to Phase 8 (and, after the 2026-09-19 control-plane check, to Phase 9); Phase 7 closes these two.

The three properties no competitor has — declared-then-do with typed principals, state
replay instead of code replay, and verified derived state — are exactly the ones that are
hard to retrofit, and are the reason to keep the core as it is rather than chase the
feature table. Full comparison and the mentor read: `Study/AGENTOS_LANDSCAPE.md` §5.

## Phase 7 — Streaming + inner harness  ·  ~1-2 weekends  ·  ✅ shipped `v0.8.0-watchable` (2026-09-18)
**Goal:** a pilot evaluator can watch a run live from any process, and can run the agent
SDK they already use as a step, governed by AgentOS's gate, cost meter and log. **Met:**
the stream is a consumer of the log (verified through separate API and worker processes,
resume via `Last-Event-ID`); an OpenAI Agents SDK agent runs as one governed step and a
`spend`-declaring step is suspended before the SDK is ever invoked.

- [x] **Run stream** `GET /runs/{id}/stream` (SSE, [#44](https://github.com/AmitSinghOM/AgentOS/pull/44), `agentos/api/stream.py`): a *consumer of the log*, like the
  observers — the API polls `read_events(after_seq)` so it works from any process with no
  in-memory subscription map (mastra #19252 / C6); `id` = `seq`, `Last-Event-ID` / `?after=` resume;
  closes on the terminal event (also when the resume point is already at/past it — a client
  resuming at the terminal seq would otherwise wait out the full bound, caught in test);
  keep-alive comments every `AGENTOS_STREAM_KEEPALIVE_SECONDS`; `AGENTOS_STREAM_POLL_SECONDS`,
  `AGENTOS_STREAM_MAX_SECONDS` (3600) from env; `data` is byte-identical to `/events`
  (`ensure_ascii=False`, pinned with a non-ASCII fixture). Event-level, not token-level
- [x] **Inner-harness executor** `agentos-provider-openai-agents` ([#45](https://github.com/AmitSinghOM/AgentOS/pull/45), `providers/openai-agents/`):
  runs an OpenAI Agents SDK agent as one AgentOS step — the SDK owns the loop, AgentOS owns
  the gate, cost and log. Tools are operator-registered Python (`agentos.openai_agents_tools`
  entry point at the time; now the shared `agentos.tools` group, see the PydanticAI item below), each with an effect class; the model is offered only tools whose class the
  AgentOS agent *declared* (the rest withheld and listed in the output); tool calls and usage
  land in provenance/cost; a step declaring `spend` is suspended before dispatch by the
  existing tier-2 gate (proven through the core: `run.started, approval.requested,
  run.suspended`, no `step.started`). Each step runs on its own `asyncio.run` loop and closes
  its `AsyncOpenAI` client in `finally` (review finding: `Runner.run_sync` leaves the thread
  loop and pool open). SDK `needs_approval` interruptions raise, never auto-approve. Tested
  against the SDK's `ScriptedModel` (no network); live via Ollama; core forbids importing `agents`
- [x] **Boundary echo fix** ([#46](https://github.com/AmitSinghOM/AgentOS/pull/46)): `DATA_BOUNDARY` is appended last, so a small model at
  temperature 0 completed it as the task — `qwen2.5:0.5b` returned the boundary sentence as
  its "poem" for 7/20 topics incl. the quickstart's default. Framed as "Note on the input
  format: …" → 1/20; measured by `scripts/probe_boundary_echo.py`, frame pinned by
  `tests/test_trust_boundary.py`, cassettes re-recorded live
- [ ] ⏭ Token-level streaming via a `progress(fraction, note)`-style executor hook
- [ ] ⏭ Mid-step suspension on the SDK's `needs_approval` interruptions (requires
  persisting `RunState` as a blob and a resume protocol)
- [x] **PydanticAI inner harness** `agentos-provider-pydantic-ai` (`providers/pydantic-ai/`):
  same shape as the above — one `Agent.run` per step on its own loop, `max_turns` via
  `UsageLimits(request_limit)`, `DeferredToolRequests` (approval / external execution) RAISES
  rather than auto-resolving, errors mapped by `ModelHTTPError.status_code`, client built
  from this provider's config only (never `OPENAI_BASE_URL`, tested). The second harness
  forced the right refactor: the tool registry moved to `agentos.providerkit.tools` with the
  SDK-neutral `agentos.tools` entry-point group (openai-agents still loads its old
  `agentos.openai_agents_tools` group as an alias for one release), `strip_fences` moved to
  `providerkit.prompt`, and a test pins that both harnesses emit the SAME output keys and
  effects on the same scripted trajectory — a workflow switches harness by changing
  `executor`. Tested against PydanticAI's `FunctionModel` (no network); live via Ollama; core
  forbids importing `pydantic_ai`
- [x] **Typed output** `config.output_schema` on both inner harnesses: a JSON Schema object in
  agent config (definition, never input), shown to the model by each SDK (PydanticAI
  `PromptedOutput(StructuredDict)`, Agents SDK `response_format` via an `AgentOutputSchemaBase`
  adapter) and ENFORCED once by `agentos.providerkit.schema` — neither SDK validates the
  contract itself (`StructuredDict` checks only "is an object"). Valid → `output["json"]` +
  `schema_sha256` on `step.completed`; violation → `BadResponse` naming the path, the node's
  retry policy decides (proven through the API: violation, `step.failed`, attempt 2 satisfies).
  Draft 2020-12, root `type: object`, remote `$ref` refused (no fetch), `format` not enforced
  (reproducible), schema in `prompt_hash`. `jsonschema` in the `providerkit` extra only; core
  forbidden list gains it. Example `examples/critic_typed_agent.json`

**Tag:** `v0.8.0-watchable`; `v0.8.1` adds the PydanticAI harness and the shared tool registry (#48). **Post:** "The Stream Is the Log."

**Carry-forward decision (2026-09-18).** Both goal items shipped in the shape the landscape
check asked for. Token-level streaming is deferred because it needs an executor hook that
every provider would have to implement, and the current consumers (the quickstart's
`curl -N`, an evaluator polling a run) are served by event-level frames. Mid-step
suspension is deferred because the honest version needs the SDK's `RunState` persisted as
a blob plus a resume protocol; raising on interruption is the safe default until then. The
Phase 6 ⏭ items (A8, A11, A12, A7) are unchanged.

**What I'd do differently:** run the small-model quickstart under a *topic sweep*, not one
topic, when the C12 boundary shipped in Phase 5. The echo only shows on topics near the
boundary's own vocabulary; "the sea" would never have caught it, "event logs" did — and it
was the default the whole time.

## Control-plane check — 2026-09-19 (KiroCrew, after `v0.8.1`)

The 2026-09-17 check compared AgentOS to other *durable-execution* engines. This one asked a
different question: is an **agent control panel** (KiroCrew — Apache-2.0, one Gateway per
host owning sessions, memory, cron/heartbeat/webhook triggers, approvals, messaging
channels, a dashboard, an operator policy *ceiling* the agent cannot loosen, and an
HMAC-chained security event log with `verify`) the same product, and if not, what does it
have that a FAANG engineer would refuse a pilot without?

**Answer.** Different layer. KiroCrew is the cockpit for interactive and scheduled agent
sessions (layer 1.5 in the 2026-09-17 framing); AgentOS is the flight recorder and governor
underneath declared workflows (layer 3). KiroCrew has task runs with checkpoints but no event
log, no declared effects and no replay contract; AgentOS has runs but no sessions, memory,
triggers, channels or UI. They compose rather than compete — KiroCrew could be the surface
that shows AgentOS approvals; AgentOS could be the durable backend under its task runs.

**What AgentOS absorbs, ranked by "a pilot would be refused without it".** Full table with
costs and the honest reverse direction (what KiroCrew could take from AgentOS: declared-then-do
effects, state replay, cost as a governed quantity) in `Study/AGENTOS_LANDSCAPE.md` §8.

| # | Mechanism KiroCrew has | AgentOS today | Absorb as |
| --- | --- | --- | --- |
| ★1 | Authenticated callers; every decision bound to a principal | `Principal` exists in the model but **nothing authenticates the caller** — any client can POST an approval as `kind=human` | Auth port at the API boundary → `Principal`; the human-only rule for `spend`/`write_external` becomes *enforced*, not asserted |
| ★2 | Operator ceiling (`security_policy.json`, tightest-wins, fail-closed when unreachable) | Agents declare their own effects; budget comes from the run request; no document bounds *all* agents | Operator-owned policy file the worker reads and no API writes; agent definitions can only narrow; `governance.decision` events |
| ★3 | HMAC-chained audit log + `verify` | Per-run hash chain, tamper-*evident* but **unsigned** — DB access can rewrite a run and recompute | Sign the chain tail; `GET /runs/{id}/integrity` reports signature state; `agentos verify` CLI (promotes the Phase 6 ⏭ A7) |
| ★4 | `doctor` / `status` / `policy explain` / `snapshot` | `GET /executors` health block + README runbook | `agentos doctor` (store, migrations, executors, golden fold, chain), `agentos policy explain <agent>`, `agentos snapshot` |
| 5 | Cron / heartbeat / authenticated webhooks | Runs start only via `POST /workflows/{name}/runs` | `trigger` adapter package outside core: cron → `start_run` (idempotency key = slot), webhook → `start_run` |
| 6 | Slack/Discord/Telegram approval buttons | `POST /runs/{id}/approve` only | Notifier port fed from the SSE stream; one Slack adapter proves the shape (needs #1) |
| 7 | Credential redaction before any surface; env scrub for spawned processes | No redaction pass on step output / error text | Redaction pass before append; env scrub list for the `subprocess` tool |
| 8 | OS sandbox around the agent subprocess | `tool` subprocess runs with worker privileges | Optional bubblewrap / `sandbox-exec` wrapper behind a policy ordinal; document the default honestly |
| 9 | Deny-rules + sensitive-path keystone | Operator-fixed argv (stronger for *what* runs), nothing for *where* it writes | Sensitive-path check on subprocess cwd/args and HTTP-derived paths |
| 10 | SHA-256-verified installer, SLSA provenance, multi-arch image, `min_version` pin | Tags + GitHub releases only | PyPI publish with attestations + `ghcr.io` image; `pip install agentos[providerkit]` as the documented path |
| 11 | Every deny audited; per-chokepoint fail-open/closed *documented* | Gate outcomes are events, but no written table | `docs/FAIL_MODES.md`: each chokepoint, which way it fails, the test that pins it. Zero code |
| 12 | Dashboard | Phase UI planned | Unchanged: run visualizer over the stream + approvals inbox |
| 13 | Telemetry that admits what it sends | None | **Skip.** "Nothing leaves" is the differentiator |

Not absorbed (different product): persistent memory/lessons, messaging channels as first-class
surfaces, Apps, multi-harness ACP backend selection, desktop app.

**What this changes in the plan.** Items 1, 2, 3 and 10 are what turn "well-designed durable
core" into something a staff engineer can put behind a controlled edge for a paid pilot; they
become Phase 8. The UI moves to Phase 9 — a dashboard over an API that does not authenticate
its callers would be a screenshot, not a product.

## Phase 8 — Pilot readiness  ·  ~2-3 weekends  ·  ✅ shipped `v0.9.0-pilot` (2026-09-20)
**Goal:** every guarantee AgentOS makes about *who* decided something is enforced at the
boundary, bounded by an operator, verifiable after the fact, and installable by a stranger.
Order follows the control-plane check: the invariant first, the checklist that scopes the
ceiling second, then the ceiling, then the signature, then operability, then distribution.

- [x] **Auth → `Principal`** (#1). `agentos/api/auth.py`: an `Authenticator` protocol
  resolves the request's credential to a `Principal`; in bearer mode request bodies no longer
  *carry* a principal, they *receive* one. `AGENTOS_AUTH=asserted|bearer` (default `asserted`
  = today's behaviour, one startup WARNING that principals are unverified);
  `AGENTOS_AUTH_TOKENS=<file>` of SHA-256 hashes → `{kind, id}` (a plaintext `token` key
  fails startup with the fix; startup fails naming the variable when bearer has no file).
  Middleware, not a per-route dependency, so a route added later is protected by default
  (tested by mounting one after import). Unauthenticated → 401 + `WWW-Authenticate` on every
  path except `/health` and `/metrics`; body `principal` in bearer mode → 422 (rejected, not
  replaced); `agent`-kind token on `POST /agents|/workflows` → 403 before the store; the
  engine's human-only rule still returns 403 on `spend` with nothing appended. Recorded
  principal carries `attestation = token:sha256:<12 hex>`. Rejections logged by hash prefix,
  never the token; accepted calls not logged. Core and worker untouched. 21 tests in
  `tests/test_auth.py`; contract in `docs/TRUST_BOUNDARY.md` §1a. Deferred, with reasons in
  the PR: default `bearer` (v1.0), token-file permission check (policy slice), hot reload
  (restart is the rotation protocol; OIDC is the second authenticator).
- [x] **`docs/FAIL_MODES.md`** (#11): 44 chokepoints across execution, durability, derived
  state and the API boundary, each with a direction (closed / open / not a boundary) and the
  test that pins it; `tests/test_fail_modes.py` fails the build if a cited test disappears, a
  row has no direction, or a row cites the placeholder without being declared unpinned. One
  gap surfaced and declared rather than hidden: `_fail`'s conflict branch (two settles failing
  the run at once) has no test. Doubles as the scoping checklist for the policy ceiling.
- [x] **Operator policy ceiling** (#2): `agentos/core/policy.py`. `AGENTOS_POLICY=<file>`
  (`allowed_executors`, `effect_ceiling`, `always_approve`, `agent_approval_allowed`,
  `max_step_cost`, `max_run_cost`, `max_step_wall_seconds`; `extra=forbid`). Every workflow
  budget is intersected with it by `apply_ceiling` before the gate, the settle checks AND the
  approve path see a `Budget` — tightest wins, a policy can only narrow; the gate itself is
  unchanged. Executors outside the allowlist fail the run at dispatch in the log.
  `governance.policy_applied` follows `run.started` with the policy sha256 and every
  narrowing; the folded run carries `policy_sha256`; replay never consults the policy.
  `GET /policy` shows the ceiling and its hash. Unset → warns; set and bad → startup refuses.
  16 tests in `tests/test_policy.py`; six FAIL_MODES rows. Deferred with reasons: tool-name
  allowlist (needs a `StepRequest` slot both harnesses honour), allowlist check at
  `POST /agents` (UX; dispatch is the invariant), hot reload / central distribution, policy
  signature (goes with #3).
- [x] **Signed chain tail + `agentos verify`** (#3; absorbs Phase 6 ⏭ A7): `agentos/core/seal.py`.
  `AGENTOS_SIGNING_KEYS=<keyring>` (`{"active": "k1", "keys": {"k1": "<64+ hex>"}}`, ≥32-byte
  keys, `key_id` per seal so rotation keeps old seals verifiable). Every idle/terminal event
  (`run.completed|failed|cancelled|suspended|paused`) is followed IN THE SAME BATCH by an
  `integrity.sealed` event: HMAC-SHA256 over `agentos-seal-v1:{run_id}:{seq}:{hash}`; the seal
  is itself chained. No new table, no port change, no migration — stores untouched; the fold
  records `sealed_through` only. `verify_seals` judges signature + that the sealed event still
  carries the hash; states `unsigned | verified | unverifiable | INVALID`; `unsigned_tail`
  reports what no in-log scheme can cover. `GET /runs/{id}/integrity` gains `seals`. Proven:
  rewrite-and-rechain fools the chain and is caught by the seal after the edit. Honest limits
  in TRUST_BOUNDARY §2 (symmetric key, truncation after last seal). Ed25519 is the next signer
  through the same seam.
- [x] **`agentos doctor` / `policy explain`** (#4): `agentos/cli.py`, console script `agentos`
  (`[project.scripts]`). `verify [--run|--all]` exits 1 on any chain or seal failure. `doctor`:
  store reachable, schema at latest migration, every executor's `health()`, auth mode, policy,
  keyring, every run folds and verifies — warnings for unconfigured, FAIL only for broken.
  `policy explain <workflow>`: workflow budget → effective budget → each narrowing → per node
  executor allowed / each declared class runs · asks approval · REFUSED. `--json` everywhere.
  `agentos/store/factory.py` is now the one `AGENTOS_STORE*` reader for API, worker and CLI.
  ⏭ `agentos snapshot` (store + blobs export) deferred to the publishing slice.
- [x] **PyPI + `ghcr.io` image with provenance** (#10). Found first: `agentos` on PyPI is taken
  (agentos.org, import package also `agentos`), so the distribution is **`agentos-durable`**;
  providers depend on `agentos-durable[providerkit]`; import package unchanged (rename = open
  decision, `docs/RELEASING.md`). `publish.yml` on `v*` tags or `workflow_dispatch(dry_run)`:
  build sdist+wheel ×5, `twine check --strict`, fresh-venv install + `scripts/release_smoke.py`
  (version, entry points, `agentos doctor`), SLSA provenance for every artifact
  (`actions/attest-build-provenance`), multi-arch image built FROM THOSE WHEELS by path (never an
  index) and attested on its digest, GitHub release with artifacts + `SHA256SUMS`, PyPI via
  Trusted Publishing only when repository variable `PYPI_PUBLISH=true` (explicit switch; the
  summary says so otherwise). Every action SHA-pinned (the workflow holds `id-token: write`).
  CI gains a `package` job running the same build+smoke on every PR. Local rehearsal caught
  three real defects before any tag: hatch could not select files for a dist whose name differs
  from the import package; the sdist shipped the whole tree incl. a linter cache (193 files →
  ~65); providers had no README metadata so `twine check --strict` failed. Deferred: import
  package rename; PyPI Trusted Publisher setup (Amit's account); `agentos snapshot`.
- [ ] ⏭ Triggers (#5), Slack approval adapter (#6), redaction (#7), sensitive paths (#9),
  sandbox (#8), `agentos snapshot` — after the items above, in that order.

**Tag:** `v0.9.0-pilot`. **Post:** "A Human Said Yes — Prove It."

## Phase 9 — UI (optional)  ·  ~2 weekends
**Goal:** a visual the recruiter screenshot remembers. Plays to frontend strength.

- [x] **React app over `GET /runs/{id}/stream`** — no UI kit yet (SVG + CSS; the kit decision
  waits for the timeline/cost panel). The stream is read with `fetch` + a ReadableStream SSE
  parser (`ui/src/sse.ts`, unit-tested across chunk boundaries, multi-line data, id carry-over)
  because `EventSource` cannot send `Authorization` and the token is never allowed in a URL.
  Reconnects with `Last-Event-ID` after the server's max-duration close; stops on a terminal run.
- [x] **Live workflow run graph** (`/ui/runs/<id>`): layered DAG (longest-path layering, no
  graph library) from `GET /workflows/{name}`; node state derived from the SERVER's folded run —
  the browser never folds events (one derivation of state, with the golden corpus); every stream
  frame lands in a ticker and schedules one debounced refetch of `GET /runs/{id}`. States:
  pending / running (+progress) / completed (+cost) / failed / dead-lettered / cancelled /
  awaiting approval (links to the inbox) / retry backoff — each from one field of the fold.
  States a pinned-version mismatch (C3) instead of drawing the wrong graph. Landing page
  `GET /runs` (new, newest-first, clamped). 19 new component/unit tests + 3 API tests; live
  through separate API and worker incl. resume after Last-Event-ID.
- [ ] Event-log timeline view (time-travel debugging over the log)
- [ ] Cost + latency panel per run
- [x] **Approvals inbox (authenticated)** — Phase 9 opener, the first surface a non-author
  operator touches. `ui/` Vite 5 + React 19 + TS, no UI kit yet (chosen against one screen it
  would be the wrong kit). Served by the API at `/ui` (`agentos/api/ui.py`): same origin so no
  CORS surface; the auth middleware exempts GET/HEAD of static files under `/ui` ONLY, and a test
  asserts no API route lives there and traversal cannot escape the bundle. Token in
  `sessionStorage` (never localStorage / cookie / URL), sent as `Authorization: Bearer`; SSE
  cannot carry headers so the inbox polls `GET /approvals` every 3 s rather than leak a token
  in a query string. New `GET /me` tells the screen exactly what the log will record; the
  banner shows it before any button is enabled. Asserted mode is labelled **Unverified** and
  requires a typed principal. Decision bodies are the API's own (`reason` only in bearer mode);
  401/403/409/422 text rendered verbatim — no client-side authorization. Cost approvals show the
  proposed ceiling. 6 component tests (threat model in the file header), 7 Python mount tests,
  CI `ui` job builds the bundle and runs the mount tests against it.
- [x] **Bundle in the wheel and image.** `scripts/bundle_ui.py` copies `ui/dist` to
  `agentos/_ui` (gitignored; hatch `artifacts` ships it; refuses a partial build; writes a
  VERSION stamp) before `python -m build` in both `publish.yml` (Node step, SHA-pinned) and the
  CI `package` job. `ui_dir()` resolves env → checkout → package, so a developer's build is never
  shadowed. `release_smoke.py` fails a wheel without the bundle or with a mismatched UI version;
  the image smoke asserts `ui_dir()` resolves inside the container. Proven: an empty venv with
  only the wheels served `/ui/runs` and its hashed asset with no checkout present.

**Tag:** `v0.10.0-ui`. **Post:** "Building a Time-Travel Debugger over an Event Log."

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
