"""`agentos export-run --format jsonl` (ROADMAP A11, audit E-08): one run's record as a file an
auditor or an external evaluator (opik, langfuse) can take away. AgentOS records, never scores:
the file carries the log exactly as stored plus the fold's own summary — nothing derived beyond
it. One JSON object per line, per run, in this order:

    {"kind": "agentos.export", "version": 1, "run_id": ..., "exported_at": ...,
     "chain": {"verified": bool, "sealed_through": int | null, "seals": ..., "error": ...}}
    <every event in seq order: the dict `GET /runs/{id}/events` returns, hashes included>
    {"kind": "blob", "sha256": ..., "bytes_b64": ...}        only with include_blobs
    {"kind": "summary", "run_id": ..., "status": ..., "events": n, "cost": ..., "error": ...}

An event line is any line with `event_type` (`approval.requested` carries a `kind` of its own,
so classify by `event_type` first). Every line is canonical JSON — sorted keys, no whitespace,
ASCII — the serialization `dagentos.core.integrity.event_hash` hashes, so an event line minus
its `hash` hashes to that `hash` with the standard library alone. Re-exporting a run is
byte-identical except `exported_at`. Read-only: nothing is appended, nothing is redacted (the
log is already stored redacted where an executor redacts). Contract: docs/REPLAY.md
"Exporting a run"; `version` bumps only on an incompatible shape.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any, TextIO

from dagentos.core.events import Event
from dagentos.core.fold import FoldError, fold
from dagentos.core.integrity import IntegrityError, verify
from dagentos.core.models import BlobRef
from dagentos.core.ports import BlobStore, Store
from dagentos.core.seal import HmacKeyring, verify_seals

KIND = "agentos.export"
VERSION = 1


def _line(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"


def chain_state(events: Sequence[Event], keyring: HmacKeyring | None) -> dict[str, Any]:
    """The judgement `agentos verify` makes: the hash chain, then the seals. Seals that are
    unsigned or cannot be judged without a keyring are not a failure (verify warns); a broken
    chain or an INVALID seal is."""
    error = None
    try:
        verify(events)
    except IntegrityError as exc:
        error = f"chain: {exc}"
    seals = verify_seals(events, keyring)
    if error is None and seals.state == "INVALID":
        error = "; ".join(seals.problems)
    return {"verified": error is None, "sealed_through": seals.sealed_through,
            "seals": seals.state, "error": error}


def referenced_blobs(records: Sequence[dict[str, Any]]) -> list[str]:
    """sha256 of every blob the log references, first reference first, once each: every
    BlobRef-shaped value (`inputs_ref`, `output_ref`, …) and every `pricing_snapshot_hash`.
    Walking the records instead of naming fields keeps a ref added later covered."""
    seen: dict[str, None] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("sha256"), str) and "size" in value:
                seen.setdefault(value["sha256"])
            if isinstance(value.get("pricing_snapshot_hash"), str):
                seen.setdefault(value["pricing_snapshot_hash"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for record in records:
        walk(record)
    return list(seen)


def summary(run_id: str, events: Sequence[Event]) -> dict[str, Any]:
    """The fold's own facts. A log the fold refuses (broken chain, seq gap) gets no status or
    cost — the export never states a state the engine itself would not."""
    try:
        run = fold(events)
    except FoldError as exc:
        return {"kind": "summary", "run_id": run_id, "status": None, "events": len(events),
                "cost": None, "error": str(exc)}
    return {"kind": "summary", "run_id": run_id, "status": run.status.value,
            "events": len(events), "cost": run.total_cost, "error": None}


def write_run(out: TextIO, store: Store, blobs: BlobStore, run_id: str, *,
              keyring: HmacKeyring | None, include_blobs: bool,
              exported_at: str) -> tuple[dict[str, Any], list[str]]:
    """Write one run's lines to `out`. Returns the header's `chain` block and the sha256 of
    every referenced blob the store no longer holds (each written as a `missing` line)."""
    events = store.read_events(run_id)
    if not events:
        raise ValueError(f"unknown run {run_id!r}")
    records = [ev.to_record() for ev in events]
    chain = chain_state(events, keyring)
    out.write(_line({"kind": KIND, "version": VERSION, "run_id": run_id,
                     "exported_at": exported_at, "chain": chain}))
    for record in records:
        out.write(_line(record))
    missing: list[str] = []
    if include_blobs:
        for sha in referenced_blobs(records):
            ref = BlobRef(sha256=sha, size=0)
            if blobs.exists(ref):
                data = base64.b64encode(blobs.get(ref)).decode("ascii")
                out.write(_line({"kind": "blob", "sha256": sha, "bytes_b64": data}))
            else:
                missing.append(sha)
                out.write(_line({"kind": "blob", "sha256": sha, "bytes_b64": None,
                                 "missing": True}))
    out.write(_line(summary(run_id, events)))
    return chain, missing
