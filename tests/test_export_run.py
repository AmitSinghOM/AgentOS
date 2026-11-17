"""`agentos export-run --format jsonl` (ROADMAP A11, audit E-08): one run's record as a file
an auditor or an external evaluator (opik, langfuse) can take away.

  * shape: a header line, every event in seq order exactly as stored (the dicts
    `GET /runs/{id}/events` returns, hashes included), one summary line from the fold;
  * every line is canonical JSON, so the chain re-verifies from the file with the stdlib alone;
  * re-exporting is byte-identical except `exported_at`; a file is created 0600;
  * `--all` is one stream, a header naming the run before each run's lines;
  * a tampered log still exports, marked `verified: false`, and the command exits 1;
  * `--include-blobs` adds every referenced blob once, between the events and the summary;
  * an unknown run id exits 2, names the id and writes nothing;
  * round trip: re-folding the file's events (outputs hydrated from its blob lines) equals the
    live `GET /runs/{id}` — state is reconstructible from the file alone.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json

import pytest

from dagentos.cli import main as cli_main
from dagentos.core.engine import Engine
from dagentos.core.events import from_record
from dagentos.core.fold import fold
from dagentos.core.integrity import chain
from dagentos.core.models import (
    Agent,
    AgentType,
    BlobRef,
    Cost,
    Effect,
    EffectClass,
    Principal,
    PrincipalKind,
    Provenance,
    StepResult,
    WorkflowDefinition,
    WorkflowRun,
)
from dagentos.core.seal import HmacKeyring
from dagentos.store.sqlite import SqliteStore
from tests.test_seal_and_cli import _keyring_file

HUMAN = Principal(kind=PrincipalKind.human, id="amit")


class PricedExecutor:
    """Echo-shaped, but every step's cost names a pricing table held in the BlobStore — the
    third kind of blob a log references, next to run inputs and step outputs."""

    name, version = "priced", "test"

    def __init__(self, pricing: BlobRef) -> None:
        self.pricing = pricing

    def execute(self, req, progress):
        return StepResult(output={"step": req.step_id},
                          effects=[Effect(effect_class=c) for c in req.declared_effects],
                          cost=Cost(amount="0.25", pricing_snapshot_hash=self.pricing.sha256),
                          provenance=Provenance(executor="priced", executor_version="test"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "export.db"))
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(_keyring_file(tmp_path)))
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_AUTH", raising=False)
    return tmp_path


def _seed(tmp_path, *, complete: bool = True):
    """n1 (compute) → pay (spend, gated). Completed: the log holds an approval, two seals and
    priced costs. Not completed: the run is left suspended on the approval."""
    store = SqliteStore(str(tmp_path / "export.db"))
    keyring = HmacKeyring.from_file(tmp_path / "keys.json")
    pricing = store.put(b'{"table": "test-prices-v1"}')
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="w", nodes=[
        {"id": "n1", "agent": "calc"}, {"id": "pay", "agent": "payer", "depends_on": ["n1"]}]))
    eng = Engine(store=store, blobs=store, executors={"echo": PricedExecutor(pricing)},
                 lease=store, keyring=keyring)
    run = eng.start_run("w", inputs={"topic": "export"})
    if complete:
        (approval,) = run.approvals.values()
        eng.approve(run.id, approval.approval_id, principal=HUMAN)
        run = eng.advance_until_terminal(run.id)
    return store, run, pricing


def _canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def read_export(text: str) -> list[dict]:
    """What an evaluator does with the file: split it per run, classify each line. An event
    line is any line with `event_type` (`approval.requested` has a `kind` of its own)."""
    runs: list[dict] = []
    for line in text.splitlines():
        doc = json.loads(line)
        if "event_type" in doc:
            runs[-1]["events"].append(doc)
        elif doc["kind"] == "agentos.export":
            runs.append({"header": doc, "events": [], "blobs": [], "summary": None})
        elif doc["kind"] == "blob":
            runs[-1]["blobs"].append(doc)
        elif doc["kind"] == "summary":
            runs[-1]["summary"] = doc
        else:
            raise AssertionError(f"unclassifiable line: {line}")
    return runs


def refold(block: dict) -> WorkflowRun:
    """State from the file alone: fold the event lines, hydrate from the blob lines."""
    blobs = {b["sha256"]: base64.b64decode(b["bytes_b64"]) for b in block["blobs"]}
    run = fold([from_record(r) for r in block["events"]])
    for step in run.steps:
        if step.output_ref is not None:
            step.output = json.loads(blobs[step.output_ref.sha256])
    if run.inputs_ref is not None:
        run.inputs = json.loads(blobs[run.inputs_ref.sha256])
    return run


# ------------------------------------------------------------------ shape

def test_header_then_every_event_as_stored_then_the_fold_summary(env, capsys):
    store, run, _ = _seed(env)
    assert cli_main(["export-run", "--run", run.id, "--format", "jsonl"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    docs = [json.loads(line) for line in lines]
    header, *events, summary = docs
    stored = store.read_events(run.id)

    assert set(header) == {"kind", "version", "run_id", "exported_at", "chain"}
    assert (header["kind"], header["version"], header["run_id"]) == ("agentos.export", 1, run.id)
    sealed_at = next(e.seq for e in stored if type(e).event_type == "run.completed")
    assert header["chain"] == {"verified": True, "sealed_through": sealed_at,
                               "seals": "verified", "error": None}
    assert events == [e.to_record() for e in stored]               # exactly as stored, in seq order
    assert [e["seq"] for e in events] == list(range(1, len(stored) + 1))
    assert all(e["hash"] for e in events)
    # the classification rule is needed: an approval event carries its own `kind`
    assert any(e["event_type"] == "approval.requested" and "kind" in e for e in events)
    assert summary == {"kind": "summary", "run_id": run.id, "status": "completed",
                       "events": len(stored), "cost": run.total_cost, "error": None}
    assert run.total_cost == "0.50"                                 # the fold's own number
    assert not any(d.get("kind") == "blob" and "event_type" not in d for d in docs)
    # canonical lines: the chain re-verifies from the file with the standard library alone
    assert lines == [_canonical(d) for d in docs] and out.endswith("\n")
    prev = None
    for line in lines[1:-1]:
        rec = json.loads(line)
        claimed = rec.pop("hash")
        assert rec["prev_hash"] == prev
        assert hashlib.sha256(_canonical(rec).encode()).hexdigest() == claimed
        prev = claimed


def test_re_export_is_byte_identical_except_exported_at_and_the_file_is_0600(env):
    _store, run, _ = _seed(env)
    a, b = env / "a.jsonl", env / "b.jsonl"
    for path in (a, b):
        assert cli_main(["export-run", "--run", run.id, "--format", "jsonl", "--include-blobs",
                         "--out", str(path)]) == 0
    la, lb = a.read_bytes().split(b"\n"), b.read_bytes().split(b"\n")
    assert la[1:] == lb[1:] and len(la) > 3
    ha, hb = json.loads(la[0]), json.loads(lb[0])
    assert ha.pop("exported_at") and hb.pop("exported_at")
    assert ha == hb
    assert a.stat().st_mode & 0o777 == 0o600


def test_all_is_one_stream_with_a_header_per_run(env, capsys):
    store, done, _ = _seed(env)
    _store, suspended, _ = _seed(env, complete=False)
    assert cli_main(["export-run", "--all", "--format", "jsonl"]) == 0
    blocks = read_export(capsys.readouterr().out)
    assert [b["header"]["run_id"] for b in blocks] == store.list_run_ids() == [done.id, suspended.id]
    assert [b["summary"]["status"] for b in blocks] == ["completed", "suspended"]
    for block in blocks:
        rid = block["header"]["run_id"]
        assert block["summary"]["run_id"] == rid
        assert block["events"] == [e.to_record() for e in store.read_events(rid)]


# ------------------------------------------------------------------ tampering

def _tamper(store, run_id: str, how: str) -> None:
    records = [e.to_record() for e in store.read_events(run_id)]
    records[0]["workflow"] = "someone-elses"
    if how == "edit":                     # no rechain: the hash chain itself breaks
        rows = [(records[0]["seq"], records[0])]
    else:                                 # rechain: the chain verifies, the seal does not
        fixed = chain([from_record(dict(r, hash=None, prev_hash=None)) for r in records], 0, None)
        rows = [(ev.seq, ev.to_record()) for ev in fixed]
    for seq, rec in rows:
        store._conn.execute("UPDATE run_events SET record = ? WHERE run_id = ? AND seq = ?",
                            (json.dumps(rec), run_id, seq))
    store._conn.commit()


@pytest.mark.parametrize(("how", "problem", "status"), [
    ("edit", "content does not match its hash", None),
    ("rechain", "does not carry the sealed hash", "completed"),
])
def test_a_tampered_run_still_exports_marked_unverified_and_exits_1(env, capsys, how, problem,
                                                                     status):
    store, run, _ = _seed(env)
    _tamper(store, run.id, how)
    assert cli_main(["export-run", "--run", run.id, "--format", "jsonl"]) == 1
    captured = capsys.readouterr()
    (block,) = read_export(captured.out)
    assert block["header"]["chain"]["verified"] is False
    assert problem in block["header"]["chain"]["error"]
    assert block["events"] == [e.to_record() for e in store.read_events(run.id)]  # still all there
    assert block["events"][0]["workflow"] == "someone-elses"        # as stored, not repaired
    # the fold refuses a broken chain, so the summary states no status it cannot stand behind
    assert block["summary"]["status"] == status
    assert (block["summary"]["error"] is None) == (status is not None)
    assert run.id in captured.err and "verified=false" in captured.err


# ------------------------------------------------------------------ blobs

def test_include_blobs_emits_every_referenced_blob_once_before_the_summary(env, capsys):
    store, run, pricing = _seed(env)
    assert cli_main(["export-run", "--run", run.id, "--format", "jsonl", "--include-blobs"]) == 0
    docs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    blob_at = [i for i, d in enumerate(docs) if d.get("kind") == "blob" and "event_type" not in d]
    blobs = [docs[i] for i in blob_at]
    by_node = {s.node_id: s for s in run.steps}
    assert [b["sha256"] for b in blobs] == [            # first reference first, once each
        run.inputs_ref.sha256, by_node["n1"].output_ref.sha256, pricing.sha256,
        by_node["pay"].output_ref.sha256]
    assert blob_at == list(range(len(docs) - 1 - len(blobs), len(docs) - 1))   # then the summary
    for b in blobs:
        data = base64.b64decode(b["bytes_b64"])
        assert set(b) == {"kind", "sha256", "bytes_b64"}
        assert hashlib.sha256(data).hexdigest() == b["sha256"]
        assert data == store.get(BlobRef(sha256=b["sha256"], size=0))


def test_a_referenced_blob_missing_from_the_store_is_a_marked_line_not_a_gap(env, capsys):
    store, run, pricing = _seed(env)
    store._conn.execute("DELETE FROM blobs WHERE sha256 = ?", (pricing.sha256,))
    store._conn.commit()
    assert cli_main(["export-run", "--run", run.id, "--format", "jsonl", "--include-blobs"]) == 0
    captured = capsys.readouterr()
    (block,) = read_export(captured.out)
    (gone,) = [b for b in block["blobs"] if b["sha256"] == pricing.sha256]
    assert gone == {"kind": "blob", "sha256": pricing.sha256, "bytes_b64": None, "missing": True}
    assert pricing.sha256 in captured.err


# ------------------------------------------------------------------ usage

def test_an_unknown_run_exits_2_names_it_and_writes_nothing(env, capsys):
    _seed(env)
    out = env / "never.jsonl"
    assert cli_main(["export-run", "--run", "nope", "--format", "jsonl", "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert "unknown run 'nope'" in captured.err and captured.out == ""
    assert not out.exists()


# ------------------------------------------------------------------ round trip

def test_round_trip_refolding_the_file_equals_the_live_api(env, monkeypatch):
    """An external evaluator reconstructs state from the file alone: every run's events,
    re-folded and hydrated from the file's blob lines, equal `GET /runs/{id}`; the event lines
    equal `GET /runs/{id}/events`."""
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
                  json={"inputs": {"topic": "export"}}).json()
    parked = c.post("/workflows/gated/runs", params={"sync": "true"}).json()
    assert (done["status"], parked["status"]) == ("completed", "suspended")

    path = env / "all.jsonl"
    assert cli_main(["export-run", "--all", "--format", "jsonl", "--include-blobs",
                     "--out", str(path)]) == 0
    blocks = read_export(path.read_text())
    assert [b["header"]["run_id"] for b in blocks] == [done["id"], parked["id"]]
    for block in blocks:
        rid = block["header"]["run_id"]
        live = c.get(f"/runs/{rid}").json()
        assert refold(block) == WorkflowRun.model_validate(live)
        assert block["events"] == c.get(f"/runs/{rid}/events").json()["data"]
        assert (block["summary"]["status"], block["summary"]["cost"],
                block["summary"]["events"]) == (live["status"], live["total_cost"],
                                                live["last_seq"])
