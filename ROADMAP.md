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

## Phase 1 — Durable Execution  ·  ~2 weekends   ← the flagship
**Goal:** a run survives a crash and resumes. No step runs twice. This is the demo that
wins interviews.

- [ ] Event-sourced run state: `run_events` table, fold-to-state replay
- [ ] Worker process consuming a Redis run queue (decoupled from the API)
- [ ] Real LLM agent (Bedrock or OpenAI) + a tool agent
- [ ] Idempotency keys on step execution; re-run after crash = no double call
- [ ] Redis execution lock per run (exactly-one-worker advancement)
- [ ] State snapshots to bound replay cost
- [ ] **Chaos test:** kill the worker mid-run; on restart it resumes correctly
- [ ] **C1** — effects recorded before ack; replay never re-executes user code ([#1](https://github.com/AmitSinghOM/AgentOS/issues/1))
- [ ] **C2** — idempotent enqueue and append, `UNIQUE(run_id, seq)` ([#2](https://github.com/AmitSinghOM/AgentOS/issues/2))
- [ ] **C3** — replay by `step_id` + `workflow_version`, never by position ([#3](https://github.com/AmitSinghOM/AgentOS/issues/3))
- [ ] **C6** — separate API/worker, per-run lease, no process-local live state ([#6](https://github.com/AmitSinghOM/AgentOS/issues/6))
- [ ] **C10** — persistence behind a port; typed serialization round-trips ([#10](https://github.com/AmitSinghOM/AgentOS/issues/10))
- [ ] **C14** — event-sourced *state* replay, not replay-the-code determinism; written into DESIGN.md ([#14](https://github.com/AmitSinghOM/AgentOS/issues/14))
- [ ] **C15** — append-only log; snapshots bounded, never the source of truth ([#15](https://github.com/AmitSinghOM/AgentOS/issues/15))

**Demo:** start a 3-step run, `kill -9` the worker after step 2, restart → it finishes
without repeating step 2. Show the event log.
**Tag:** `v0.2.0-durable`. **Post:** "Durable Execution: Resuming Agent Workflows After a Crash."

## Phase 2 — DAG Orchestration  ·  ~2-3 weekends
**Goal:** real multi-agent workflows, not just sequences.

- [ ] Workflow = DAG (nodes + dependency edges); validate acyclic
- [ ] Topological scheduler: run all ready nodes, parallelize independent branches
- [ ] Agent versioning: pin a run to a specific `agent_version`
- [ ] Retry with exponential backoff (Redis-tracked) + max attempts
- [ ] Dead-letter state for poison steps; run fails cleanly with cause in the log
- [ ] Pass outputs along edges (step N output → step M input)
- [ ] **C4** — event per step transition; cancellation is a persisted event ([#4](https://github.com/AmitSinghOM/AgentOS/issues/4))
- [ ] **C5** — first-class cancel/pause; client disconnect never changes run state ([#5](https://github.com/AmitSinghOM/AgentOS/issues/5))
- [ ] **C9** — one execution model: every DAG node is a durable step ([#9](https://github.com/AmitSinghOM/AgentOS/issues/9))
- [ ] **C11** — `DEAD_LETTERED` step state with cause and a retry path ([#11](https://github.com/AmitSinghOM/AgentOS/issues/11))

**Demo:** a fan-out/fan-in workflow (1 → [2,3,4 parallel] → 5) with one branch failing
and retrying.
**Tag:** `v0.3.0-dag`. **Post:** "Retry Without Data Corruption" + "CQRS & Event Sourcing, for real."

## Phase 3 — Human-in-the-Loop + Observability  ·  ~2-3 weekends
**Goal:** production-shaped — pausable, traceable, costed.

- [ ] Approval node: run → `SUSPENDED`, lock released, `approval_requests` row
- [ ] `POST /runs/{id}/approve|reject` resumes or fails the run
- [ ] OpenTelemetry spans per run/step → Jaeger (docker compose)
- [ ] Prometheus metrics: run latency, throughput, error rate, queue depth, retries
- [ ] Token + cost accounting per step, rolled up per run; Grafana dashboard
- [ ] Grafana + Jaeger added to compose; screenshots in README
- [ ] **C7** — approval is a run state; a gated step cannot run before its approval event ([#7](https://github.com/AmitSinghOM/AgentOS/issues/7))
- [ ] **C8** — per-step cost on `step.completed`; Prometheus + OTel only, no SaaS ([#8](https://github.com/AmitSinghOM/AgentOS/issues/8))
- [ ] **C12** — trust boundary on resume payloads and replayed events ([#12](https://github.com/AmitSinghOM/AgentOS/issues/12))

**Demo:** run pauses at approval, approve from another terminal, run resumes; show the
Jaeger trace and the cost breakdown.
**Tag:** `v0.4.0-observable`. **Post:** "Suspended Workflows: Human Approval as a First-Class State."

## Phase 4 — UI (optional)  ·  ~2 weekends
**Goal:** a visual the recruiter screenshot remembers. Plays to frontend strength.

- [ ] React + (Cloudscape or shadcn) app
- [ ] Live workflow run graph: nodes light up as steps complete
- [ ] Event-log timeline view (time-travel debugging over the log)
- [ ] Cost + latency panel per run

**Tag:** `v0.5.0-ui`. **Post:** "Building a Time-Travel Debugger over an Event Log."

---

## Definition of done (every phase)
1. Tests pass, CI green.
2. README roadmap table updated (status + a screenshot/gif if visual).
3. Tagged release with notes.
4. Blog post drafted (even if published later).
5. One honest "what I'd do differently" note in the PR.
