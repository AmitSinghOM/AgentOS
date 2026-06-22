# AgentOS — Build Roadmap

Built in weekend-sized phases. Each phase ends with a **tagged release**, a **demo you
can record in 60 seconds**, and a **blog post**. Ship small, iterate visibly — the commit
history should read like a real project, because it is one.

> Rule: do not start phase N+1 until phase N is demoable and tagged. Earn scope by
> finishing.

---

## Phase 0 — Walking Skeleton  ·  ~1 weekend
**Goal:** the whole pipe is connected end-to-end, even if it does almost nothing.

- [ ] Repo scaffold, `pyproject.toml`, ruff + pytest config, CI green on push
- [ ] `docker compose up` brings up Postgres + Redis
- [ ] FastAPI app boots; `/health` returns OK
- [ ] Agent registry: `POST /agents`, `GET /agents` (in-memory or single table)
- [ ] One workflow definition with a single "echo" agent node
- [ ] `POST /workflows/{name}/runs` runs it synchronously, `GET /runs/{id}` shows result

**Demo:** register echo agent → define workflow → start run → see output.
**Tag:** `v0.1.0-skeleton`. **Post:** "Designing AgentOS: a control plane for agent workflows."

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
