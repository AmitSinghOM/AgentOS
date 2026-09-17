"""Tamper-evident event log (C12, docs/TRUST_BOUNDARY.md §2).

Every event the engine appends carries `prev_hash` (the previous event's `hash`, or None
for the first) and `hash` = SHA-256 of its own canonical record with `hash` removed. The
fold verifies the chain, so an event that was edited, inserted or removed after the fact
turns the run un-foldable with a `FoldError` naming the seq — rather than becoming the
"replayed event" a scheduler happily dispatches.

Additive (`schema_version` stays 1): logs written before v0.6.0 carry no hashes and fold
as before; once a hashed event appears, every later event must chain. The chain is
computed here, in the core, not in the store adapters — the store persists exactly what it
is given and a store that rewrote it would break the chain it cannot forge without the
whole prefix.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from agentos.core.events import Event


def event_hash(ev: Event) -> str:
    """SHA-256 over the canonical record (sorted keys, no whitespace), `hash` excluded.
    `seq` and `prev_hash` ARE included — that is what makes it a chain."""
    rec = ev.to_record()
    rec.pop("hash", None)
    canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def chain(events: Sequence[Event], expected_seq: int, prev_hash: str | None) -> list[Event]:
    """Return copies stamped with seq (expected_seq+1…), prev_hash and hash. The store
    assigns the same seqs on append, so what is persisted is what was hashed."""
    out: list[Event] = []
    for i, ev in enumerate(events, start=expected_seq + 1):
        stamped = ev.model_copy(update={"seq": i, "prev_hash": prev_hash, "hash": None})
        stamped = stamped.model_copy(update={"hash": event_hash(stamped)})
        prev_hash = stamped.hash
        out.append(stamped)
    return out


class IntegrityError(ValueError):
    """The log does not verify: an event was altered, inserted or removed."""


def verify(events: Sequence[Event], *, prev_hash: str | None = None,
           chained: bool = False) -> int:
    """Check the chain over an ordered log. Returns the number of hashed events verified.
    Raises IntegrityError with the seq of the first event that fails. `prev_hash` /
    `chained` describe the log prefix this slice continues (a snapshot's tail, C15)."""
    prev: str | None = prev_hash
    verified = 0
    for ev in events:
        if ev.hash is None:
            if chained:
                raise IntegrityError(f"seq {ev.seq}: unhashed event after a hashed one")
            continue
        chained = True
        if ev.prev_hash != prev:
            raise IntegrityError(f"seq {ev.seq}: prev_hash does not match seq {ev.seq - 1}")
        if event_hash(ev) != ev.hash:
            raise IntegrityError(f"seq {ev.seq}: content does not match its hash")
        prev = ev.hash
        verified += 1
    return verified
