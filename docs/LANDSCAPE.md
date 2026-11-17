# Landscape survey — public extract

`ROADMAP.md` and `docs/DEVELOPMENT_STRUCTURE.md` cite a survey, `AGENTOS_LANDSCAPE.md`
(2026-09-13), that lives in a private workspace and is not in this repository. This page is a
**redacted public extract**, added 2026-11-16 so those citations resolve. It contains only what
the ROADMAP and the `landscape-con` GitHub issues already state; the per-runtime issue
citations, the mentor read and the cost estimates stay in the private note.

The survey compared LangGraph, agno, Microsoft Agent Framework, crewAI, OpenAI Agents SDK,
Google ADK, mastra, Temporal, DBOS and Hatchet, and reduced what they get wrong to fifteen
cons, each cited to an open issue or a documented rule in that runtime.

## §4 — The cons (C1–C15)

Each con became a GitHub issue labelled `landscape-con`; an issue closed only when its
acceptance test was in the suite. All fifteen are closed (`gh issue list --label landscape-con
--state all`).

| Con | What the popular runtimes get wrong | Closed | ROADMAP phase |
| --- | --- | --- | --- |
| [C1](https://github.com/AmitSinghOM/AgentOS/issues/1) | Step effects recorded before ack; replay never re-executes user code | 2026-09-13 | 1 |
| [C2](https://github.com/AmitSinghOM/AgentOS/issues/2) | Idempotent enqueue and event append (at-least-once without duplicates) | 2026-09-13 | 1 |
| [C3](https://github.com/AmitSinghOM/AgentOS/issues/3) | Replay by stable step id and workflow version, never by position | 2026-09-13 | 1 |
| [C4](https://github.com/AmitSinghOM/AgentOS/issues/4) | Event per step transition; cancellation is a persisted event | 2026-09-13 | 2 |
| [C5](https://github.com/AmitSinghOM/AgentOS/issues/5) | First-class cancel and pause; client disconnect never changes run state | 2026-09-13 | 2 |
| [C6](https://github.com/AmitSinghOM/AgentOS/issues/6) | Separate API and worker with a per-run lease; no process-local live state | 2026-09-13 | 1 |
| [C7](https://github.com/AmitSinghOM/AgentOS/issues/7) | Approval is a run state; a gated step cannot execute before its approval event | 2026-09-13 | 3 |
| [C8](https://github.com/AmitSinghOM/AgentOS/issues/8) | Per-step token and cost accounting with vendor-free observability | 2026-09-14 | 3 |
| [C9](https://github.com/AmitSinghOM/AgentOS/issues/9) | One execution model: every DAG node is a durable step | 2026-09-13 | 2 |
| [C10](https://github.com/AmitSinghOM/AgentOS/issues/10) | Persistence behind a port: one driver, configurable schema, typed serialization | 2026-09-17 | 6 |
| [C11](https://github.com/AmitSinghOM/AgentOS/issues/11) | Dead-letter state for poison steps with cause and a replay path | 2026-09-13 | 2 |
| [C12](https://github.com/AmitSinghOM/AgentOS/issues/12) | Trust boundary on resume payloads and replayed events | 2026-09-14 | 5 |
| [C13](https://github.com/AmitSinghOM/AgentOS/issues/13) | Stay MIT; docker compose is the whole product | 2026-09-15 | 0 (test in 6) |
| [C14](https://github.com/AmitSinghOM/AgentOS/issues/14) | Event-sourced state replay, not replay-the-code determinism | 2026-09-15 | 6 |
| [C15](https://github.com/AmitSinghOM/AgentOS/issues/15) | Append-only event log; snapshots are a bounded optimization, never the source of truth | 2026-09-17 | 6 |

## §5 — Which layer AgentOS competes in

Three layers are all called "the production LLM handler for agents": hosted agent runtimes
(Bedrock AgentCore, Azure Foundry, Vertex Agent Engine), agent SDKs / inner harnesses
(LangGraph, OpenAI Agents SDK, PydanticAI, ADK, crewAI, Strands), and durable execution /
outer harnesses (Temporal, Restate, DBOS, Hatchet, Inngest). AgentOS competes only in the
third. The property-by-property comparison, re-verified against primary sources on
2026-09-17, is the table in `ROADMAP.md` "Landscape check — 2026-09-17"; the three properties
no competitor had (declared-then-do with typed principals, state replay instead of code
replay, verified derived state) are the reason the core was kept as it is.

## §8 — Control-plane comparison

Whether an agent *control panel* (KiroCrew) is the same product as AgentOS, and what AgentOS
should absorb from it, is answered in `ROADMAP.md` "Control-plane check — 2026-09-19":
different layer, they compose; the ranked absorb table there became Phase 8. The reverse
direction (what a control panel could take from AgentOS: declared-then-do effects, state
replay, cost as a governed quantity) is only summarised there; the full version is in the
private note.
