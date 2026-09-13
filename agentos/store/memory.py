"""In-memory Store + BlobStore. Implements the same contract as the SQLite adapter and
passes the same suite (tests/contract/). Used for unit tests and the zero-config demo;
nothing survives a process restart, by design."""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence

from agentos.core.events import Event, RunStarted, from_record
from agentos.core.models import Agent, BlobRef, WorkflowDefinition
from agentos.core.ports import ConflictError


class MemoryStore:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._events: dict[str, list[dict]] = {}          # run_id → records in seq order
        self._requests: dict[str, str] = {}               # request_id → run_id
        self._blobs: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    # definitions
    def put_agent(self, agent: Agent) -> None:
        self._agents[agent.name] = agent

    def get_agent(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def list_agents(self) -> list[Agent]:
        return list(self._agents.values())

    def put_workflow(self, wf: WorkflowDefinition) -> None:
        self._workflows[wf.name] = wf

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        return self._workflows.get(name)

    # run log
    def append_events(self, run_id: str, expected_seq: int,
                      events: Sequence[Event]) -> list[Event]:
        with self._lock:
            log = self._events.get(run_id, [])
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
            for ev in out:
                if isinstance(ev, RunStarted):
                    self._requests[ev.request_id] = run_id
            return out

    def read_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        # Round-trip through records so this adapter has the same serialization
        # behaviour as a real database (enum → str → enum), cf. agno #8454.
        return [from_record(r) for r in self._events.get(run_id, []) if r["seq"] > after_seq]

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
