# Durable Execution: Resuming Agent Workflows After a Crash

> Draft — accompanies the `v0.2.0-durable` release.

Phase 0 connected the pipe. Phase 1 is the reason AgentOS exists: a run survives the
death of the process running it, and no step runs twice. This post is about how, and
about the two bugs the tests caught in my own code while I was building the thing that
is supposed to prevent bugs like them.

## The demo, as a test

```
pytest tests/chaos/test_kill9_real_process.py -v
```

A real worker process starts a three-step run and is hard-killed — `os._exit(137)`, the
exit status SIGKILL leaves, no cleanup, nothing flushed — the instant after step 2 is
committed. A second real process starts, finds the run, and finishes it. The only thing
the two processes share is a SQLite file. The test asserts that step 2 is never even
*started* again, and that the log ends with exactly one completion per step.

That is the whole feature. Everything below is what it took to make that test honest.

## State is the fold of a log

A run is not a row with a `status` column. It is the fold over an append-only sequence
of events: `run.started`, `step.started`, `step.completed`, `step.failed`,
`run.completed`, `run.failed`. Every event carries a `schema_version`, an `event_type`,
and a dense, monotonic `seq` assigned by the store on append.

`fold(events)` is pure and it is the *only* definition of "what state is this run in".
The API, the worker, the chaos tests and the golden replay corpus all call it. It refuses
to fold a log with a gap in `seq`, a log that does not start with `run.started`, or a log
with two completions for the same step. That last refusal is the exactly-once invariant
stated as code: if it ever fires, a writer is broken, and I want to know rather than have
the fold quietly pick one.

Why a log and not a row? Because with a row, a crash between "update status" and "the
side effect actually happened" leaves you unable to tell whether the step ran. With a
log, the answer is whether `step.completed` exists. There is no third state.

## Resume is replay

`Engine.advance(run_id)` re-derives everything from the log, walks the DAG in
topological order, and skips any step that already has `step.completed`. That one
`continue` is the crash-recovery mechanism. There is no separate "resume" code path,
no checkpoint file, no state machine to reconcile — the second process runs the same
function as the first and the log tells it where to begin.

This is deliberately *not* how Temporal does it. Temporal replays your workflow *code*
against the history and requires that code to be deterministic; the moment you
`asyncio.gather` two activities in a way that resolves in a different order on replay,
you get a non-determinism error. Agent code is the least deterministic code there is.
So AgentOS replays *state*, not code: steps are opaque, their effects are recorded, and
only the scheduler is replayed. A step that returns a random number replays to the
recorded number.

## Two processes, one run: leases with fencing tokens

Crash recovery is easy if exactly one process ever touches a run. Making that true
across N workers is the actual distributed-systems problem, and the popular agent
frameworks mostly do not try: Microsoft's Agent Framework runs all executors in one
process; mastra keeps live sessions in an in-memory `Map` that a second pod cannot see.

AgentOS gives each run a **lease** with a **fence**: a strictly increasing integer per
run that every acquisition bumps. The worker passes its fence to every append, and the
store rejects any append whose fence is lower than the highest it has seen. So a worker
that stalls past its TTL, loses the lease to a second worker, and then wakes up and tries
to write is refused — not because its `seq` is stale (it might not be), but because its
fence is. The lease turns "probably one writer" into "provably one writer".

The queue, by contrast, is deliberately weak: at-least-once delivery of run ids, with a
visibility timeout. It may deliver a run twice, late, or to two workers at once. All of
that is safe, because none of the exactly-once properties come from the queue. They come
from the log, the lease and the fence. (This is also why Redis went from "the queue" in
the original design to "an optional adapter": the queue is a port with SQLite and
Postgres implementations, so `pip install agentos` needs no infrastructure at all.)

## Chaos, deterministically

Netflix's Chaos Monkey terminates random instances in production. That is the right idea
and the wrong tool for a one-worker project: it needs a fleet to hunt in and knows
nothing about where my commit boundaries are. What carries over is the discipline —
state a steady-state hypothesis, inject a fault, check the hypothesis.

So the engine and worker call `faults.at("<point>")` at every boundary where a crash
would be interesting: `after_lock_acquire`, `before_effect_commit`,
`after_effect_commit`, `after_run_commit_before_ack`. Production binds a no-op. The chaos
suite binds an injector that raises `Crash` — a `BaseException`, so no `except Exception`
in the engine can swallow it, exactly like SIGKILL — or calls `os._exit` for the
real-process test. After every fault, five invariants are asserted: dense monotonic
`seq`; exactly one completion per step; `started` precedes `completed`; no run is left
`RUNNING` without a live lease holder; and the fold of a JSON round trip equals the fold
of the originals.

Then one network fault that no code hook can produce, because it is about *time*: Toxiproxy
sits between worker A and Postgres, and two seconds of latency are injected while A is
mid-step, so A's lease lapses; worker B takes over on a direct link and finishes; and the
test asserts that A's late write was rejected *by the fence* — it reads the rejection
reason, because a rejection by `seq` would have been a coincidence, not a guarantee.

## The two bugs the suite caught in my own code

I want to be specific about these, because "the tests caught bugs" is only meaningful
with the bugs attached.

1. **Fences reset on release.** The SQL adapters implemented `release()` as `DELETE FROM
   leases`. The fence counter lived on that row. So a released-then-reacquired lease
   started again at fence 1 — *lower* than a stale holder's fence 2 — and the new,
   legitimate worker would have been refused while the stale one was allowed. The
   contract test "fences strictly increase across release and expiry" failed on SQLite
   and Postgres and passed on the memory adapter, whose counter happened to live in a
   separate dict. Fix: release *expires* the row; the row is never deleted while the run
   exists.

2. **Fence recorded on first write, not on acquire.** I originally recorded the highest
   fence when a worker *appended*. That leaves a window: B acquires the lease (fence 2),
   and before B's first append, stale A (fence 1) appends. Nothing had told the store
   about fence 2 yet, so A's write would have landed. Designing the Toxiproxy test is
   what made the window visible. Fix: `acquire()` records the fence immediately; from
   that instant, any lower fence is stale whether or not the new holder has written.

Both were caught by tests written *before* the crash test passed, which is the point of
writing the contract before the adapter.

## What I would do differently

The Phase 0 README's `curl -d @file` lines shipped without a `Content-Type` header and
returned 422. Trivial, but it means the quick start had never been run as written. It is
a test now (`tests/test_quickstart.py`), and so is everything else in this post.

## Next: Phase 2

Parallel branches, retries with backoff, a dead-letter state with the cause in the log,
first-class cancel and pause, and the remaining Toxiproxy scenarios (partition
mid-run, latency under fan-out). And the thing Phase 1 deliberately did not touch: the
`Executor` port still hands a step a dict and gets a dict back. Phase 2 widens it to
`StepRequest`/`StepResult` with *declared* effects — so the approval gate can refuse a
step before it acts, not audit it afterwards.
