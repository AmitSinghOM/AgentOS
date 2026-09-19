"""Queue and lease ports for the worker (C6).

Lease semantics (the part that makes N workers safe):
- `acquire(run_id, holder, ttl)` returns a `LeaseToken` or None. Exactly one holder at a
  time. The token's `fence` is a strictly increasing integer per run.
- `renew(token)` extends the TTL; returns False if the lease was lost (expired and taken).
- `release(token)` ends it early.
- **Fencing**: the worker passes its `fence` to `Store.append_events`; the store rejects
  any append whose fence is lower than the highest fence it has seen for that run. A
  worker that stalled past its TTL and wakes up later therefore cannot write, even
  though it is still running. This is what turns "lease expiry" from a race into a
  guarantee (split-brain case in ROADMAP.md, chaos Layer 2).

Queue semantics are deliberately weak: at-least-once delivery of `run_id`s. All
exactly-once properties come from the log + lease, never from the queue (C2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LeaseToken:
    run_id: str
    holder: str
    fence: int


class Lease(Protocol):
    def acquire(self, run_id: str, holder: str, ttl_seconds: float) -> LeaseToken | None: ...
    def renew(self, token: LeaseToken, ttl_seconds: float) -> bool: ...
    def release(self, token: LeaseToken) -> None: ...


class Queue(Protocol):
    """At-least-once run queue. `pull` blocks up to `timeout` and returns a run_id or
    None; `ack` removes the delivery; an un-acked delivery is redelivered after
    `visibility_seconds`. `push(run_id, delay_seconds=d)` makes the run deliverable no
    earlier than `d` seconds from now — the retry-backoff primitive."""

    def push(self, run_id: str, *, delay_seconds: float = 0.0) -> None: ...
    def pull(self, timeout: float) -> str | None: ...
    def ack(self, run_id: str) -> None: ...
