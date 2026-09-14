# Suspended Workflows: Human Approval as a First-Class State

> Draft — accompanies the `v0.4.0-observable` release.

The frameworks I surveyed before starting this project all have "human-in-the-loop". Almost
none of them have it as a *state*. LangGraph's `interrupt()` is a function you call inside a
node; when you resume, the node re-runs from the top and anything before the interrupt
executes again — the docs tell you to make your side effects idempotent. Google ADK's
confirmation pause lost the results of parallel siblings, and in one issue the destructive
tool had already run while the user saw an empty reply. These are not bugs in the usual
sense. They are what happens when approval is a library pattern layered over an engine that
does not know it exists.

Phase 3 makes the engine know.

## The gate has three tiers

Everything hangs off the declare-then-do gate from Phase 2. An agent declares the effect
classes it may cause; the workflow's budget says what to do with each:

| Declared class is in… | What happens |
| --- | --- |
| `allowed_effect_classes` (default: read, compute) | the step runs |
| `approval_required_for` (default: write_external, spend, send_message, execute_code) | the run **suspends before dispatch** |
| neither | the step is refused, dead-lettered, before dispatch |

"Before dispatch" is the whole point. When `approval.requested` and `run.suspended` are
appended, the gated step has no `step.started` event and has consumed no attempt. Its
executor has not been called. There is nothing to re-run, nothing to make idempotent,
nothing to lose. Ungated siblings in the same wave run and are recorded first, so a
suspension never costs you completed work.

Then the worker releases the lease and the run leaves the queue. The recovery sweep skips
suspended runs. `advance()` on one is a no-op. A human can take a week; the system holds
nothing.

## The decision is in the log, and so is the decider

`approve` requires a `Principal` — `{kind: human | agent | system, id}`. Without one the API
returns 422. For `spend` and `write_external` the principal must be human unless the
workflow explicitly sets `allow_agent_approval`; an agent trying to approve a payment gets a
403. That rule is the difference between "human-in-the-loop" and "some process in the
loop", and it is enforced by the core, not by convention.

When the last pending gate is granted the run is running again and goes back on the queue.
The approved step's request carries the `approval_id`, and the test that matters asserts
that its `step.started` has a strictly higher sequence number than the `approval.granted`
event, and that it ran exactly once. That is the acceptance criterion from the landscape
survey, stated as an assertion.

Rejection reuses the dead-letter path: `approval.rejected`, then `step.dead_lettered` with
the cause `approval rejected by human:amit: too expensive`, then `run.failed`. The existing
retry endpoint reopens the step, and — because the gate is evaluated fresh — the step
re-requests approval rather than running. Timeouts are rejections by the `system` principal,
applied on the worker's sweep. Cancelling a suspended run is immediate, because nobody holds
its lease.

## Money is a gate too

DESIGN.md's "budget guardrails" line became concrete. When the run's rolling cost exceeds
`max_run_cost`, the step that tripped it is *already recorded* — the charge is in the log —
and then the run suspends with a `kind=cost` approval. Granting it raises the effective
ceiling to `total + max_run_cost` (one more budget's worth), and that new ceiling is written
into the grant so the fold knows it; the run can trip again at the raised ceiling and ask
again. Cost approvals are human-only by the same rule as `spend`. Rejecting one fails the
run without a dead-letter: the money is spent, there is nothing to reopen, and pretending
otherwise would be dishonest.

## Observability as a consumer of the log

The second half of the phase is tracing and metrics, and the design choice is the same one
that runs through the whole project: derive it from the log.

An `Observer` port receives every committed event. The OpenTelemetry adapter builds one span
per run and one per step attempt, with start and end times taken from each event's
`occurred_at` rather than from the clock at export time. The Prometheus adapter counts and
buckets the same events. Neither SDK is imported by the core — the import-linter contracts
still hold, and a test spawns a fresh interpreter, imports the core, and asserts no
telemetry module loaded.

Two consequences fall out, and both are tested rather than asserted. First, an observer
that throws cannot affect a run; it is logged and dropped, because telemetry is never the
source of truth. Second, and this is the one I like: **replaying a stored log rebuilds
identical telemetry.** The test runs a workflow live with an OpenTelemetry observer
attached, then feeds the same log to a fresh observer, and compares the two span sets —
names, timestamps, statuses, attributes, span events. They are equal. Same for the metric
samples. If you lose your tracing backend, you have lost nothing.

Attribute names follow the OpenTelemetry GenAI semantic conventions where a concept exists
(`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`) so that 2030 tooling
reads 2026 traces; they are pinned as strings and a test compares them against the
installed semconv package, so an upstream rename shows up as a failure rather than a silent
wire-format change. Everything AgentOS-specific lives under `agentos.*`. Approvals,
suspensions and dead-letters are span events on the run span, with the principal on them.

Compose now brings up Jaeger, Prometheus and Grafana with a provisioned ten-panel dashboard:
runs per minute, error rate, runs awaiting approval, total cost, latency percentiles, step
outcomes, retries and dead-letters, tokens by agent, approval wait, and cost per minute by
workflow. The one metric that is not an event fact — queue depth — is read from the queue at
scrape time and is labelled as such.

## What the tests caught

One this phase, small and instructive. The queue-depth contract test asserted that acking a
run which had been pushed but never pulled removes it. The SQL adapters did that naturally
(one table, one `DELETE`). The memory adapter did not: `ack` only cleared the in-flight heap
and left the run in the waiting deque. Three adapters, one contract, one of them wrong in a
way no existing test had reason to notice until a new observable made the difference count.

## What I would do differently

Ship the `Observer` port with Phase 1. It needed no engine changes beyond the fan-out, and
having spans during the Toxiproxy investigations would have made the lease-expiry race
faster to read than log lines were.

## Next

The carried-forward items across three phases now share a single prerequisite: the first
real provider plugin. Capability aliases and model substitution, provider tests against
recorded cassettes, the protocol adapter layer, crypto-shredding of payloads, export for
external evaluators, the executor-input trust boundary and the README screenshots all need
an agent that actually calls a model. That is Phase 4's first slice, ahead of the optional UI.
