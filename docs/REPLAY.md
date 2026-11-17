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

## Exporting a run (A11)

Pilot teams score runs in an external evaluator (opik, langfuse) and hand auditors a file.
`agentos export-run` writes that file from the same log replay reads. AgentOS records and
never scores: the file holds the log exactly as stored plus the fold's own summary, and no
metric derived beyond it.

```bash
agentos export-run --run <id> --format jsonl --out run.jsonl     # one run; - (default) = stdout
agentos export-run --all --format jsonl --include-blobs > all.jsonl
```

**Format** (`dagentos/export.py`). JSON Lines, one object per line. Per run, in order:

| line | shape |
| --- | --- |
| header | `{"kind": "agentos.export", "version": 1, "run_id", "exported_at", "chain": {"verified", "sealed_through", "seals", "error"}}` |
| every event, seq order | the dict `GET /runs/{id}/events` returns, `prev_hash` and `hash` included |
| one per referenced blob, only with `--include-blobs` | `{"kind": "blob", "sha256", "bytes_b64"}`; a blob the store no longer holds is `{"kind": "blob", "sha256", "bytes_b64": null, "missing": true}` (and a warning on stderr) |
| summary | `{"kind": "summary", "run_id", "status", "events", "cost", "error"}`, from `fold()` |

- An event line is any line with `event_type`; every other line has `kind` and no `event_type`.
  Classify by `event_type` first: `approval.requested` carries its own `kind` field.
- `chain` is the judgement `agentos verify` makes. `verified` is false when the hash chain
  breaks or a seal is `INVALID`; `seals` is `unsigned | verified | unverifiable | INVALID`;
  `sealed_through` is the highest seq a *valid* seal covers (null without a keyring or seals).
- Inputs and outputs are referenced by hash, as the log already does (`inputs_ref`,
  `output_ref`, `cost.pricing_snapshot_hash`). `--include-blobs` adds each referenced blob once,
  first reference first, between the last event and the summary, so the summary is always a
  run's last line.
- `--all` is **one stream**, oldest run first: each run's lines start with its header (which
  names `run_id`) and end with its summary. One file per run is a shell loop over
  `--run`; a stream keeps a single artifact to hand over.
- Every line is canonical JSON (sorted keys, no whitespace, ASCII), the serialization the
  chain hashes. An event line minus its `hash` field hashes (SHA-256) to that `hash`, and its
  `prev_hash` is the previous line's `hash`, so a reader re-verifies the chain with a standard
  library alone. Exporting the same run twice is byte-identical except `exported_at`.
- An export file is created `0600`, like the SQLite store: it is run data.

**Exit status.** 0 when every exported run verifies. A run whose chain or seals fail is
**still exported**, because the auditor needs the tampered record as stored, with
`chain.verified: false` in its header; stderr names the run and the problem, and the exit is 1.
Its summary then carries `status: null`, `cost: null` and the fold's error when the fold
refuses the log (a broken chain): the file never states a state the engine would not. An
unknown `--run` id exits 2, names the id and writes nothing.

**Redaction.** The export adds none and removes none. Events are stored already redacted where
an executor redacts (the `tool` executor's `${NAME}` values since #137); what the log holds is
what the file holds.

**Versioning.** `version` bumps only on an incompatible change to this shape: a field removed,
renamed or re-typed, or the line order changed. Adding a field, or exporting a new event type
(events evolve under their own `schema_version` and upcasters), keeps `version: 1`, so a reader
ignores fields it does not know.

**Round trip.** `tests/test_export_run.py::test_round_trip_refolding_the_file_equals_the_live_api`
reads a `--all --include-blobs` file back, re-folds each run's event lines, hydrates outputs and
inputs from its blob lines, and asserts the result equals the live `GET /runs/{id}`: an
external evaluator can reconstruct a run's state from the file alone.

Read-only against the store: nothing is appended and nothing enters a run log. Out of scope
here: erasure (A7 crypto-shredding) and importing a file back. A bound on disk is the next
section.

## Archiving runs (E-08): export, then purge

An append-only log has no delete, so disk grows without bound. `agentos archive` is the one
path that removes rows from a store, and it is the export above followed by a purge — never
the reverse, and never for a run that is still live or that fails verification.

```bash
agentos archive --older-than 30d --out ./archive            # dry run (default): what WOULD leave
agentos archive --older-than 30d --out ./archive --yes      # export, check, purge
agentos archive --older-than 90d --status failed,cancelled --out ./archive --include-blobs --yes
```

**Mechanism** (`dagentos.export.archive_runs`, `Store.purge_run`). The run index picks the
candidates — status terminal (`completed`, `failed`, `cancelled`; `--status` narrows), last
updated before now minus `--older-than` — oldest first; nothing is decided on the index. For
each candidate:

1. Re-read the log, judge the chain and seals (`chain_state`), fold it. A run the fold does not
   call terminal, or whose last event is not older than the cutoff, is skipped as `index drift`.
2. Write the A11 export to `<out>/<run_id>.jsonl.part` (0600), fsync, read it back the way an
   auditor will (every line parses, the header names the run, events are seq 1..n, the summary
   agrees), rename to `<out>/<run_id>.jsonl`, fsync the directory. A `.jsonl` in the archive
   dir is therefore always a complete, checked file. `--include-blobs` writes the referenced
   blobs into it, as `export-run` does.
3. Only if `chain.verified` is true: `Store.purge_run(run_id, expected_seq=n)` removes the run's
   events, snapshot, queue and lease rows and the run row in **one transaction**, refusing if
   the log's last seq is no longer `n` (a retry on a failed run appended since). With
   `--include-blobs` the run's referenced blobs are named too, and each is deleted **only if no
   remaining run's stored record contains its sha256** — blobs are content-addressed and shared
   (a pricing table, identical inputs), so the reference check is a scan of the remaining
   records, done inside the same transaction. Without `--include-blobs` no blob is touched.
4. Append one row to `<out>/ARCHIVE.jsonl` (0600, append-only, fsynced): `{"kind":
   "agentos.archive", "version": 1, "run_id", "exported_at", "file", "sha256", "bytes",
   "events", "status", "chain_verified", "purged", "blobs_removed"}`. The manifest is how an
   operator proves what left the store and when; `sha256` is of the file as written.

A run whose chain or seals do **not** verify is exported (step 2) and then **kept**: the store
never loses the only evidence of a tampering. It is listed under `kept`, its manifest row says
`purged: false`, stderr names it, and the exit is 1. Non-terminal runs are never read, exported
or touched — they are listed as `skipped (non-terminal)`. Dry run (the default) makes every
decision and writes nothing: `--yes` performs.

**Afterwards.** The run is simply gone from the store: `GET /runs/{id}` (and `/events`,
`/integrity`) answer 404, `GET /runs` and its `counts` no longer list it, `agentos verify --all`
and `doctor` no longer see it, and its `Idempotency-Key` is released (a client replaying the
original start request would start a *new* run). **The export file is the record**: it re-folds
to the run the API served before the purge
(`tests/test_archive_runs.py::test_yes_exports_then_purges_and_the_file_refolds_to_the_pre_purge_run`).
Keep `<out>` where you keep the database backups. Without `--include-blobs` the file references
inputs and outputs by hash only and the blobs stay in the store.

**Failure ordering** (`docs/FAIL_MODES.md`, "archive" rows). Export fails → nothing purged, no
`.jsonl`, no manifest row. Process dies between the rename and the purge → the file exists,
the run is still in the store, no manifest row claims it left; the re-run exports the same
bytes (same `exported_at` → same sha256) and finishes. A `.jsonl` present with no manifest row
means exactly that window: the purge did not happen (or the row's write did not land) — the
run is either still in the store or the file is its complete record; `agentos verify --run`
tells which.

**Privacy and erasure boundary.** Archive is *retention*, not *erasure*. The rows leave the
store, but the exported file still holds every event, every input and output it referenced
(with `--include-blobs`, their bytes), and the manifest holds the run id. Deleting that file is
an operator act outside AgentOS and leaves no proof. True erasure — a per-run data key in a
`KeyStore` port so that destroying the key makes the stored bytes unreadable while the log stays
append-only and hash-chained, recorded as a `RunErased` event — is ROADMAP **A7** and is still
open. Until then: a deletion request is served by `archive --yes --include-blobs` for the run's
cohort plus deletion of the exported file, and the chain of custody ends at that deletion.

## The run index is derived, never replayed (E-07)

`runs.status` (with `pending_approvals`, `indexed_seq`, `updated_at`; migration 4) is an index
of each run's log, computed by `dagentos.core.run_index.index_events` inside the transaction
that appends the events. The fold never reads it, so replay is exactly as deterministic as
before: drop the columns and every run folds to the same state. It only decides *which* runs
`GET /runs?status=`, `GET /approvals` and the worker's sweep fold; each of them acts on the
fold. `tests/test_run_index.py` pins `index_events` equal to the fold's status and pending
approvals at every cut point of the golden corpus and of a seeded log that reaches every
status, and `agentos doctor` compares the stored rows with the fold on a live store.

## Related

- Executable form of this guarantee over *random* logs: `tests/property/` (Hypothesis) drives
  random DAGs through the engine with random executor outcomes and operator interleavings and
  asserts `fold(events[:k]) == GET /runs/{id}?at=k` at every k, snapshot + tail == full fold,
  and that any single edit/insert/remove of the log is refused (`tests/property/conftest.py`
  documents the strategies).
- Tamper-evidence of those facts: `TRUST_BOUNDARY.md` §2.
- Snapshots (C15, #15): see the section above.
- Taking a run away as a file: "Exporting a run (A11)" above.
- Bounding disk without breaking the log: "Archiving runs (E-08)" above.
