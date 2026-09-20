"""Server-Sent Events over the run log (Phase 7).

The stream is a CONSUMER of the log, exactly like the observers: each tick reads
`store.read_events(run_id, after_seq)` and emits every new record as one SSE event whose
`id` is the record's `seq`, `event` its `event_type`, and `data` the same JSON that
`GET /runs/{id}/events` returns — so the two views can never disagree.

Consequences, each tested:
- Works from ANY process. Nothing here subscribes to an in-memory bus; a worker's appends
  are visible the moment they are committed (C6 — the mastra #19252 failure is impossible).
- Resumable. `Last-Event-ID` (or `?after=`) is a seq; the client gets only later events.
- Bounded. The connection closes after the terminal event, or after `max_seconds`, so an
  abandoned client on a run that waits hours for an approval cannot pin a worker thread;
  the client reconnects with `Last-Event-ID` and misses nothing. A client that resumes AT
  or past the terminal event is closed immediately (the anchor is peeked once).
- Event-level, not token-level. Token deltas would need an executor hook; see ROADMAP.

Configuration (all validated at startup, invalid values name the variable):
  AGENTOS_STREAM_POLL_SECONDS       store poll interval   (default 0.25, floor 0.05)
  AGENTOS_STREAM_KEEPALIVE_SECONDS  comment while idle    (default 15)
  AGENTOS_STREAM_MAX_SECONDS        close after           (default 3600)
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import anyio

from dagentos.core.engine import TERMINAL
from dagentos.core.ports import Store

_log = logging.getLogger("agentos.api.stream")

MEDIA_TYPE = "text/event-stream"
POLL_FLOOR_SECONDS = 0.05
_TERMINAL_TYPES = frozenset(f"run.{s}" for s in TERMINAL)


@dataclass(frozen=True)
class StreamConfig:
    poll_seconds: float = 0.25
    keepalive_seconds: float = 15.0
    max_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> StreamConfig:
        return cls(
            poll_seconds=max(_float_env("AGENTOS_STREAM_POLL_SECONDS", 0.25), POLL_FLOOR_SECONDS),
            keepalive_seconds=_float_env("AGENTOS_STREAM_KEEPALIVE_SECONDS", 15.0),
            max_seconds=_float_env("AGENTOS_STREAM_MAX_SECONDS", 3600.0),
        )


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number > 0, got {raw!r}") from None
    if not value > 0:
        raise RuntimeError(f"{name} must be a number > 0, got {raw!r}")
    return value


def parse_after(last_event_id: str | None, after: int) -> int:
    """`Last-Event-ID` wins over `?after=`; both are a seq. Raises ValueError on garbage so
    the endpoint can answer 400 BEFORE the response starts (a generator cannot)."""
    if last_event_id is None or last_event_id == "":
        return after
    try:
        value = int(last_event_id)
    except ValueError:
        raise ValueError(f"Last-Event-ID must be an integer seq, got {last_event_id!r}") from None
    if value < 0:
        raise ValueError(f"Last-Event-ID must be >= 0, got {last_event_id!r}")
    return value


def format_event(record: dict) -> str:
    """One SSE frame. `data` is single-line JSON (no raw newlines), so one `data:` line.
    `ensure_ascii=False` + compact separators is exactly what Starlette's JSONResponse
    renders for GET /runs/{id}/events, so the two views are byte-identical, not just
    value-equal (a review caught the first draft escaping non-ASCII)."""
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"id: {record['seq']}\nevent: {record['event_type']}\ndata: {payload}\n\n"


def _after_anchor(events: list, after: int) -> list | None:
    """First read peeks one extra row (the anchor at `after`) so a client resuming AT or
    past the terminal event is told the run is over now, not after max_seconds. Returns the
    events to send, or None when the anchor itself is terminal and the stream should end."""
    if after > 0 and events and events[0].seq == after:
        if type(events[0]).event_type in _TERMINAL_TYPES:
            return None
        return events[1:]
    return events


async def stream_run(store: Store, run_id: str, *, after: int, config: StreamConfig,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
                     ) -> AsyncIterator[str]:
    """Yield SSE frames for `run_id` from seq `after`+1 until the run's terminal event or
    `config.max_seconds`, whichever first. A store error is logged and ends the stream —
    the client reconnects with Last-Event-ID; nothing is ever fabricated.

    An async generator on purpose (production pass 2, A2): Starlette runs a sync generator
    through `iterate_in_threadpool`, one thread-pool token per `next()`, and the poll sleep
    sat inside `next()` — so every idle client held one of anyio's 40 default tokens for its
    whole connection, and N open tabs left 40 - N threads for the rest of the (sync) API.
    Here the wait is `await sleep(...)` on the event loop and only the store query borrows a
    thread, for the query's duration."""
    started = clock()
    last_sent = started
    seq = after

    async def read(after_seq: int) -> list:
        return await anyio.to_thread.run_sync(store.read_events, run_id, after_seq)

    try:
        events = _after_anchor(await read(max(after - 1, 0)), after)
        if events is None:
            return
        while True:
            for event in events:
                record = event.to_record()
                seq = event.seq
                last_sent = clock()
                yield format_event(record)
                if record["event_type"] in _TERMINAL_TYPES:
                    return
            now = clock()
            if now - started >= config.max_seconds:
                yield ": max duration reached; reconnect with Last-Event-ID\n\n"
                return
            if now - last_sent >= config.keepalive_seconds:
                last_sent = now
                yield ": keep-alive\n\n"
            await sleep(config.poll_seconds)
            events = await read(seq)
    except Exception as exc:  # noqa: BLE001 — headers are sent; the only honest move is to end
        _log.warning("run %s: stream ended after seq %s (%s)", run_id, seq, exc)
        return
