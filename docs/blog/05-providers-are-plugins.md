# Providers Are Plugins: Surviving Model Churn With Aliases, Cassettes and a Pricing Hash

> Draft — accompanies the `v0.5.0-providers` release.

Every agent framework I surveyed at the start of this project ships its model integrations
inside the framework. It is the obvious thing to do and it is the thing that ages worst.
Models are retired in six to eighteen months. Vendor SDKs make breaking changes on their
own schedule. A framework whose core imports `openai` inherits every one of those
schedules. The longevity review in `docs/DEVELOPMENT_STRUCTURE.md` called this out as
E3 — "exactly the coupling that dies with each model generation" — and prescribed the
fix before a single provider existed: providers are separate distributions behind an
`Executor` port, discovered by entry point, and the core never imports one.

Phase 4 is where that prescription meets a real model. Two of them, in fact, through two
different wire formats, on a laptop, with no API key.

## The seam

`agentos-provider-openai-compat` is its own package under `providers/`. It speaks the
OpenAI chat-completions format over plain `httpx` — no vendor SDK, because the wire format
is the de facto protocol that Ollama, vLLM, LM Studio, OpenRouter and OpenAI itself all
share, and a documented JSON shape outlives any client library. It advertises itself
through the `agentos.executors` entry-point group. The API and worker composition roots
call `discover_executors()` at startup; the core's import-linter contracts now forbid
`httpx`, the provider packages and the discovery module itself, and a fresh-interpreter
test asserts none of them load when the core does.

An agent chooses its executor by name:

```json
{"name": "poet", "type": "llm", "executor": "openai-compat",
 "config": {"model": "chat.fast", "prompt": "Write a haiku about {run.topic}."}}
```

If the named executor is not installed, the run fails at dispatch with a message that says
which distribution to install and which executors *are* registered. A plugin that fails to
load is skipped with a warning naming it; one broken provider must not take the API down.

The proof that the seam is real came with the second provider.
`agentos-provider-anthropic` speaks the Anthropic Messages API: a different request shape
(top-level `system`, a required `max_tokens`), a different auth header (`x-api-key` plus
`anthropic-version`), a different error envelope including the `529 overloaded_error` no
other API has, and no `response_format`, so asking for JSON means an instruction and some
fence-stripping. It took zero changes to the core and zero changes to the first provider.
The quickstart's poet and critic run on it by changing one field. Ollama happens to serve
this format too, at `/v1/messages`, so the second provider is as free and keyless as the
first — and both providers' cassettes were recorded from the same local model through the
two different doors.

What the second provider did change was the shape of the first. Cassettes, the pricing
table, the error vocabulary, prompt templating and environment parsing were all things a
provider needs and none of them are provider-specific, so they moved into
`dagentos.providerkit` — in the core distribution behind an extra, forbidden to the core
itself, and in the adapter-independence contract. The OpenAI provider lost 150 lines and
kept its behaviour. A third provider is now one executor module.

## Three things the design review wanted, closed

**Aliases (A3).** An agent says `chat.fast`, not `qwen2.5:0.5b`. The provider resolves the
alias through its table — `AGENTOS_OPENAI_ALIASES='{"chat.fast": "gpt-4o-mini"}'` moves
every agent that uses it to a different model with no redefinition. The interesting case is
a run that is *in flight* when the table changes: suspended for a week of approvals, say,
and woken to find its model retired. The engine calls the plugin's `resolve()` before
dispatch and compares it with what the same agent ran on earlier in this run. If they
differ, it appends `executor.substituted{from, to, reason}` before `step.started`, with
the principal `system:<executor>`. Completed steps are never re-executed, so a substitution
only affects future steps, and it is never silent. The test for this uses a retry: the
step fails once, the alias is re-pointed between attempts, and the log shows the
substitution immediately before attempt 2.

**Metered cost with a pricing hash (A6).** Every step's cost carries `input_tokens`,
`output_tokens` and `requests` meters, a Decimal amount, and the SHA-256 of the pricing
table used. At startup the composition root stores each provider's table in the BlobStore,
so `GET /blobs/{hash}` answers "why did this cost that" for as long as the log exists.
Locally served open-weight families are priced at zero and marked `priced: true`; a model
not in the table is priced at zero and marked `priced: false`, which is a different
statement. The Anthropic provider surfaced a wrinkle here that the reference server never
would have: Ollama reports cache reads separately from `input_tokens`, so the first
recording showed a prompt of one token and thirty-two cached. The meter now counts every
prompt token at the full input rate — conservative, since Anthropic charges a tenth for
cache reads — and records the cached portion as its own meter so the discount is
recoverable from the log.

**Cassettes (A10).** Provider tests never touch the network. A small, dependency-free
transport records request-hash → response into a JSON file, with auth headers never
written, and replays from it; a miss tells you exactly which variable to set to re-record.
The committed cassettes were recorded from a real Ollama — each file says so in its
`source` — and a nightly opt-in workflow installs Ollama on a GitHub runner, pulls the
model, re-records both providers, and re-runs the replay tests on the fresh recordings. It
ran green on its first dispatch. When a model is finally retired, the tests still pass on
the last recording, and the nightly job turning red for thirty days is the signal to mark
the plugin deprecated in its own README — not the core's.

## The bar for developer experience

The instruction I was given for this phase was blunt: make it good enough that an engineer
at a large company would pick it up for a pilot. That is a different target from "the
invariants hold", and it changed what got built.

`docs/quickstart-llm.md` is five minutes from nothing to a two-step LLM workflow on your
own machine, with no account anywhere. It is executed by a test against the recorded
cassette so it cannot rot, and it was run as written against live Ollama — separate API
and worker processes, `202` then poll — for both providers. `scripts/quickstart_llm.py`
does the whole thing in one command. `GET /executors` reports, per plugin, whether the
model server is reachable and which aliases it can actually serve, because that is the
first question when a step fails. Every provider error is written to be acted on:
`ollama serve`, `ollama pull no-such-model:1b`, set `AGENTOS_OPENAI_API_KEY`, here are the
template keys you *do* have. I ran the two most likely failures live — server down, model
missing — and both land in the run's log with the fix in the message. Configuration is
typed, every variable has a documented default, and a bad value raises an error naming
the variable rather than a stack trace from somewhere inside httpx.

## What the tests caught

Two things, both from the real model rather than the reference server. The cached-token
accounting above. And a 0.5B model wrapping its JSON in ` ```json ` fences despite being
told not to — the fence-stripping exists because a recording showed it was needed, and the
test that asserts the critic's JSON parses now runs against that recording.

One from the tooling: the two providers' test modules shared a basename, and pytest's
default import mode refused to collect the second. Unique names per provider.

## Next

The carry-forward list is shorter than it was. The executor-input trust boundary (C12, A8)
now has a consumer and is the natural next slice. README screenshots are recordable. The
`tool` agent in core is still owed. The optional UI is Phase 5.
