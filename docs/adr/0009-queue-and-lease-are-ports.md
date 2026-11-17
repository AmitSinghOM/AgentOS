# ADR 0009 — Queue and lease are ports; Redis is not required

Status: accepted (decided 2026-09-13, Phase 1 — the "ADR note (to become ADR 0009)" in
`ROADMAP.md` Phase 1; Redis dependency removed 2026-09-17, Phase 6; written down 2026-11-16).

## Context

`DESIGN.md` §3 named Redis as the run queue and lock layer, with Postgres as the source of
truth ("Postgres = truth, Redis = speed", §4.3). That is two systems to run, two to reconcile,
and `pip install dagentos` would have needed infrastructure before the first run
(`docs/DEVELOPMENT_STRUCTURE.md` §7, "zero-ops or it dies").

## Decision

- The run queue and the per-run lease are ports — `Queue` and `Lease` in
  `dagentos/core/coordination.py` — implemented by the same store adapters as the log
  (`queue` and `leases` tables in `dagentos/store/sqlite.py` and `dagentos/store/postgres.py`;
  `dagentos/store/memory.py` for tests). One system of record.
- Queue semantics are deliberately weak (at-least-once delivery of run ids). Every
  exactly-once property comes from the log plus the lease, never from the queue (C2).
- The lease carries a strictly increasing **fencing token** per run; `Store.append_events`
  rejects an append whose fence is lower than the highest it has seen, so a worker that
  stalled past its TTL cannot write when it wakes (C6).
- Redis may return as an optional adapter for lower queue latency; correctness never depends
  on it. The `redis` and `sqlalchemy` packages are on the core's forbidden-import list.

## Consequences

- `tests/contract/test_coordination_contract.py` runs the same lease and queue suite against
  every adapter; `tests/chaos/network/test_lease_expiry_race.py` proves the fence over
  Toxiproxy against a second worker.
- `docker-compose.yml` has no redis service; the default install is SQLite with no
  infrastructure.
- `DESIGN.md` §3, §4.3, §4.5, §5 and §6 carry dated "Superseded" banners pointing here and at
  the ROADMAP rather than being rewritten.
