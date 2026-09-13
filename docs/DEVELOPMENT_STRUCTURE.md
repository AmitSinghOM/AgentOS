# AgentOS — Development Structure for a Seven-Year Horizon

Status: adopted 2026-09-13 at Phase 0. This document is the *how we build* companion to
`DESIGN.md` (*what we build*). It exists because the question "will this still run in
2033, after several model generations and possibly after systems most people would call
AGI" has a real answer, and the answer is not a prediction about models. It is a set of
structural rules that make model change irrelevant to the core.

---

## 0. Staff evaluation of the current state (honest)

What is right:

- The five decisions in `DESIGN.md` §4 (event-sourced state, idempotency keys, Postgres
  as truth, approval as a suspended state, one-worker-now-N-later) are the correct
  decisions and are the ones the popular runtimes got wrong (see
  `Study/AGENTOS_LANDSCAPE.md` C1–C15).
- The roadmap rule "earn scope by finishing" is the single most important longevity
  practice in the repo. Keep it.
- Phase 0 shipped small (270 LOC), tested, tagged, with an honest "what I'd do
  differently". That is the habit that keeps a project alive.

What would not survive seven years as written (all fixable now, cheaply):

| # | Finding | Why it matters at year 7 | Fix |
| --- | --- | --- | --- |
| E1 | `core/engine.py` imports the `store` singleton and `agents.echo` directly | The core depends on its adapters; every adapter change ripples into the engine; nothing enforces the layering DESIGN promises | Ports in `core/ports.py`; adapters injected; **import-linter contract in CI** (done in this change) |
| E2 | `WorkflowRun.steps` is a mutable list; no `schema_version` on any persisted object | A 2026 run cannot be replayed by a 2033 build; there is no upcasting path | Event-shaped domain in Phase 1 with `schema_version` on every event and an upcaster registry (§3) |
| E3 | Phase 1 planned "Real LLM agent (Bedrock or OpenAI)" *inside* the core | This is exactly the coupling that dies with each model generation | Providers are out-of-core plugins behind an `Executor` port with a byte-level contract (§2) |
| E4 | Cycle detection duplicated in `models.validate_dag` and `engine._topo_order` | Two sources of truth drift | One implementation in the domain, called by both |
| E5 | CI pins Python 3.11; development happens on 3.14 | Silent drift; the project rots on whichever side is not tested | Matrix `[3.11, 3.14]` (done in this change) |
| E6 | Pydantic and FastAPI types leak into the domain | Both have had breaking majors within five years; the domain would be rewritten with them | Tolerated in Phase 0–1; §1 defines the boundary so the migration is a port swap, not a rewrite |

---

## 1. Layering — the rule that outlives every dependency

```
agentos/
  core/         domain + ports. Imports: stdlib (+ pydantic, tolerated until Phase 2).
                MUST NOT import agentos.api, agentos.store, agentos.agents, agentos.providers,
                or any SDK (openai, boto3, anthropic, redis, sqlalchemy, fastapi).
  store/        persistence adapters implementing core.ports.Store   (memory, postgres, sqlite)
  agents/       step executors implementing core.ports.Executor       (echo, tool, llm bridge)
  providers/    model-vendor bridges, one package per vendor, independently versioned
  api/          transport adapters (FastAPI now; whatever replaces it later)
  observability/ OTel/Prometheus adapters
  worker/       process that pulls the queue and drives core.engine
```

Direction of dependency: everything points *inward* to `core`. `core` points at nothing.
This is enforced mechanically by `import-linter` in `pyproject.toml` and run in CI; a PR
that violates it fails. Documentation does not keep layers honest. CI does.

Why this is the seven-year rule: FastAPI, SQLAlchemy, Pydantic, Redis clients and every
model SDK are *expected* to have breaking changes in seven years. If the domain never
imports them, each one is a contained adapter rewrite. If it does, each one is a
project rewrite.

## 2. Model-agnosticism — how GPT-N and "AGI" become irrelevant to the core

AgentOS is **not the intelligence**. It is the ledger and the governor around the
intelligence: what ran, in what order, with what inputs, producing what effects, costing
what, approved by whom, replayable forever. Every increase in model capability makes that
ledger *more* necessary, not less. That is the positioning that survives.

### 2.1 The Executor port (the only thing a model touches)

```python
class Executor(Protocol):
    def execute(self, req: StepRequest) -> StepResult: ...

@dataclass(frozen=True)
class StepRequest:
    run_id: str; step_id: str; attempt: int
    idempotency_key: str            # run_id:step_id:hash(inputs)
    inputs: bytes                   # opaque to the core; JSON by convention
    budget: Budget                  # max cost, max wall time, allowed effect classes
    deadline: datetime

@dataclass(frozen=True)
class StepResult:
    outputs: bytes                  # opaque to the core
    effects: tuple[Effect, ...]     # every side effect the step claims to have caused
    cost: Cost                      # tokens in/out, currency amount, provider, model id
    provenance: Provenance          # executor name+version, model id, prompt hash
```

Rules:

- The core never inspects `inputs`/`outputs` beyond hashing them. A step may be a 2026
  chat completion, a 2030 agentic loop that runs its own tools, or a 2033 system that
  plans its own sub-workflow. To the core each is one step with recorded effects.
- `effects` is the unit of idempotency. Replay returns the recorded `StepResult`; it never
  calls `execute` again for a completed `(run_id, step_id)`.
- `budget` is enforced by the core, not trusted to the executor: a step that reports cost
  above budget is `DEAD_LETTERED`, and a run whose rolling cost exceeds its ceiling is
  `SUSPENDED` for approval (DESIGN §8 "budget guardrails" becomes Phase 3 scope).
- `provenance` is mandatory so a 2033 reader can tell which model produced a 2026 step.

### 2.2 Providers are plugins, not core

Each vendor bridge is its own distribution (`agentos-provider-openai`,
`agentos-provider-bedrock`, `agentos-provider-anthropic`, `agentos-provider-local`),
discovered via an entry point group `agentos.executors`. They pin their own SDK, release
on their own cadence, and can be archived when a vendor disappears without a core release.
The core ships with `echo` and `tool` (HTTP/subprocess) only.

### 2.3 Dynamic DAGs — the one structural thing to add now for the "AGI" case

More capable agents will propose their own steps. The log must model that without
breaking the invariants: a step may emit a `ChildRunRequested` effect; the core starts a
child run with `parent_run_id` and `parent_step_id`, and the parent step completes only
when the child reaches a terminal state. Every C1–C15 invariant applies per run; budgets
and approval gates are inherited. This is a Phase 2 item, but `parent_run_id` goes into
the `run_events` schema in Phase 1 so it never needs a migration.

### 2.4 Human authority is structural, not a feature

The approval gate (C7), cancel (C5), budget ceilings (§2.1), and the trust boundary on
resume payloads (C12) are the governor. They live in `core`, they cannot be disabled by an
executor, and no executor can mark its own step approved. This is the property that makes
the project *more* relevant as autonomy increases.

## 3. The event log is the public API

Code can be rewritten; a persisted log cannot be un-written. Compatibility is defined on
the log, not on Python signatures.

- Every event carries `schema_version` (integer) and `event_type` (string).
- Events are **append-only and immutable**. Fields are only ever *added*; never renamed,
  removed, or re-typed. A new meaning is a new `event_type`.
- Reading an old version goes through an **upcaster registry** (`v1 → v2 → … → current`)
  in `core/events/upcast.py`. Upcasters are pure functions and are tested against the
  golden corpus (§5.3).
- Serialization is JSON with explicit field names. Never pickled Python, never
  library-specific encodings.
- The store port exposes `append(run_id, expected_seq, events)`, `read(run_id, after_seq)`,
  `snapshot(run_id, seq, state)`, `latest_snapshot(run_id)`. Anything richer is an adapter
  concern.

Compatibility promise (written into `README.md` at v1.0): *any run log written by any
released version can be replayed by any later version.* CI proves it on every PR (§5.3).

## 4. Dependency policy

| Layer | Allowed | Policy |
| --- | --- | --- |
| `core` | stdlib; pydantic (tolerated until Phase 2, then stdlib dataclasses) | zero runtime third-party deps target |
| adapters | one library per concern | pinned in lockfile; upgrade in isolated PRs |
| providers | vendor SDK | separate distribution, separate lockfile |
| toolchain | `uv` lock, container image digests | reproducible builds; `uv lock --check` in CI |

- Python floor moves at most one minor version per year, announced one release ahead.
- No dependency is adopted without an ADR naming its exit strategy.
- Anything with a hosted-service requirement is rejected (C13).

## 5. Testing pyramid

1. **Unit** (`tests/unit/`) — domain logic, upcasters, fold function. Milliseconds.
2. **Contract** (`tests/contract/`) — every `Store` and `Executor` adapter passes the
   same port test suite. Adding an adapter = pointing the suite at it.
3. **Golden replay corpus** (`tests/golden/`) — one recorded run log per released version,
   committed as JSON. Every PR replays every corpus file and asserts the fold is equal to
   the committed expected state. **This is the seven-year test.** A log from v0.2.0 must
   fold correctly in v7.
4. **Chaos** (`tests/chaos/`) — fault points and Toxiproxy per `ROADMAP.md` "Chaos
   engineering plan"; property tests over random fault schedules.
5. **End-to-end** (`tests/e2e/`) — README quick start against compose.

Coverage is not a target; invariant coverage is. Every C-issue closes only when its
acceptance test is in the suite.

## 6. Change management

- **ADRs** in `docs/adr/NNNN-title.md` for every decision that would be expensive to
  reverse. `DESIGN.md` §4 is retroactively ADR 0001–0005; new decisions get their own.
- **SemVer with a compatibility statement per release**: what log versions it reads, what
  Python it runs on, which provider packages it is tested with.
- **Deprecation window**: one minor release with a warning before removal; never remove an
  `event_type` (see §3).
- **Release cadence**: each roadmap phase is a tagged release with notes and a "what I'd do
  differently" entry. Between phases, patch releases only.
- **Kill criteria** (written down so the decision is not emotional): if two consecutive
  years pass with no phase shipped and no external user, archive the repo with a final
  release whose README states what it proved. A clean archive is a good outcome.

## 7. Operability — zero-ops or it dies

- `docker compose up` is the whole product. Add an **SQLite store adapter** in Phase 1 so
  `pip install agentos && agentos serve` works with no infrastructure at all; Postgres is
  the production adapter, not the entry ticket.
- Single binary of truth: the event log. Backup = `pg_dump` (or copy the SQLite file).
- Health, readiness, and a `agentos doctor` command that replays the golden corpus
  against the live store to prove the installation can read its own data.
- No telemetry leaves the host unless configured.

## 8. Bus factor — a project one person can put down and pick up

- `README.md` quick start is a test (already true). `DESIGN.md`, this document, the ADRs,
  and `ROADMAP.md` are the complete written brain; nothing lives only in chat history.
- `make bootstrap` (or `uv sync`) from a clean machine to green tests in under five
  minutes, verified in CI on a fresh runner.
- Every phase ends with a writeup in `docs/blog/`. The writeups are the memory of *why*.

## 9. What this means for Phase 1 concretely

Phase 1 gains these items (mirrored in `ROADMAP.md`):

- `core/ports.py` with `Store` and `Executor`; engine takes both as arguments. **Done.**
- `import-linter` contract: `agentos.core` imports only stdlib + pydantic. **Done, in CI.**
- CI matrix `[3.11, 3.14]`. **Done.**
- Event model with `schema_version`, `event_type`, `parent_run_id`; upcaster registry.
- `StepRequest`/`StepResult` as in §2.1; `echo` and `tool` executors implement the port.
- LLM executor moved to `agentos-provider-*` plugins discovered via entry points; the
  core's Phase 1 demo uses `tool` (HTTP) so no vendor key is needed to run it.
- `tests/contract/` for the store port; `tests/golden/v0.2.0.json` recorded at release.
- SQLite store adapter alongside Postgres.
- ADR 0006 "Core depends on nothing", ADR 0007 "The event log is the public API",
  ADR 0008 "Providers are plugins".

## 10. Confidence

- That layering + log-compatibility + zero-ops are the mechanisms behind long-lived
  infrastructure software — 90%; this is the pattern of SQLite, Postgres, Temporal's
  history model, and Kafka's log-as-contract, not a theory.
- That the Executor byte-level contract absorbs future agentic models — 80%; the
  unknown is whether future systems demand *streaming* multi-turn control that a
  request/response step cannot express. Mitigation: `effects` and child runs (§2.3) give
  a step a way to ask for more steps rather than hold a connection.
- That a solo portfolio project reaches year 7 — low regardless of structure (~20%).
  The structure raises the odds and, more importantly, makes an archive at year 3 a
  finished artifact rather than an abandoned one (§6 kill criteria).
