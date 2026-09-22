# Four-seat review of the v0.16.0 range (`6159213..a413d83`)

Seats: Staff development engineer (S), Product engineer (P), Security engineer (Sec), CTO (C).
Range under review: PRs #94–#98 as merged into `main` at `a413d83` (38 files, +2517/−276).
Method: the code-reviewer skill's manifest → per-bundle read of the full file, not the hunk →
anchor → reflection (findings below 80% confidence dropped) → four-seat debate → red-first fixes
→ the same skill run over the fixes themselves → CQA before/after. Each seat was scoped to
surfaces earlier passes had not reached; declined candidates are listed with the evidence that
cleared them so a false positive stays visible as *declined*, not silently dropped.

Working records (manifest, ledger, per-cycle notes, debate transcript, CQA reports) live outside
the repo in the reviewer's workspace; this file is the durable summary.

## Result

| | before | after |
|---|---|---|
| Findings ≥ 80% on the range | 3 (0 critical, 0 high, 1 medium, 2 low) | 0 open |
| Debate items | 14 raised | 4 accepted + fixed, 9 declined with evidence, 1 deferred; +1 accepted from the closing gate (A5) |
| CQA (`cqa-analyzer` 3.4.1, offline) | 8.8 / Excellent, 85 findings, 0 errors | 8.8 / Excellent, **83** findings, 0 new |
| Self-review over the fix commits | — | 1 finding (R1), fixed |

Verdict: **APPROVE**. Nothing in the range was a correctness or security defect in shipped
behaviour; the accepted items are two missing locks on invariants the range introduced, one
stale-invariant fix in the store, and one redundant control.

## Accepted and fixed (each red first, each proven to discriminate)

| id | seat | finding | fix | lock | proof |
|---|---|---|---|---|---|
| A1 | Sec → C | `ApprovalCard.tsx` mirrors `models.HUMAN_ONLY_EFFECTS` and `permission.ts` re-implements `policy.apply_ceiling`'s agent-approval narrowing; the UI states the engine's verdict in words with no test tying the two sides together (`debate2.test.tsx` exercised the sentence against mocked inputs, i.e. the UI's own rule). | One truth table `tests/fixtures/agent_approval_matrix.json` (all six workflow × policy combinations). `tests/test_ui_engine_drift.py` parses the TS `Set` literal and compares it with the engine's, and drives `engine.approve` with a non-human principal through every row; `debate2.test.tsx` renders the card's sentence for the same rows. | `tests/test_ui_engine_drift.py`, `debate2.test.tsx` "matches … row for row" (6 cases) | Mutating the TS `Set` → the constant test fails; restore → 9 passed. vitest 13 → 19. |
| A2 | P + C | `docs/tutorial.md` prints absolute numbers (gate at `seq 5`, `?at=5`, `202`, `422`, the 17-row table) while `tests/test_tutorial.py` pinned only relative order, so the page could go stale with the test green. CQA also flagged the one 62-line test (PY-MAINT-001/003). | Split into fixtures + 9 tests; the seq table is a literal `TUTORIAL_LOG` compared row by row; a prose-pin test asserts the page still quotes each number; the engine is advanced in-process where the page says "the worker" (same `engine.advance` the worker calls; the lease adds no event type). | `tests/test_tutorial.py` (9 tests) | Changing the doc's `run.suspended` row from seq 9 to 8 → the prose test fails; restore → 9 passed. Both CQA hits on the file gone. |
| A3 | S | `SqliteStore.migrate()` ran its SELECT, scripts and INSERTs on the raw connection outside the lock, and the lock was created *after* the constructor's `migrate()`, while the class comment added in #94 promised "every statement goes through the lock". `migrate()` is public and idempotent, so a re-run on a live store can overlap a read. | Lock created before the first `migrate()`; `migrate()` holds it end to end. | `test_migrate_is_serialised_with_reads` (8-thread barrier: half re-run `migrate()`, half read; memory adapter skipped, no schema) | Fails 3/3 on the old code on both SQLite adapters (`IndexError` at `r[0]` from another statement's rows, or a non-empty applied list on an idempotent re-run); passes 3/3 after. |
| A4 | C | Two header chips said the seal reach twice: the `sealed ≤ N` fact chip and the #98 verdict chip whose title already states "sealed through N" (DEBATE-3 J4, parked until #98 merged). | Chip removed; `docs/UI.md` updated. | `debate2.test.tsx`: run fixture carries `sealed_through` (so the old chip would render) and the verdict test asserts no `sealed ≤` text while the title still carries the seq. | Fails on the previous commit, passes after; tsc clean. |
| A5 | S (found by the closing gate; out of range) | `pyproject.toml`'s `testpaths` lists every provider's tests and says they `importorskip` when the provider is not installed. True for `openai-compat` and `anthropic`; the two newest, `openai-agents` and `pydantic-ai`, imported their package at module top, so a root `pytest` on any host without those two distributions aborted at collection. CI never saw it because it installs all four. | `pytest.importorskip(...)` ahead of the import in both files, matching the older two. | The root run itself: it collects on a host without the providers. | Before: `2 errors` at collection, suite never ran. After: 466 passed, 13 skipped. |

## Declined — with the evidence that cleared each

| id | seat | candidate | why declined | conf. |
|---|---|---|---|---|
| D1 | P | Inbox empty state: "appears the moment one of its steps declares an effect" is imprecise for a non-default Budget. | True on the default-budget first-hour path it serves (`models.py` Budget defaults); wording nit, below the bar. | 75 |
| D2 | Sec | Browser notification exposes gate metadata on the OS notification centre. | Same data as the badge, only while a session token is live, per browser profile; carries no authority or credential (`notify.ts`). | 55 |
| D3 | Sec | Notify preference stays on after the browser permission is revoked. | `NotifySwitch` initial state is `pref && Notification.permission === "granted"` (`App.tsx`). **Self-raised, withdrawn.** | — |
| D4 | Sec | `permission.ts` "The API will accept this" could be a false promise for cost gates or unnarrowed budgets. | `engine._budget_for(run)` is the policy-narrowed effective budget (`engine.py`, `policy.py`); the `allow_agent_approval` exception covers `kind == cost` too. Sentence equals the engine for every gate kind; A1 keeps it so. **Self-raised, withdrawn.** | — |
| D5 | S | Run page reads `(shown ?? run).approvals` while a seek is loading. | Transient; `decidable = at === null` is already false, so nothing decidable renders (the #96 fix holds). | 40 |
| D6 | S | Stream loop stops reconnecting after one failed refetch on a non-terminal run. | Real weakness, but pre-existing at `6159213:138`, outside the range. Parked for a future pass, not charged to #94–#98. | 70 (out of range) |
| D7 | C | CQA PY-MAINT-001/002 on `append_events` complexity. | The `BEGIN IMMEDIATE` transaction with fence check and rollback-and-re-raise is the documented boundary pattern; pre-existing, untouched by #94. | — |
| D8 | C | CQA TS-COR-001 ×3 documented empty catches. | Each comment states the intent (bonus context fetch; timeline catches up next refetch; non-JSON body kept as text). | — |
| D9 | C | CQA TS-COR-005 nine `!` in `graph.test.ts`; PY-MAINT-001 on the thousand-step test. | Test files, both pre-existing. | — |
| D10 | C | DEBATE-3 J1: types-only OpenAPI codegen with a drift test. | Accepted in DEBATE-3 but a toolchain change with its own review surface; stays queued as its own PR. | deferred |

## Self-review over the fix commits (code-reviewer skill, `a413d83..93b846b`)

8 files → 5 risk-ordered bundles, all reviewed. One finding:

- **R1** (low/test, 90%) — `test_ui_human_only_effects_equal_the_engines` parsed the TS `Set`
  with `.*?` and no `re.S`, so a formatter wrapping the literal across lines would have failed the
  drift test as "no longer declares HUMAN_ONLY_EFFECTS as a Set literal": a false alarm, not drift.
  Fixed (`re.S`); demonstrated by reformatting the literal (old regex fails, new passes).

Declined in the self-review: the fixture-consumer check being a substring test (weak but not
vacuous; the import and the row iteration are in `debate2.test.tsx`), the in-process worker
stand-in in `test_tutorial.py` (same `engine.advance`, identical log), PRAGMAs before the lock
in `SqliteStore.__init__` (constructor-only, unreachable concurrently), and shared module state
across the table-driven vitest rows (`beforeEach` resets it).

## CQA

`python3 -m cqa_analyzer --offline --output-format json .` at `a413d83` and at the branch tip:
rating 8.8 / Excellent and architecture 8.8 unchanged, all files authoritative, 85 → 83 findings.
The two gone are the tutorial test's PY-MAINT-001 and PY-MAINT-003 (A2's split); a baseline diff
(`--baseline … --new-findings-only`) reports zero new findings. Of the nine baseline findings
inside the range's files, seven were pre-existing or documented boundary patterns (D7–D9) and two
were the tutorial test's — the expected shape for a hardened repo, where most in-diff CQA hits are
documented patterns rather than defects.

## Closing gate (branch tip)

- Python: `pytest -q --ignore=tests/chaos` — **466 passed, 13 skipped**, exit 0 (the skips are the
  PostgreSQL-only and provider-not-installed cases). `ruff check .` clean at the version CI installs.
- UI (Node 22): `tsc --noEmit` clean; `vitest run` — **13 files, 99 tests passed**.
- CQA: 8.8 / Excellent, 83 findings, zero new against the `a413d83` baseline.

Two host-side facts worth recording for whoever runs this locally: a globally installed
`pytest-asyncio` (not a dependency of this repo) breaks collection under pytest 9.1 (`-p no:asyncio`
clears it), and a stale local `ruff` (0.8.x) reports E402 on all four providers' tests that the
version CI installs (0.16.x) does not — match CI's version before trusting a lint result.

## What this pass did not do

No engine or API behaviour changed. The two engine-level items the earlier debates deferred
(approval assignment/routing; declared choices at the gate, with Airflow's HITL task as the
reference shape) still need their own ADRs. D6 is real and out of range. D10 stays its own PR.
