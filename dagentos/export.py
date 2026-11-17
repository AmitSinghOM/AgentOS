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

`archive_runs` (audit E-08, second half; `agentos archive`) is the one path that removes rows
from a store, and it is export-then-purge, never the reverse: a terminal run older than the
cutoff is written with `write_run` to `<out>/<run_id>.jsonl`, fsynced, read back and counted,
and only then removed in one `Store.purge_run` transaction; `<out>/ARCHIVE.jsonl` gets one
appended row per exported run naming the file's sha256. A run whose chain or seals fail is
exported but never purged (the store keeps the evidence); a non-terminal run is never touched;
every decision is made on the fold, the index only picks the candidates. Contract:
docs/REPLAY.md "Archiving runs".
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from dagentos.core.engine import TERMINAL
from dagentos.core.events import Event
from dagentos.core.fold import FoldError, fold
from dagentos.core.integrity import IntegrityError, verify
from dagentos.core.models import BlobRef, RunStatus
from dagentos.core.ports import BlobStore, ConflictError, Store
from dagentos.core.seal import HmacKeyring, verify_seals

KIND = "agentos.export"
VERSION = 1
ARCHIVE_KIND = "agentos.archive"
ARCHIVE_VERSION = 1
MANIFEST = "ARCHIVE.jsonl"
TERMINAL_STATUSES = tuple(s for s in RunStatus if s.value in TERMINAL)


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


# ------------------------------------------------------------------ archive (E-08)

@dataclass
class Archived:
    """One exported run. `purged` is False in a dry run and for a run kept because its chain
    or seals did not verify (`error` says why); `file`, `sha256` and `bytes` are None in a
    dry run, where nothing is written."""

    run_id: str
    status: str | None
    events: int
    chain_verified: bool
    error: str | None = None
    file: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    purged: bool = False
    blobs_removed: list[str] = field(default_factory=list)


@dataclass
class Skipped:
    """A run the archive did not export. `reason`: `non-terminal` (never touched), `too recent`,
    `status not selected`, `unindexed` (no index row yet), `index drift` (the index called it
    terminal and old, the fold does not), `does not fold`, `appended during archive`."""

    run_id: str
    status: str | None
    reason: str


@dataclass
class ArchiveReport:
    cutoff: str
    dry_run: bool
    out: str
    exported_at: str
    archived: list[Archived] = field(default_factory=list)     # exported and purged (or would be)
    kept: list[Archived] = field(default_factory=list)         # exported, kept: chain unverified
    skipped: list[Skipped] = field(default_factory=list)
    manifest: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.kept)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "failed": self.failed}


def _skip_reason(status: RunStatus | None, updated_at: datetime | None, cutoff: datetime,
                 selected: frozenset[str]) -> str | None:
    if status is None:
        return "unindexed"
    if status.value not in TERMINAL:
        return "non-terminal"
    if status.value not in selected:
        return "status not selected"
    if updated_at is None or updated_at >= cutoff:
        return "too recent"
    return None


def _check_export(data: bytes, run_id: str) -> int:
    """Read the file back the way an auditor will: every line parses, the header names this
    run, the event lines are seq 1..n, the summary agrees. Returns n (= the last seq)."""
    docs = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    if len(docs) < 3:
        raise RuntimeError(f"archive of run {run_id!r}: file has {len(docs)} lines")
    header, *body, summary = docs
    seqs = [d["seq"] for d in body if "event_type" in d]
    if header.get("kind") != KIND or header.get("run_id") != run_id:
        raise RuntimeError(f"archive of run {run_id!r}: header names {header.get('run_id')!r}")
    if seqs != list(range(1, len(seqs) + 1)):
        raise RuntimeError(f"archive of run {run_id!r}: event lines are not seq 1..n")
    if summary.get("kind") != "summary" or summary.get("events") != len(seqs):
        raise RuntimeError(f"archive of run {run_id!r}: summary counts {summary.get('events')} "
                           f"events, file has {len(seqs)}")
    return len(seqs)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_archive_file(path: Path, store: Store, blobs: BlobStore, run_id: str, *,
                        keyring: HmacKeyring | None, include_blobs: bool,
                        exported_at: str) -> tuple[dict[str, Any], str, int, int]:
    """`write_run` to `<path>.part` (0600), fsync, read back and check, then rename into place
    and fsync the directory: a `.jsonl` in the archive dir is always a complete, checked file.
    Returns (chain, sha256, bytes, last_seq)."""
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as fh:
            chain, _missing = write_run(fh, store, blobs, run_id, keyring=keyring,
                                        include_blobs=include_blobs, exported_at=exported_at)
            fh.flush()
            os.fsync(fh.fileno())
        data = part.read_bytes()
        last_seq = _check_export(data, run_id)
        os.replace(part, path)
        _fsync_dir(path.parent)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return chain, hashlib.sha256(data).hexdigest(), len(data), last_seq


def _append_manifest(path: Path, entry: Archived, exported_at: str) -> None:
    """One appended, fsynced row per exported run: what left the store (or was kept), when, and
    the sha256 of the file that holds it. Append-only; never rewritten."""
    row = {"kind": ARCHIVE_KIND, "version": ARCHIVE_VERSION, "run_id": entry.run_id,
           "exported_at": exported_at, "file": entry.file, "sha256": entry.sha256,
           "bytes": entry.bytes, "events": entry.events, "status": entry.status,
           "chain_verified": entry.chain_verified, "purged": entry.purged,
           "blobs_removed": entry.blobs_removed}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(fd, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(_line(row))
        fh.flush()
        os.fsync(fh.fileno())


def archive_runs(store: Store, blobs: BlobStore, out: Path, *, cutoff: datetime,
                 statuses: Sequence[RunStatus], include_blobs: bool,
                 keyring: HmacKeyring | None, dry_run: bool, exported_at: str) -> ArchiveReport:
    """Export-then-purge every run the index lists as terminal, in `statuses`, and last updated
    before `cutoff`; oldest first. The index only picks candidates: each run is re-read, its
    chain judged and its log folded, and a run the fold does not call terminal (or older than
    the cutoff) is skipped as `index drift`. A verified run is written, read back, then
    removed with `Store.purge_run` at the seq the file holds, so anything appended in between
    refuses the purge. An unverified run is written and kept. Dry run: the same decisions,
    nothing written, nothing removed."""
    report = ArchiveReport(cutoff=cutoff.isoformat(), dry_run=dry_run, out=str(out),
                           exported_at=exported_at)
    selected = frozenset(s.value for s in statuses)
    for row in reversed(store.list_runs(limit=None).runs):            # oldest first
        reason = _skip_reason(row.status, row.updated_at, cutoff, selected)
        if reason is not None:
            report.skipped.append(Skipped(row.run_id, row.status.value if row.status else None,
                                          reason))
            continue
        events = store.read_events(row.run_id)
        chain = chain_state(events, keyring)
        entry = Archived(run_id=row.run_id, status=summary(row.run_id, events)["status"],
                         events=len(events), chain_verified=chain["verified"],
                         error=chain["error"])
        if chain["verified"]:
            try:
                run = fold(events)
            except FoldError as exc:
                report.skipped.append(Skipped(row.run_id, entry.status, f"does not fold: {exc}"))
                continue
            if run.status.value not in TERMINAL or events[-1].occurred_at >= cutoff:
                report.skipped.append(Skipped(row.run_id, run.status.value, "index drift"))
                continue
        if dry_run:
            (report.archived if chain["verified"] else report.kept).append(entry)
            continue
        out.mkdir(mode=0o700, parents=True, exist_ok=True)         # holds run data, like the db
        path = out / f"{row.run_id}.jsonl"
        chain, entry.sha256, entry.bytes, last_seq = _write_archive_file(
            path, store, blobs, row.run_id, keyring=keyring, include_blobs=include_blobs,
            exported_at=exported_at)
        entry.file, entry.events, entry.chain_verified, entry.error = (
            path.name, last_seq, chain["verified"], chain["error"])
        if entry.chain_verified:
            refs = referenced_blobs([ev.to_record() for ev in events]) if include_blobs else ()
            try:
                entry.blobs_removed = store.purge_run(row.run_id, expected_seq=last_seq, blobs=refs)
            except ConflictError as exc:
                report.skipped.append(Skipped(row.run_id, entry.status,
                                              f"appended during archive: {exc}"))
                continue
            entry.purged = True
            report.archived.append(entry)
        else:
            report.kept.append(entry)
        _append_manifest(out / MANIFEST, entry, exported_at)
        report.manifest = str(out / MANIFEST)
    return report
