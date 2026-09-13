# Retry Without Data Corruption

> Draft — accompanies the `v0.3.0-dag` release.

Phase 1 made a run survive a crash. Phase 2 makes it survive *everything else that goes
wrong on purpose*: flaky steps, poison steps, operators changing their minds, agents
redeployed mid-run, and three branches racing to append to the same log. The theme is the
title: every one of those is a chance to corrupt the record, and the design's job is to
make corruption structurally impossible rather than merely unlikely.

## Waves, and one writer

The scheduler now runs the DAG in waves. Every node whose dependencies are complete
executes concurrently, bounded by `max_parallelism`. Three branches of a fan-out run at
once and the fan-in step waits for all of them — the roadmap demo (1 → [2,3,4] → 5) is a
test that asserts the three branches' wall-clock spans overlap and the fan-in starts after
the last of them ends.

Concurrency is where logs get corrupted, so the rule is simple: one `advance()` call has
exactly one appender. Every write — the scheduler's own, and the `step.progress`
heartbeats from three threads — goes through a single serialized `_Log` that owns
`expected_seq`. When `advance()` returns, the log is closed; a straggler thread that
finishes late finds its append silently dropped rather than landing after a terminal
event. The invariant "the terminal event is the last event" is never violated by timing.

There is one deliberate exception, and it is the most interesting design decision in this
phase. An operator's cancel request has to get *into* the log while a worker is writing to
it. That is two writers. The resolution: control requests (`run.cancel_requested`,
`run.pause_requested`) are the only foreign appends the engine's log will adopt. On a
sequence conflict it re-reads; if every new event is a control request, it advances its
own sequence, remembers the request, and retries once. Anything else — another worker,
a corrupted write — is still a hard conflict. Two writers, but only one of them is ever
allowed to say only two things.

## Retry without data corruption

The phrase means something specific. A retry corrupts data when the *second* attempt
cannot tell what the *first* attempt did. AgentOS answers that with the log:

- An executor exception with attempts remaining appends `step.failed(terminal=False,
  retry_at=…)`. Nothing else. The step's `attempt` counter is in the log, the next attempt
  is `attempt + 1`, and the backoff is exponential and capped —
  `[0, 2, 6, 18, 40, 40]` seconds for a policy of base 2, multiplier 3, cap 40.
- The worker does not sleep. `advance()` hands the run back with the delay, the worker
  re-pushes it to the queue with `delay_seconds`, and moves on. A run waiting on a
  30-second backoff costs nothing.
- The last attempt appends `step.failed(terminal=True)`, then `step.dead_lettered` with the
  cause `failed after N attempt(s): <error>`, then `run.failed`. Poison steps and governor
  refusals (an undeclared effect, a blown budget) share one dead-letter path, so there is
  exactly one place a run can end up when a step cannot be made to work.
- **A completed sibling is never lost.** Every step of a wave is settled — completions
  recorded, failures scheduled — *before* the run's fate is decided. If branch 2 dead-letters
  while branches 3 and 4 complete, the log has all three outcomes, then `run.failed`.
  Google ADK's #6732 is the bug this rule prevents: an approval pause there lost the
  results of parallel siblings, and the user saw an empty reply while the destructive tool
  had already run.

Then the human path. `POST /runs/{id}/steps/{step}/retry` appends `step.retry_requested`
with the `Principal` who asked and why. The fold reopens the run: status back to running,
that step's dead-letter cleared, and — this is the part that matters — attempt numbering
*continues*. The retried step runs as attempt 4, not attempt 1, and every healthy step is
replayed from the log, not re-executed. The log tells the truth about how many times the
world was touched.

## The governor

Between Phase 1 and this one the `Executor` port was widened, and the widening changed
what the engine *is*. It is not just a scheduler; it is a governor with four moves:

1. **Gate.** An agent declares its effect classes (`read`, `compute`, `write_external`,
   `spend`, `send_message`, `execute_code`, `spawn_run`). If the declaration exceeds what
   the workflow's budget allows, the step is refused *before its executor runs*. The
   executor is never called. Declare-then-do.
2. **Dispatch.** The executor receives a `StepRequest` (hydrated inputs, the declared
   effects it may not widen, the budget, a deadline) and a `progress()` callback.
3. **Verify.** The result's reported effects must be within the declaration; its metered
   cost within the step cap; its wall time within the limit. Violations dead-letter with
   the class or the cost named. Nothing the executor says about its own compliance is
   trusted.
4. **Record.** `step.completed` carries effects, cost (Decimal, with a pricing-snapshot
   hash so a 2033 reader can explain a 2026 charge) and provenance (executor, version,
   model). The run's rolling cost is checked *after* recording, so the charge that tripped
   the ceiling is in the log.

`progress()` does three jobs: it renews the worker's lease (so an hour-long step is not
mistaken for a dead worker), it appends a rate-limited `step.progress`, and it is the
cooperative cancellation token — it raises `Cancelled` once a cancel request is seen.

## Cancel and pause

Both are persisted intent, finalized at a boundary. Cancel: heartbeating steps stop at
their next `progress()` and are recorded as `step.cancelled`; a step that never heartbeats
finishes and is recorded as completed; then `run.cancelled`, terminal, last. Pause never
interrupts a step: the current wave settles, then `run.paused`, the run leaves the queue
and the recovery sweep skips it; `resume` puts it back.

If nobody holds the run's lease when a cancel arrives — an idle or paused run — the API
finalizes it immediately, under a lease. If a worker holds it, the API leaves finalization
to the worker. That asymmetry is what guarantees a step that has completed is never
cancelled out from under it.

And the thing Google ADK's most-upvoted issue asks for: nothing ties a run to a client
connection. Disconnecting the stream changes nothing. Only `POST …/cancel` does.

## Agents are versioned, runs are pinned

An agent definition is immutable per `(name, version)`; the store refuses to overwrite a
version with a different body. A run pins the version of every agent it references at
start and resolves against the pin on every attempt, recording `agent_version` on each
`step.started`. Redeploy an agent while a run is half-way through and the second half runs
on the *same* version as the first. A new run picks up the new version. A pinned version
that has vanished fails in the log rather than silently upgrading.

## What the tests caught this time

Honesty section, as before. Three things the suite caught in my own code:

1. **The Toxiproxy race passed for the wrong reason.** The first version asserted that the
   stale worker's write was rejected, and it was — by `seq`, because the healthy worker had
   already finished. That is a coincidence, not a guarantee. The fence is the guarantee.
   The adapters now check fence before seq, and the test asserts on the logged *reason*.
   Then, once heartbeats existed, the stale worker detected its lost lease before ever
   trying to write, and the fence was never exercised. The test is now parametrized: a
   silent executor is stopped by the fence, a heartbeating one by `LeaseLost`. Two
   defence layers, proven independently over a real slow network.
2. **The golden corpus was too strict, then not strict enough.** Adding `effects`, `cost`
   and `provenance` to `step.completed` made the v0.2.0 recording fail an exact-equality
   check on fields that did not exist when it was recorded. The comparator now checks
   "recorded is a recursive subset of folded" — additive fields at any depth are fine, any
   recorded value changing or vanishing still fails — and a unit test proves it bites on
   each failure class. The v0.2.0 log folds under v0.3.0.
3. **A test wiped `sys.modules`.** `test_core_package_imports_no_adapter` deleted every
   `agentos.*` module to prove the core loads alone, and never put them back. Later tests
   held two identities for `StepResult` and failed with the delightful message `returned
   StepResult, not StepResult`. It now restores the module table.

## What I would do differently

Record the fence at `acquire()` from the start (Phase 1's lesson), and design the
Toxiproxy test *before* the fencing code rather than after — the test found two windows the
code had, and both would have been cheaper to close in the design.

## Next: Phase 3

Human-in-the-loop as a first-class state: `SUSPENDED` with an `approval_requests` row and
a `Principal` on the decision; gates on `spend` and `write_external` requiring a human
unless the workflow opts out; the governor's refusals turning from "fail" into
"suspend for approval"; cost ceilings likewise. OpenTelemetry GenAI semantic conventions
and Prometheus. And the two Toxiproxy scenarios carried forward: partition mid-run, and
latency under fan-out.
