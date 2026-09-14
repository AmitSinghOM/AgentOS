# AgentOS

**A control plane for durable, observable, human-in-the-loop LLM agent workflows.**

Define a workflow as a DAG of agent steps. AgentOS runs it durably — surviving crashes,
never double-running a step, pausing for human approval, and emitting a full trace and
cost breakdown for every run.

The interesting part isn't calling LLMs. It's the distributed-systems layer around them:
**durable execution, idempotency, retries without corruption, suspended workflows, and
observability.** See [DESIGN.md](./DESIGN.md) for the architecture and the decisions
behind it.

---

## Why this exists

A notebook chaining three agent calls works until something real happens — the process
crashes mid-run, a model call times out, a step needs sign-off, or you need to explain
why run #4821 cost $2.10. AgentOS handles those.

## Architecture (at a glance)

```mermaid
flowchart TD
    C[Client] -->|define / start / approve| API[FastAPI API]
    API -->|enqueue| Q[(Redis Queue)]
    Q --> ENG[Workflow Engine<br/>topological scheduler]
    ENG --> ROUTER[Agent Router]
    ROUTER --> LLM[LLM Agents]
    ROUTER --> TOOL[Tool Agents]
    ENG -->|append events| PG[(PostgreSQL<br/>event-sourced state)]
    ENG -.locks / retries.-> R[(Redis)]
    ENG -.traces.-> OTEL[OpenTelemetry → Jaeger]
    ENG -.metrics.-> PROM[Prometheus → Grafana]
```

PostgreSQL is the source of truth (append-only event log). Redis is the speed layer
(locks, retry timers, cache) and is safe to flush.

## Quick start

```bash
cp .env.example .env
pip install -e ".[dev]"
uvicorn agentos.api.main:app --reload      # API (SQLite file ./agentos.db by default)
python -m agentos.worker                   # worker, in another terminal
```

No Docker needed for the default SQLite store. For Postgres:
`docker compose up -d`, then `AGENTOS_STORE=postgres AGENTOS_PG_DSN=postgresql://agentos:agentos@localhost/agentos`
for both processes.

Define and run a workflow (the `Content-Type` header matters — without it curl sends a
form body and the API answers 422):

```bash
# register an agent
curl -X POST localhost:8000/agents -H 'Content-Type: application/json' -d @examples/echo_agent.json

# define a workflow
curl -X POST localhost:8000/workflows -H 'Content-Type: application/json' -d @examples/hello_workflow.json

# start a run: 202 + run id; the worker advances it. Repeating with the same
# Idempotency-Key returns the same run.
curl -X POST localhost:8000/workflows/hello/runs -H 'Idempotency-Key: demo-1'

# fetch the folded state, or the raw event log paged by seq
curl localhost:8000/runs/{run_id}
curl 'localhost:8000/runs/{run_id}/events?after=0'

# a step that exhausted its retries is dead-lettered with the cause; reopen it,
# recording who asked, and the run continues from where it stopped
curl -X POST localhost:8000/runs/{run_id}/steps/{step_id}/retry \
     -H 'Content-Type: application/json' \
     -d '{"principal": {"kind": "human", "id": "amit"}, "reason": "fixed the agent"}'

# operator control: each is a persisted request, finalized at the worker's next
# boundary (or immediately if nothing is running). Completed steps are never lost.
curl -X POST localhost:8000/runs/{run_id}/pause
curl -X POST localhost:8000/runs/{run_id}/resume
curl -X POST localhost:8000/runs/{run_id}/cancel

# human-in-the-loop: a step whose agent declares spend / write_external / send_message /
# execute_code suspends the run BEFORE it runs. Decide from the inbox; the decision and
# who made it are in the log; the step then runs exactly once.
curl localhost:8000/approvals
curl -X POST localhost:8000/runs/{run_id}/approvals/{approval_id}/approve \
     -H 'Content-Type: application/json' \
     -d '{"principal": {"kind": "human", "id": "amit"}, "reason": "within budget"}'

# or run synchronously in the API process (Phase 0 behaviour)
curl -X POST 'localhost:8000/workflows/hello/runs?sync=true'
```

Or run the same walkthrough as a test: `pytest tests/test_quickstart.py`.

### Observability

Telemetry is derived from the event log, so it can be rebuilt from any stored run and the
core imports no telemetry SDK.

```bash
pip install -e ".[observability]"
docker compose up -d jaeger prometheus grafana
AGENTOS_OTEL_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  uvicorn agentos.api.main:app          # and the worker, with the same two variables
```

- Traces: [Jaeger](http://localhost:16686) — one span per run, one per step attempt, with
  OpenTelemetry GenAI attributes (`gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.*_tokens`) plus `agentos.*` (effects declared/reported, cost, approval and
  dead-letter events).
- Metrics: `GET /metrics` on the API, scraped by [Prometheus](http://localhost:9090).
- Dashboard: [Grafana](http://localhost:3000), provisioned — runs/min, error rate, runs
  awaiting approval, total cost, latency p50/p95/p99, step outcomes, retries and
  dead-letters, tokens by agent, approval wait, cost per minute by workflow.

### The crash demo, as a test

```bash
pytest tests/chaos/test_kill9_real_process.py -v
```

A real worker process is started, hard-killed (exit 137) right after step 2 of 3 is
committed, and a second process picks the run up and finishes it. The test asserts step 2
is never started again and the log has exactly one completion per step. Every fault point
in the commit path has a test in `tests/chaos/`; they run on every PR.

## Roadmap

This is built in public, in phases, each ending in a tagged release and a writeup.
See [ROADMAP.md](./ROADMAP.md).

| Phase | Ships | Status |
|-------|-------|--------|
| 0 · Walking skeleton | API + Postgres + Redis up, single-step run end-to-end | ✅ `v0.1.0-skeleton` |
| 1 · Durable execution | Event-sourced state, idempotent steps, crash-resume | ✅ `v0.2.0-durable` |
| 2 · DAG orchestration | Parallel branches, agent versioning, retries + DLQ | ✅ `v0.3.0-dag` |
| 3 · HITL + observability | Approval gates, OpenTelemetry, cost dashboard | ⚪ planned |
| 4 · UI (optional) | React run visualizer over the event log | ⚪ planned |

Phases 1–3 are scoped against a survey of what the popular agent runtimes get wrong
(LangGraph, agno, Microsoft Agent Framework, crewAI, ADK, mastra, Temporal, Hatchet…).
Each gap is a tracked issue labelled `landscape-con`; see [ROADMAP.md](./ROADMAP.md).

## Engineering writeups

Each phase produces an article on a distributed-systems problem solved here:
- Idempotency Beyond API Keys
- Durable Execution: Resuming Workflows After a Crash
- Retry Without Data Corruption
- Suspended Workflows: Human Approval as a First-Class State

(See [`docs/blog/`](./docs/blog).)

## License

MIT
