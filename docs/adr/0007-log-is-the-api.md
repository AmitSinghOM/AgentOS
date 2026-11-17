# ADR 0007 — The event log is the public API

Status: accepted (decided 2026-09-13, Phase 1; written down 2026-11-16 from
`docs/DEVELOPMENT_STRUCTURE.md` §3 and `ROADMAP.md` Phase 1 "Event-sourced run state").

## Context

Code can be rewritten; a persisted run log cannot be un-written. A run recorded in 2026 must
still fold in a much later build, after model generations and framework majors have come and
gone. Compatibility therefore has to be defined on the log, not on Python signatures.

## Decision

- Every event carries `event_type` and `schema_version` (`dagentos/core/events.py`);
  `parent_run_id` is on every event from the first release so child runs never need a
  migration.
- Events are append-only and immutable. Fields are only ever added; a new meaning is a new
  `event_type`, never a renamed or re-typed field.
- Old versions are read through the upcaster registry in `dagentos/core/upcast.py`
  (`@upcaster(event_type, from_version=…)`, pure functions, `v1 → v2 → … → current`).
- Serialization is JSON with explicit field names; every event is hash-chained to its
  predecessor (`dagentos/core/integrity.py`, `docs/TRUST_BOUNDARY.md` §2).
- State is the fold over the log (`dagentos/core/fold.py`); snapshots are derived and
  anchor-verified, never the truth (`docs/REPLAY.md`).

## Consequences

- The golden corpus (`tests/golden/v0.*.json`) is replayed on every PR:
  `test_golden_log_folds_to_recorded_state` and `test_fold_from_any_cut_point_equals_the_full_fold`
  fail the build if any released log stops folding to its recorded state.
- `tests/test_fold_upcast.py` pins the upcasters as pure functions.
- Removing an `event_type` is never allowed; deprecation is a warning for one minor release
  (`DEVELOPMENT_STRUCTURE.md` §6).
