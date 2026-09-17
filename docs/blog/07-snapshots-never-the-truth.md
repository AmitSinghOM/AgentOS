# Snapshots That Are Never the Truth

> Draft — accompanies the `v0.7.0-complete` release.

The fifteenth and last con from the landscape survey was the one I had the least respect
for. C15 said: *keep the log append-only; if you add snapshots, bound them and never let
them become the source of truth.* Every event-sourcing tutorial says this in its second
paragraph. I deferred it for five phases because replaying tens of events was
sub-millisecond and there was nothing to bound.

Then the survey closed. The `tool` agent shipped, C13 became a test instead of a sentence,
C14 got its design note, and C10's typed round-trip had a test per event type on every
adapter. Only C15 remained, and by then `advance()` was refolding the whole log at every
wave of every run. A thousand-step run paid for its own history a thousand times. So the
optimization finally had a cost to remove — and, it turned out, three ways to go wrong that
the tutorials do not mention.

## The shape

A snapshot is a `WorkflowRun` — the same folded state `GET /runs/{id}` returns — stored
next to the log with the `seq` and `hash` of the last event it includes. The engine reads
the snapshot, reads the events after that `seq`, and continues the fold from the
snapshotted state:

```python
run = fold_from(snapshot, tail)     # instead of fold(all_events)
```

`fold_from` verifies that the first tail event's `prev_hash` equals the snapshot's
`last_hash`, so a snapshot that does not belong to this log, or a log edited behind it,
is refused exactly like any other integrity violation. Every `AGENTOS_SNAPSHOT_EVERY`
committed events (default 200), the worker writes a new one when `advance()` returns.
Deleting a snapshot is always safe: the engine folds the full log instead.

That was the version I put up for review. The review — four seats, then the code-reviewer
skill's anchored pass, then cqa-analyzer — found three things I had gotten wrong. Each is
an instance of the same mistake: I had treated the snapshot as *my* data rather than as
something a second, possibly stale, possibly wrong process could also have written.

## Wrong 1: a cache write that can fail the thing it caches

`_maybe_snapshot` ran in `advance()`'s `finally`. If the snapshot write raised — a
transient database error, a full disk — the exception propagated out of an `advance()`
whose events were already committed. Worse: if the advance had ended with a real
exception (`Crash` from the chaos suite, `LeaseLost`), the snapshot error replaced it.
The worker would log a database hiccup and never learn it had lost its lease.

Derived work has one rule, and the observers already obeyed it: it must never fail the
thing it derives from, and it must never mask the exception that ended it. The snapshot
write now catches and logs. The test injects a store whose `put_snapshot` raises and
asserts the advance completes and the run's events are all present.

## Wrong 2: last writer wins, and the last writer can be the wrong one

All three adapters overwrote the snapshot unconditionally — `INSERT OR REPLACE`,
`DO UPDATE SET`, a dict assignment. Consider a fenced-out worker. Its lease was taken
over; the fence stopped its `append_events`; but its `finally` still runs, and it still
holds a `WorkflowRun` folded a moment ago. It writes that as the snapshot. The live worker,
which has since appended twenty events and written a newer snapshot, is now behind a
snapshot from the past.

The fold would have caught it: an older snapshot still chains to the log, so the engine
would simply replay more tail than necessary. Correctness holds. But the *bound* is gone,
silently, and a bound that can be silently lost is not a bound. `put_snapshot` is now
monotonic per run — `WHERE excluded.seq > run_snapshots.seq` on SQLite,
`WHERE run_snapshots.seq < EXCLUDED.seq` on Postgres, a compare on the memory adapter —
and the contract test writes seq 10 then seq 5 and asserts 10 is what remains, on every
adapter, including real Postgres.

## Wrong 3: a snapshot with nothing after it was trusted

`fold_from` verifies the link between the snapshot and the *first tail event*. If there is
no tail — the snapshot is at the log's current end, which is the common case right after
it was taken — there is nothing to link to, and the original code returned the snapshot's
state unverified. A snapshot whose `seq` was beyond the log's end, or whose `hash` was not
the log's hash at that `seq`, would have been served as the run's state.

The fix costs one row: read the anchor event along with the tail
(`read_events(after_seq=seq - 1)`) and require that its `seq` and `hash` match the
snapshot's before continuing from it. The test asserts that the valid case reads exactly
one event, and that both forged cases — `seq` past the end, `hash` altered — fall back to
the full fold. `docs/TRUST_BOUNDARY.md` now places snapshots inside the tamper-evidence
boundary and says plainly what is verified (the anchor and the tail's chain) and what is
not (the snapshot's own prefix — that is what `GET /runs/{id}/integrity` is for, and it
never reads a snapshot).

## The test that made me comfortable

`fold_from(fold(events[:k]), events[k:]) == fold(events)` for every `k` in every golden
log the project has recorded, `v0.2.0-dev` through `v0.6.0`. Six logs, every cut point,
one parametrized test. If a future change to the fold makes a continuation diverge from a
fresh fold anywhere in five releases' worth of recorded history, CI says so.

## What this phase closes

With C15, all fifteen `landscape-con` issues that scoped this project are closed, each
with an acceptance test named in its closing comment. The survey asked: what do the
popular agent runtimes get wrong about durability, ordering, replay, trust and
persistence? The answer is now a test suite rather than an opinion.

The carry-forward is honest about the cost: `advance()` has grown through gate, dispatch,
verify, record, cancel, pause, approvals and now snapshots, and cqa-analyzer flags its
complexity. That refactor is the next PR, judged on its own diff against the golden
corpus and the chaos suite, rather than folded into this tag.
