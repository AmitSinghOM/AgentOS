# Designing AgentOS: A Control Plane for Agent Workflows

> Draft — accompanies the `v0.1.0-skeleton` release.

Chaining a few LLM calls in a notebook is a demo. Running those chains reliably is a
distributed-systems problem. This series builds AgentOS — a control plane that executes
agent workflows durably — and writes up each hard part as I hit it.

## The thesis
The interesting engineering in "agentic" systems isn't the prompt. It's everything
around it: what happens when the process dies mid-run, when a model call times out, when
a step needs a human to say yes, when finance asks why a run cost $2.10. Those are
durability, idempotency, retries, suspended execution, and observability — the same
problems any serious backend faces, concentrated.

## Phase 0: the walking skeleton
Before any of the hard parts, I wanted the whole pipe connected: an API that registers
agents, defines a workflow as a DAG, runs it in topological order, and returns the
result. No durability yet — just proof the shape is right and the seams are in the
right places.

Two decisions I made now to avoid pain later:
1. **The store is an interface, not Postgres-or-bust.** Phase 0 uses an in-memory store
   behind the exact method surface the engine will use forever. Phase 1 swaps in Postgres
   without touching the engine.
2. **The engine already thinks in a DAG**, even though Phase 0 workflows are tiny. The
   topological scheduler is the thing that makes parallel branches (Phase 2) a small
   change instead of a rewrite.

## Next
Phase 1 is the one that matters: event-sourced run state so a run survives `kill -9` and
resumes without re-running a completed step. That's the post I'm most looking forward to.
