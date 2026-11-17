"""`agentos archive --older-than <duration>` (audit E-08, second half): a bound on disk that
keeps every guarantee of the log. Export-then-purge, never the reverse:

  * dry run is the default: lists what would leave, writes nothing, changes nothing;
  * `--yes` exports each terminal run older than the cutoff with the A11 writer to
    `<out>/<run_id>.jsonl`, fsyncs it, re-reads it, and only then removes the run's rows in one
    transaction; the file re-folds to the pre-purge `GET /runs/{id}`;
  * a run whose chain does not verify is exported but KEPT — the store never loses the only
    evidence of a tampering — and the exit is 1;
  * a non-terminal run is never touched, and the decision is made on the fold, not the index;
  * blobs leave only with `--include-blobs`, and only those no remaining run references;
  * `<out>/ARCHIVE.jsonl` gets one row per exported run with the file's sha256;
  * a crash between export and purge leaves the run in the store; the re-run is idempotent.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dagentos import cli, export
from dagentos.cli import main as cli_main
from dagentos.core.events import RunFailed, RunStarted
from dagentos.core.models import WorkflowRun
from dagentos.store.sqlite import SqliteStore
from tests.test_export_run import _seed, _tamper, read_export, refold
from tests.test_seal_and_cli import _keyring_file


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "export.db"))
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(_keyring_file(tmp_path)))
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_AUTH", raising=False)
    return tmp_path


@pytest.fixture
def later(monkeypatch):
    """The CLI's clock, two days ahead: a run seeded now is then older than `1d`."""
    at = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(cli, "_now", lambda: at)
    return at


def _archive(*extra: str, out: Path) -> tuple[int, dict, str]:
    """Run `agentos --json archive --older-than 1d --out OUT …`, return (exit, doc, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(["--json", "archive", "--older-than", "1d", "--out", str(out), *extra])
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _records(store, run_id: str) -> list[dict]:
    return [e.to_record() for e in store.read_events(run_id)]


def _old_failed_run(store, run_id: str) -> None:
    """A hand-built failed run dated 2026-01-01: terminal, unhashed (verifies trivially)."""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    store.append_events(run_id, 0, [
        RunStarted(run_id=run_id, workflow="w", workflow_version=1, request_id=f"req-{run_id}",
                   occurred_at=at),
        RunFailed(run_id=run_id, error="boom", occurred_at=at + timedelta(minutes=1))])


# ------------------------------------------------------------------ dry run (default)

def test_dry_run_lists_terminal_runs_older_than_the_cutoff_and_changes_nothing(env, later, capsys):
    store, done, _ = _seed(env)
    _, parked, _ = _seed(env, complete=False)
    out = env / "archive"
    before = {rid: _records(store, rid) for rid in store.list_run_ids()}

    code, doc, _err = _archive(out=out)
    assert code == 0
    assert doc["dry_run"] is True and doc["cutoff"] == (later - timedelta(days=1)).isoformat()
    (row,) = doc["archived"]
    assert (row["run_id"], row["status"], row["events"]) == (done.id, "completed", len(before[done.id]))
    assert row["chain_verified"] is True and row["purged"] is False
    assert doc["kept"] == []
    assert doc["skipped"] == [{"run_id": parked.id, "status": "suspended", "reason": "non-terminal"}]
    assert not out.exists()                                           # nothing written at all
    assert {rid: _records(store, rid) for rid in store.list_run_ids()} == before

    assert cli_main(["archive", "--older-than", "1d", "--out", str(out)]) == 0   # plain text
    text = capsys.readouterr().out
    assert "dry run" in text and done.id in text and "would archive" in text
    assert "non-terminal" in text and parked.id in text
    assert not out.exists()


def test_a_run_newer_than_the_cutoff_is_kept(env, capsys):
    store, done, _ = _seed(env)                                       # finished just now
    code, doc, _err = _archive("--yes", out=env / "archive")
    assert code == 0
    assert doc["archived"] == [] and doc["kept"] == []
    assert doc["skipped"] == [{"run_id": done.id, "status": "completed", "reason": "too recent"}]
    assert store.list_run_ids() == [done.id] and not (env / "archive").exists()


def test_status_selects_a_subset_of_the_terminal_statuses(env, later, capsys):
    store, done, _ = _seed(env)
    _old_failed_run(store, "f1")
    out = env / "archive"
    code, doc, _err = _archive("--status", "failed", "--yes", out=out)
    assert code == 0
    assert [a["run_id"] for a in doc["archived"]] == ["f1"]
    assert doc["skipped"] == [{"run_id": done.id, "status": "completed",
                               "reason": "status not selected"}]
    assert store.list_run_ids() == [done.id] and (out / "f1.jsonl").exists()


# ------------------------------------------------------------------ --yes: export, verify, purge

def test_yes_exports_then_purges_and_the_file_refolds_to_the_pre_purge_run(env, later, monkeypatch,
                                                                            capsys):
    """Round trip through the live API (the A11 helper): the file alone reconstructs the run
    the API served before the purge, and afterwards the store simply no longer has it."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    from dagentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    assert c.post("/agents", json={"name": "calc", "type": "echo"}).status_code == 201
    assert c.post("/agents", json={"name": "payer", "type": "echo",
                                   "declared_effects": ["compute", "spend"]}).status_code == 201
    c.post("/workflows", json={"name": "plain", "nodes": [{"id": "n", "agent": "calc"}]})
    c.post("/workflows", json={"name": "gated", "nodes": [
        {"id": "n1", "agent": "calc"}, {"id": "pay", "agent": "payer", "depends_on": ["n1"]}]})
    done = c.post("/workflows/plain/runs", params={"sync": "true"},
                  json={"inputs": {"topic": "archive"}}).json()
    parked = c.post("/workflows/gated/runs", params={"sync": "true"}).json()
    assert (done["status"], parked["status"]) == ("completed", "suspended")
    live = c.get(f"/runs/{done['id']}").json()
    live_events = c.get(f"/runs/{done['id']}/events").json()["data"]

    out = env / "archive"
    code, doc, _err = _archive("--include-blobs", "--yes", out=out)
    assert code == 0
    (row,) = doc["archived"]
    assert (row["run_id"], row["purged"], row["chain_verified"]) == (done["id"], True, True)
    assert doc["skipped"] == [{"run_id": parked["id"], "status": "suspended",
                               "reason": "non-terminal"}]

    path = out / f"{done['id']}.jsonl"
    assert row["file"] == path.name
    assert out.stat().st_mode & 0o777 == 0o700 and path.stat().st_mode & 0o777 == 0o600
    (block,) = read_export(path.read_text())
    assert block["header"]["chain"]["verified"] is True
    assert block["events"] == live_events
    assert refold(block) == WorkflowRun.model_validate(live)
    assert block["summary"]["status"] == "completed"
    assert not list(out.glob("*.part"))

    # gone from the store: the file is the record now
    assert c.get(f"/runs/{done['id']}").status_code == 404
    assert c.get(f"/runs/{done['id']}/events").status_code == 404
    assert c.get(f"/runs/{done['id']}/integrity").status_code == 404
    listing = c.get("/runs").json()
    assert [r["id"] for r in listing["data"]] == [parked["id"]]
    assert listing["counts"] == {"suspended": 1}
    assert cli_main(["verify", "--all"]) == 0
    verify_out = capsys.readouterr().out
    assert parked["id"] in verify_out and done["id"] not in verify_out
    assert cli_main(["doctor"]) == 0


def test_a_tampered_run_is_exported_but_kept_and_reported(env, later):
    store, run, _ = _seed(env)
    _tamper(store, run.id, "edit")
    before = _records(store, run.id)
    out = env / "archive"

    code, doc, err = _archive("--yes", out=out)
    assert code == 1
    assert doc["archived"] == []
    (kept,) = doc["kept"]
    assert (kept["run_id"], kept["chain_verified"], kept["purged"]) == (run.id, False, False)
    assert "content does not match its hash" in kept["error"]
    assert run.id in err and "kept" in err
    (block,) = read_export((out / f"{run.id}.jsonl").read_text())
    assert block["header"]["chain"]["verified"] is False
    assert block["events"] == before                                  # the evidence, as stored
    assert _records(store, run.id) == before and store.list_run_ids() == [run.id]
    (row,) = _rows(out / "ARCHIVE.jsonl")
    assert (row["run_id"], row["chain_verified"], row["purged"]) == (run.id, False, False)


def test_a_rechained_run_whose_seal_fails_is_kept_too(env, later):
    store, run, _ = _seed(env)
    _tamper(store, run.id, "rechain")                                 # chain ok, seal INVALID
    code, doc, _err = _archive("--yes", out=env / "archive")
    assert code == 1 and doc["archived"] == []
    assert doc["kept"][0]["run_id"] == run.id
    assert "does not carry the sealed hash" in doc["kept"][0]["error"]
    assert store.list_run_ids() == [run.id]


def test_a_non_terminal_run_is_skipped_even_with_yes(env, later):
    store, parked, _ = _seed(env, complete=False)
    out = env / "archive"
    code, doc, _err = _archive("--yes", out=out)
    assert code == 0
    assert doc["archived"] == [] and doc["kept"] == []
    assert doc["skipped"] == [{"run_id": parked.id, "status": "suspended", "reason": "non-terminal"}]
    assert store.list_run_ids() == [parked.id] and store.read_events(parked.id)
    assert not (out / f"{parked.id}.jsonl").exists()
    assert not (out / "ARCHIVE.jsonl").exists()


def test_the_decision_is_made_on_the_fold_not_the_index_row(env, later):
    """An index row that lies (hand edit, drift) cannot make a live run leave: the run is
    re-folded before anything is written and skipped when the fold is not terminal."""
    store, parked, _ = _seed(env, complete=False)
    store._conn.execute("UPDATE runs SET status = 'completed' WHERE run_id = ?", (parked.id,))
    store._conn.commit()
    code, doc, _err = _archive("--yes", out=env / "archive")
    assert code == 0 and doc["archived"] == []
    assert doc["skipped"] == [{"run_id": parked.id, "status": "suspended", "reason": "index drift"}]
    assert store.read_events(parked.id) and not (env / "archive" / f"{parked.id}.jsonl").exists()


# ------------------------------------------------------------------ blobs

def test_include_blobs_removes_only_blobs_no_remaining_run_references(env, later):
    """Both runs share the inputs, the n1 output and the pricing table (content-addressed);
    only the completed run's `pay` output is its alone — that is the one blob that leaves."""
    store, done, pricing = _seed(env)
    _, parked, _ = _seed(env, complete=False)
    by_node = {s.node_id: s for s in done.steps}
    shared = [done.inputs_ref, by_node["n1"].output_ref, pricing]
    own = by_node["pay"].output_ref
    out = env / "archive"

    code, doc, _err = _archive("--include-blobs", "--yes", out=out)
    assert code == 0
    (row,) = doc["archived"]
    assert row["blobs_removed"] == [own.sha256]
    assert all(store.exists(ref) for ref in shared) and not store.exists(own)
    (block,) = read_export((out / f"{done.id}.jsonl").read_text())
    assert sorted(b["sha256"] for b in block["blobs"]) == sorted(r.sha256 for r in [*shared, own])
    assert (_rows(out / "ARCHIVE.jsonl")[0]["blobs_removed"]) == [own.sha256]
    assert store.read_events(parked.id)                               # the other run, untouched


def test_without_include_blobs_every_blob_stays_in_the_store(env, later):
    store, done, pricing = _seed(env)
    refs = [done.inputs_ref, pricing, *(s.output_ref for s in done.steps)]
    code, doc, _err = _archive("--yes", out=env / "archive")
    assert code == 0 and doc["archived"][0]["blobs_removed"] == []
    assert all(store.exists(ref) for ref in refs)
    (block,) = read_export((env / "archive" / f"{done.id}.jsonl").read_text())
    assert block["blobs"] == []                                      # referenced by hash only
    assert store.list_run_ids() == []


# ------------------------------------------------------------------ manifest

def test_the_manifest_has_one_row_per_exported_run_with_the_file_sha256_and_appends(env, later):
    store, a, _ = _seed(env)
    _, b, _ = _seed(env)
    out = env / "archive"
    code, doc, _err = _archive("--yes", out=out)
    assert code == 0 and [r["run_id"] for r in doc["archived"]] == [a.id, b.id]
    rows = _rows(out / "ARCHIVE.jsonl")
    assert [r["run_id"] for r in rows] == [a.id, b.id]
    assert doc["manifest"] == str(out / "ARCHIVE.jsonl")
    assert (out / "ARCHIVE.jsonl").stat().st_mode & 0o777 == 0o600
    for row in rows:
        data = (out / row["file"]).read_bytes()
        assert set(row) == {"kind", "version", "run_id", "exported_at", "file", "sha256", "bytes",
                            "events", "status", "chain_verified", "purged", "blobs_removed"}
        assert (row["kind"], row["version"]) == ("agentos.archive", 1)
        assert row["sha256"] == hashlib.sha256(data).hexdigest() and row["bytes"] == len(data)
        (block,) = read_export(data.decode())
        assert row["events"] == len(block["events"]) == block["summary"]["events"]
        assert (row["status"], row["chain_verified"], row["purged"]) == ("completed", True, True)
        assert row["exported_at"] == block["header"]["exported_at"] == doc["exported_at"]
    assert store.list_run_ids() == []

    _, c_, _ = _seed(env)                                             # a later archive appends
    code, doc, _err = _archive("--yes", out=out)
    assert code == 0 and [r["run_id"] for r in doc["archived"]] == [c_.id]
    rows2 = _rows(out / "ARCHIVE.jsonl")
    assert rows2[:2] == rows and rows2[2]["run_id"] == c_.id
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["ARCHIVE.jsonl", f"{a.id}.jsonl", f"{b.id}.jsonl", f"{c_.id}.jsonl"])


# ------------------------------------------------------------------ failure ordering

def test_a_failed_export_purges_nothing_and_leaves_no_file(env, later, monkeypatch, capsys):
    store, run, _ = _seed(env)
    before = _records(store, run.id)
    out = env / "archive"

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export, "write_run", disk_full)
    assert cli_main(["archive", "--older-than", "1d", "--out", str(out), "--yes"]) == 2
    assert "No space left" in capsys.readouterr().err
    assert _records(store, run.id) == before and store.list_run_ids() == [run.id]
    assert not (out / f"{run.id}.jsonl").exists()                     # no half-written record
    assert not (out / "ARCHIVE.jsonl").exists()


def test_a_crash_between_export_and_purge_leaves_the_run_and_the_rerun_finishes(env, later, capsys):
    store, run, _ = _seed(env)
    before = _records(store, run.id)
    out = env / "archive"
    path = out / f"{run.id}.jsonl"

    def crash(self, run_id, *, expected_seq, blobs=()):
        raise RuntimeError("simulated crash after the export, before the purge")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SqliteStore, "purge_run", crash)
        assert cli_main(["archive", "--older-than", "1d", "--out", str(out), "--yes"]) == 2
    assert "simulated crash" in capsys.readouterr().err
    assert path.exists()                                              # the export landed …
    first = path.read_bytes()
    assert _records(store, run.id) == before                          # … the run did not leave
    assert not (out / "ARCHIVE.jsonl").exists()                       # and nothing claims it did

    code, doc, _err = _archive("--yes", out=out)                      # idempotent re-run
    assert code == 0 and [r["run_id"] for r in doc["archived"]] == [run.id]
    assert path.read_bytes() == first                                 # same clock, same bytes
    assert store.list_run_ids() == []
    (row,) = _rows(out / "ARCHIVE.jsonl")
    assert row["sha256"] == hashlib.sha256(first).hexdigest()


# ------------------------------------------------------------------ usage

@pytest.mark.parametrize("argv, message", [
    (["--older-than", "soon"], "--older-than"),
    (["--older-than", "30x"], "--older-than"),
    (["--older-than", "1d", "--status", "running"], "--status"),
    (["--older-than", "1d", "--status", "completed,nope"], "--status"),
])
def test_usage_errors_exit_2_and_touch_nothing(env, later, capsys, argv, message):
    store, run, _ = _seed(env)
    out = env / "archive"
    assert cli_main(["archive", *argv, "--out", str(out), "--yes"]) == 2
    assert message in capsys.readouterr().err
    assert store.list_run_ids() == [run.id] and not out.exists()


def test_dry_run_and_yes_are_exclusive(env):
    with pytest.raises(SystemExit):
        cli_main(["archive", "--older-than", "1d", "--out", str(env / "a"), "--dry-run", "--yes"])


def test_duration_grammar():
    assert cli._duration("30d") == timedelta(days=30)
    assert cli._duration("12h") == timedelta(hours=12)
    assert cli._duration("90m") == timedelta(minutes=90)
    assert cli._duration("2w") == timedelta(weeks=2)
    assert cli._duration("0d") == timedelta(0)
    for bad in ("", "d", "1", "-1d", "1.5d", "1s", "1 d"):
        with pytest.raises(ValueError, match="--older-than"):
            cli._duration(bad)
