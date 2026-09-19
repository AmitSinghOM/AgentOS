"""In-memory Store + BlobStore + Lease + Queue. Implements the same contracts as the
SQLite and Postgres adapters and passes the same suites (tests/contract/). Used for unit
tests and the zero-config demo; nothing survives a process restart, by design."""
from __future__ import annotations

import hashlib
import heapq
import json
import threading
import time
from collections import deque
from collections.abc import Sequence

from dagentos.core.coordination import LeaseToken
from dagentos.core.events import Event, RunStarted, from_record
from dagentos.core.models import Agent, BlobRef, WorkflowDefinition
from dagentos.core.ports import ConflictError


class MemoryStore:
    def __init__(self) -> None:
        self._agents: dict[tuple[str, int], Agent] = {}
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._events: dict[str, list[dict]] = {}          # run_id → records in seq order
        self._snapshots: dict[str, tuple[int, str | None, str]] = {}
        self._requests: dict[str, str] = {}               # request_id → run_id
        self._fences: dict[str, int] = {}                 # run_id → highest fence seen
        self._blobs: dict[str, tuple[bytes, str]] = {}
        self._leases: dict[str, tuple[str, int, float]] = {}   # run_id → (holder, fence, expires)
        self._fence_counter: dict[str, int] = {}
        self._queue: deque[str] = deque()
        self._inflight: list[tuple[float, str]] = []      # (visible_at, run_id) heap
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self.visibility_seconds = 30.0

    # definitions (agents immutable per (name, version))
    def put_agent(self, agent: Agent) -> None:
        key = (agent.name, agent.version)
        with self._lock:
            existing = self._agents.get(key)
            if existing is not None:
                if existing != agent:
                    raise ConflictError(f"agent {agent.name!r} v{agent.version} already exists "
                                        f"with a different definition; bump the version")
                return
            self._agents[key] = agent

    def get_agent(self, name: str, version: int | None = None) -> Agent | None:
        if version is not None:
            return self._agents.get((name, version))
        versions = [v for (n, v) in self._agents if n == name]
        return self._agents[(name, max(versions))] if versions else None

    def list_agents(self) -> list[Agent]:
        names = sorted({n for (n, _) in self._agents})
        return [self.get_agent(n) for n in names]  # type: ignore[misc]

    def list_agent_versions(self, name: str) -> list[int]:
        return sorted(v for (n, v) in self._agents if n == name)

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        self._workflows[wf.name] = wf

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        return self._workflows.get(name)

    # run log
    def append_events(self, run_id: str, expected_seq: int,
                      events: Sequence[Event], *, fence: int | None = None) -> list[Event]:
        with self._lock:
            log = self._events.get(run_id, [])
            # Fence first: a fenced-out writer is told so regardless of seq, because the
            # fence is the guarantee that matters when seqs still happen to match.
            if fence is not None and fence < self._fences.get(run_id, 0):
                raise ConflictError(f"run {run_id!r}: fence {fence} is stale "
                                    f"(highest seen {self._fences[run_id]})")
            if len(log) != expected_seq:
                raise ConflictError(
                    f"run {run_id!r}: expected seq {expected_seq}, log is at {len(log)}"
                )
            out: list[Event] = []
            staged: list[dict] = []
            for i, ev in enumerate(events, start=expected_seq + 1):
                stamped = ev.model_copy(update={"seq": i})
                rec = stamped.to_record()
                if isinstance(ev, RunStarted) and ev.request_id in self._requests:
                    raise ConflictError(f"request_id {ev.request_id!r} already started")
                staged.append(rec)
                out.append(stamped)
            # Commit only after every event validated — nothing on conflict, not even
            # an empty log entry for the run.
            self._events.setdefault(run_id, []).extend(staged)
            if fence is not None:
                self._fences[run_id] = fence
            for ev in out:
                if isinstance(ev, RunStarted):
                    self._requests[ev.request_id] = run_id
            return out

    def read_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        # Round-trip through records so this adapter has the same serialization
        # behaviour as a real database (enum → str → enum), cf. agno #8454.
        return [from_record(r) for r in self._events.get(run_id, []) if r["seq"] > after_seq]

    # snapshots (C15): a bounded optimization of the fold, never the source of truth.
    # Monotonic per run (see SqliteStore.put_snapshot).
    def put_snapshot(self, run_id: str, seq: int, last_hash: str | None, state: dict) -> None:
        with self._lock:                      # stored as JSON text: same round trip as SQL
            current = self._snapshots.get(run_id)
            if current is not None and current[0] >= seq:
                return
            self._snapshots[run_id] = (seq, last_hash, json.dumps(state, sort_keys=True,
                                                                     default=str))

    def get_snapshot(self, run_id: str) -> tuple[int, str | None, dict] | None:
        snap = self._snapshots.get(run_id)
        return (snap[0], snap[1], json.loads(snap[2])) if snap else None

    def list_run_ids(self) -> list[str]:
        return list(self._events)

    def run_id_for_request(self, request_id: str) -> str | None:
        return self._requests.get(request_id)

    # blobs
    def put(self, data: bytes, media_type: str = "application/json") -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        self._blobs.setdefault(digest, (bytes(data), media_type))
        return BlobRef(sha256=digest, size=len(data), media_type=media_type)

    def get(self, ref: BlobRef) -> bytes:
        try:
            return self._blobs[ref.sha256][0]
        except KeyError:
            raise KeyError(f"blob {ref.sha256[:12]}… not found") from None

    def exists(self, ref: BlobRef) -> bool:
        return ref.sha256 in self._blobs

    # lease
    def acquire(self, run_id: str, holder: str, ttl_seconds: float) -> LeaseToken | None:
        with self._lock:
            now = time.monotonic()
            cur = self._leases.get(run_id)
            if cur is not None and cur[2] > now and cur[0] != holder:
                return None
            fence = self._fence_counter.get(run_id, 0) + 1
            self._fence_counter[run_id] = fence
            self._leases[run_id] = (holder, fence, now + ttl_seconds)
            # Record the fence at acquire time, not first write: from this instant any
            # holder with a lower fence is stale, even if it writes before we do.
            self._fences[run_id] = max(self._fences.get(run_id, 0), fence)
            return LeaseToken(run_id=run_id, holder=holder, fence=fence)

    def renew(self, token: LeaseToken, ttl_seconds: float) -> bool:
        with self._lock:
            cur = self._leases.get(token.run_id)
            if cur is None or cur[0] != token.holder or cur[1] != token.fence:
                return False
            if cur[2] <= time.monotonic():
                return False
            self._leases[token.run_id] = (cur[0], cur[1], time.monotonic() + ttl_seconds)
            return True

    def release(self, token: LeaseToken) -> None:
        with self._lock:
            cur = self._leases.get(token.run_id)
            if cur and cur[0] == token.holder and cur[1] == token.fence:
                del self._leases[token.run_id]

    # queue
    def push(self, run_id: str, *, delay_seconds: float = 0.0) -> None:
        """Enqueue, or make an in-flight delivery visible again (see SqliteStore.push).
        With a delay, the run is not deliverable before now+delay."""
        with self._cv:
            self._inflight = [(t, r) for t, r in self._inflight if r != run_id]
            if run_id in self._queue:
                self._queue.remove(run_id)
            if delay_seconds > 0:
                heapq.heappush(self._inflight, (time.monotonic() + delay_seconds, run_id))
            else:
                heapq.heapify(self._inflight)
                self._queue.append(run_id)
            self._cv.notify()

    def pull(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                # Redeliver anything whose visibility timeout lapsed (at-least-once).
                while self._inflight and self._inflight[0][0] <= now:
                    _, rid = heapq.heappop(self._inflight)
                    self._queue.append(rid)
                if self._queue:
                    rid = self._queue.popleft()
                    heapq.heappush(self._inflight, (now + self.visibility_seconds, rid))
                    return rid
                remaining = deadline - now
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.05))

    def ack(self, run_id: str) -> None:
        with self._cv:
            self._inflight = [(t, r) for t, r in self._inflight if r != run_id]
            heapq.heapify(self._inflight)
            if run_id in self._queue:          # same as the SQL adapters: ack removes the
                self._queue.remove(run_id)     # run whether waiting or in flight

    def queue_depth(self) -> int:
        """Runs waiting or in flight (not yet acked)."""
        with self._cv:
            return len(self._queue) + len(self._inflight)
