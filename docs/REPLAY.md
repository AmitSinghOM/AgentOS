# Replay: state, not code (C14)

*Design note. Landscape con: Temporal sdk-python #1578 / #1591 — local activities resolve
in completion order the first time and in sequence order on replay; an `asyncio.gather` in
workflow code trips the non-determinism detector; a changed line of workflow code can make
an in-flight workflow un-resumable. Requirement: steps are opaque; effects are recorded;
only the scheduler is replayed; agent code may be non-deterministic.*

## The decision

Durable-execution engines come in two families. **Code replay** (Temporal, Durable
Functions, Restate) re-executes your workflow function from the top on every resume,
feeding recorded results back to each call so the function *deterministically* re-arrives
at the point where it stopped. It is powerful — arbitrary code becomes durable — and it is
why those systems forbid randomness, time, threads and most control-flow changes in
workflow code: any divergence between the original execution and the replay is a
non-determinism error, and a deploy that reorders two awaits can strand every in-flight
workflow.

AgentOS does **state replay**. The workflow is data (a DAG in `WorkflowDefinition`), not
code. The engine's only job is to fold the event log into `WorkflowRun` state and ask "which
nodes have every dependency completed and are not completed themselves?" — that question
is a pure function of the log and the definition, and it is the *only* thing that is ever
replayed. Everything an executor does is opaque: it receives inputs, it returns an output,
effects, a cost and provenance, and the engine records them. It may call `random()`, read
the clock, spawn threads, call a model that answers differently every time. None of that is
replayed, because none of it is ever re-run: a completed step's `step.completed` event is
the durable fact, and resumption reads the fact instead of re-deriving it.

## What follows

- **Agent code is free.** LLM calls are non-deterministic by nature; a code-replay engine
  has to quarantine them into activities and forbid them in workflow code. Here every step
  *is* the activity, and there is no workflow code to protect.
- **Deploying new agent code never strands a run.** Runs pin the *agent version* they
  started with (`agent_versions` on `run.started`, C3), so a redeploy does not change a
  running workflow's behaviour; but nothing about the new code's *shape* matters, because
  the engine never compares an execution to a recording. `test_agent_code_may_change_between_attempts_without_breaking_replay`
  resumes a run with a different executor implementation that takes a different code path
  and rolls extra randomness; the run completes.
- **The scheduler itself must be deterministic**, and it is small enough to be: given the
  same log and definition it produces the same ready set. Concurrency inside a wave does not
  matter — the wave settles every step before deciding run fate, and the settle order is
  fixed. That is the one place a code-replay-style bug *could* live, and it is covered by
  the chaos suite (crash at every commit boundary; the same step never runs twice).
- **The cost is expressiveness.** Dynamic control flow in workflow code — loops that decide
  on the fly, recursion — is not available. The design answer is dynamic DAGs
  (`ChildRunRequested`, DEVELOPMENT_STRUCTURE §2.3): a step may *request* a child run, and
  the request is an event the scheduler acts on, so it stays in the replayable state rather
  than in code.

## The acceptance test

`tests/test_replay_semantics.py::test_a_random_step_replays_to_the_recorded_value_not_a_new_roll`:
step `a` returns `random.random()` and completes; step `b` crashes; a **fresh engine over
the same store** resumes the run. `a` is not re-executed (the executor's call log shows it
ran once), `b`'s inputs are `a`'s recorded roll, and folding the finished log a third time
yields byte-identical state including that roll. A code-replay engine would have
re-executed `a`'s function and, on seeing a different random value, raised
non-determinism. Here there is nothing to disagree with: the roll is a fact in the log.

## Snapshots (C15)

A snapshot is the folded `WorkflowRun` at seq N, stored with `last_hash`. The engine takes
one whenever a run's log has grown `snapshot_every` events (default 200) past the previous
snapshot, and reads by folding the snapshot plus the events after N (`fold_from`), so a
resume touches at most `snapshot_every` events regardless of run length. The acceptance
test runs a 1,000-step chain: each `step.completed` record is the same size as the first
(the log is appended, never rewritten), a fresh engine reads ≤ 100 events to reconstruct
the run, and the full fold of the log agrees with the snapshot fold exactly.

Never the source of truth: the snapshot's anchor event (seq N with `last_hash`) must exist
in the log, and the first event after it must chain to it (C12); a snapshot whose anchor is
missing, whose hash is not the log's at that seq, or whose tail does not chain is ignored and
the whole log folded instead — including when nothing follows it. `GET /runs/{id}/integrity`
always folds from seq 1. A snapshot with a *consistent* anchor but tampered state would be
believed by `fold_from` — it is a cache with the same trust level as the store itself; the
full fold is the audit (`TRUST_BOUNDARY.md` §2). Writing one can never fail an advance (a
cache error is logged; the log is already committed) and `put_snapshot` is monotonic per run,
so a worker that lost its lease cannot regress the live worker's newer snapshot. The knob is
`AGENTOS_SNAPSHOT_EVERY` (default 200; 0 disables).

Finding along the way: the acceptance test exposed an O(n²) in the engine — a per-wave
refold of the whole log added in Phase 3 — which made a 1,000-step run take 17 s. Removing
it (the state the next wave needs is tracked locally) brought it to under half a second.

## Related

- Tamper-evidence of those facts: `TRUST_BOUNDARY.md` §2.
- Snapshots (C15, #15): see the section above.
