# Architecture decision records

`DESIGN.md` §4 is retroactively ADR 0001–0005 (event-sourced state, idempotency keys, one
system of record, approval as a suspended state, one worker now / N by design). The four
records here were promised by `ROADMAP.md` Phase 1 and `docs/DEVELOPMENT_STRUCTURE.md` §9;
they were written down on 2026-11-16 from the decisions those two files already record, each
with the enforcing test or contract named so the record cannot drift from the code.

| ADR | Title | Enforced by |
| --- | --- | --- |
| [0006](0006-core-depends-on-nothing.md) | Core depends on nothing | `lint-imports` contracts in `pyproject.toml` |
| [0007](0007-log-is-the-api.md) | The event log is the public API | `tests/golden/test_golden.py`, `tests/test_fold_upcast.py` |
| [0008](0008-providers-are-plugins.md) | Providers are plugins | `dagentos/plugins.py`, forbidden-imports contract |
| [0009](0009-queue-and-lease-are-ports.md) | Queue and lease are ports | `tests/contract/test_coordination_contract.py` |

New decisions that would be expensive to reverse get the next number here.
