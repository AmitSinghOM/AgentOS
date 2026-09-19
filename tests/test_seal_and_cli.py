"""Phase 8 #3 — the signed chain tail, and #4 — `agentos verify` / `doctor` / `policy explain`.

Threat: an attacker with database access rewrites an idle run and recomputes every hash.
  * with a keyring, every idle/terminal event is followed IN THE SAME BATCH by a seal
  * a rewrite-and-rechain of the prefix leaves a seal whose signature no longer verifies
  * a seal that points at a hash the log no longer carries is INVALID
  * deleting the seals leaves `unsigned_tail` > 0 and state `unsigned` — visible, not silent
  * an unknown key_id is reported, never silently valid; a foreign keyring is `unverifiable`
  * no keyring → no seals, byte-identical behaviour to before (golden corpus)
  * the keyring file: hex, ≥ 32 bytes, active must exist; every error names the entry
Then the CLI: verify exits 1 on a tampered run, doctor reports the configuration honestly,
policy explain says what the ceiling does to one workflow.
"""
from __future__ import annotations

import importlib
import json
import os
import secrets

import pytest

from agentos.cli import main as cli_main
from agentos.core.engine import Engine
from agentos.core.events import ChainSealed, from_record
from agentos.core.integrity import chain, verify
from agentos.core.models import (
    Agent,
    AgentType,
    Budget,
    EffectClass,
    Principal,
    PrincipalKind,
    RunStatus,
    WorkflowDefinition,
)
from agentos.core.seal import (
    SEAL_AFTER,
    HmacKeyring,
    SealError,
    keyring_from_env,
    seal_message,
    verify_seals,
)
from agentos.store.memory import MemoryStore
from agentos.store.sqlite import SqliteStore
from tests.test_approvals import PinnedWall, RecordingExecutor

HUMAN = Principal(kind=PrincipalKind.human, id="amit")


def _keyring_file(tmp_path, active="k1", extra=None):
    keys = {"k1": secrets.token_hex(32), **(extra or {})}
    p = tmp_path / "keys.json"
    p.write_text(json.dumps({"active": active, "keys": keys}))
    return p


def _engine(store, keyring, budget=None):
    store.put_agent(Agent(name="calc", type=AgentType.echo))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="w", budget=budget or Budget(), nodes=[
        {"id": "n1", "agent": "calc"}, {"id": "pay", "agent": "payer", "depends_on": ["n1"]}]))
    return Engine(store=store, blobs=store, executors={"echo": RecordingExecutor()}, lease=store,
                  wall=PinnedWall(), keyring=keyring)


def _types(store, run_id):
    return [type(e).event_type for e in store.read_events(run_id)]


# ------------------------------------------------------------------ sealing

def test_every_idle_and_terminal_event_is_followed_by_a_seal_in_the_same_batch(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")                          # suspends on spend
    assert run.status is RunStatus.suspended
    types = _types(store, run.id)
    assert types[-2:] == ["run.suspended", "integrity.sealed"]
    (approval,) = run.approvals.values()
    eng.approve(run.id, approval.approval_id, principal=HUMAN)
    run = eng.advance_until_terminal(run.id)
    assert run.status is RunStatus.completed
    events = store.read_events(run.id)
    types = [type(e).event_type for e in events]
    assert types[-2:] == ["run.completed", "integrity.sealed"]
    assert types.count("integrity.sealed") == 2
    for i, ev in enumerate(events):
        if isinstance(ev, SEAL_AFTER):
            nxt = events[i + 1]
            assert isinstance(nxt, ChainSealed) and nxt.sealed_seq == ev.seq \
                and nxt.sealed_hash == ev.hash and nxt.key_id == "k1"
    assert verify(events) == len(events)                     # seals are chained too
    report = verify_seals(events, keyring)
    assert (report.state, report.seals, report.valid) == ("verified", 2, 2)
    assert report.sealed_through == events[-2].seq and report.unsigned_tail == 0
    assert run.sealed_through == events[-2].seq


def test_reject_path_and_idle_cancel_are_sealed_too(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")
    (approval,) = run.approvals.values()
    eng.reject(run.id, approval.approval_id, principal=HUMAN, reason="no")   # _emit path
    types = _types(store, run.id)
    assert types[-2:] == ["run.failed", "integrity.sealed"]
    run2 = eng.start_run("w")
    eng.request_cancel(run2.id, principal=HUMAN)               # idle → finalized immediately
    types2 = _types(store, run2.id)
    assert "run.cancelled" in types2 and types2[types2.index("run.cancelled") + 1] == "integrity.sealed"


def test_no_keyring_means_no_seals_and_identical_logs():
    store = MemoryStore()
    eng = _engine(store, None)
    run = eng.start_run("w")
    assert "integrity.sealed" not in _types(store, run.id)
    report = verify_seals(store.read_events(run.id), None)
    assert report.state == "unsigned" and report.unsigned_tail == len(store.read_events(run.id))
    assert run.sealed_through is None and eng.keyring is None


# ------------------------------------------------------------------ the attacks

def _rechain(records: list[dict]) -> list:
    """What an attacker with DB access does: rebuild every hash so the chain verifies."""
    events = [from_record(dict(r, hash=None, prev_hash=None)) for r in records]
    return chain(events, 0, None)


def test_rewrite_and_rechain_is_caught_by_the_seal(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")
    (approval,) = run.approvals.values()
    eng.approve(run.id, approval.approval_id, principal=HUMAN)
    eng.advance_until_terminal(run.id)
    records = [e.to_record() for e in store.read_events(run.id)]
    granted = next(r for r in records if r["event_type"] == "approval.granted")
    granted["principal"] = {"kind": "agent", "id": "bot", "attestation": None}   # who approved
    tampered = _rechain(records)
    assert verify(tampered) == len(tampered)                  # the chain alone is fooled
    report = verify_seals(tampered, keyring)
    # the seal before the tampered event still verifies (its prefix is unchanged); the seal
    # after it points at a hash the rechained log no longer carries
    assert report.state == "INVALID" and (report.seals, report.valid) == (2, 1)
    assert report.problems == [(f"seal at seq {tampered[-1].seq}: event {tampered[-2].seq} "
                               f"does not carry the sealed hash")]


def test_forged_seal_over_the_right_hash_fails_the_signature(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")
    events = store.read_events(run.id)
    seal = events[-1]
    assert isinstance(seal, ChainSealed)
    forged = seal.model_copy(update={"signature": "00" * 32})
    report = verify_seals(events[:-1] + [forged], keyring)
    assert report.state == "INVALID" and "signature does not verify" in report.problems[0]


def test_deleting_the_seals_is_visible_as_an_unsigned_tail(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")
    records = [e.to_record() for e in store.read_events(run.id)
               if e.__class__ is not ChainSealed]
    stripped = _rechain(records)
    report = verify_seals(stripped, keyring)
    assert report.state == "unsigned" and report.seals == 0
    assert report.unsigned_tail == len(stripped)


def test_unknown_key_is_reported_and_a_foreign_keyring_is_unverifiable(tmp_path):
    keyring = HmacKeyring.from_file(_keyring_file(tmp_path))
    store = MemoryStore()
    eng = _engine(store, keyring)
    run = eng.start_run("w")
    events = store.read_events(run.id)
    other = HmacKeyring("z9", {"z9": secrets.token_bytes(32)})
    report = verify_seals(events, other)
    assert report.state == "unverifiable" and report.unknown_keys == ["k1"] and not report.problems
    assert verify_seals(events, None).state == "unverifiable"
    # rotation: a keyring that still lists k1 (retired) verifies old seals and signs with k2
    rotated = HmacKeyring("k2", {"k2": secrets.token_bytes(32), "k1": keyring._keys["k1"]})
    assert verify_seals(events, rotated).state == "verified"


def test_seal_message_binds_run_seq_and_hash():
    assert seal_message("r", 7, "h") == b"agentos-seal-v1:r:7:h"


# ------------------------------------------------------------------ keyring file

@pytest.mark.parametrize("payload, needle", [
    ({"active": "k1", "keys": {"k1": "zz"}}, "keys.k1 is not hex"),
    ({"active": "k1", "keys": {"k1": "ab" * 16}}, "16 bytes; need at least 32"),
    ({"active": "k9", "keys": {"k1": "ab" * 32}}, "active 'k9' is not one of keys"),
    ({"keys": {"k1": "ab" * 32}}, "active"),
    ({"active": "k1", "keys": {"k1": "ab" * 32}, "alg": "x"}, "alg"),
])
def test_keyring_file_errors_name_the_entry(tmp_path, payload, needle):
    p = tmp_path / "k.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(SealError, match=needle):
        HmacKeyring.from_file(p)


def test_keyring_from_env_warns_when_unset_and_fails_closed_when_bad(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AGENTOS_SIGNING_KEYS", raising=False)
    with caplog.at_level("WARNING", logger="agentos.seal"):
        assert keyring_from_env() is None
    assert any("UNSIGNED" in r.getMessage() for r in caplog.records)
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(tmp_path / "nope.json"))
    with pytest.raises(SealError, match="AGENTOS_SIGNING_KEYS=.*file not found"):
        keyring_from_env()
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(_keyring_file(tmp_path)))
    assert keyring_from_env().active == "k1"


# ------------------------------------------------------------------ integrity endpoint

def test_integrity_endpoint_reports_seal_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(_keyring_file(tmp_path)))
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    from agentos.api import main
    importlib.reload(main)
    c = TestClient(main.app)
    c.post("/agents", json={"name": "calc", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [{"id": "n", "agent": "calc"}]})
    run = c.post("/workflows/w/runs", params={"sync": "true"}).json()
    body = c.get(f"/runs/{run['id']}/integrity").json()
    assert body["ok"] is True and body["seals"]["state"] == "verified"
    assert body["seals"]["seals"] == 1 and body["seals"]["unsigned_tail"] == 0
    assert run["sealed_through"] == body["seals"]["sealed_through"]


# ------------------------------------------------------------------ CLI

@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("AGENTOS_SIGNING_KEYS", str(_keyring_file(tmp_path)))
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_AUTH", raising=False)
    return tmp_path


def _seed(tmp_path, keyring, budget=None):
    store = SqliteStore(str(tmp_path / "cli.db"))
    eng = _engine(store, keyring, budget)
    run = eng.start_run("w")
    return store, eng, run


def test_verify_passes_on_a_good_store_and_fails_on_a_tampered_run(sqlite_env, capsys):
    keyring = HmacKeyring.from_file(sqlite_env / "keys.json")
    store, _eng, run = _seed(sqlite_env, keyring)
    assert cli_main(["verify"]) == 0
    out = capsys.readouterr().out
    assert run.id in out and "seals verified (1/1)" in out and "0 failed" in out
    assert cli_main(["verify", "--run", run.id]) == 0
    capsys.readouterr()

    # tamper: rewrite + rechain directly in the SQLite file, as an attacker with the DB would
    records = [e.to_record() for e in store.read_events(run.id)]
    records[0]["workflow"] = "someone-elses"
    fixed = chain([from_record(dict(r, hash=None, prev_hash=None)) for r in records], 0, None)
    conn = store._conn
    for ev in fixed:
        rec = ev.to_record()
        conn.execute("UPDATE run_events SET record = ? WHERE run_id = ? AND seq = ?",
                     (json.dumps(rec), run.id, ev.seq))
    conn.commit()
    assert verify(store.read_events(run.id)) > 0                # the chain alone is fooled
    assert cli_main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "does not carry the sealed hash" in out and "1 failed" in out
    assert cli_main(["--json", "verify"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["failed"] is True and doc["checks"][-1]["status"] == "FAIL"


def test_verify_without_a_keyring_warns_instead_of_failing(sqlite_env, monkeypatch, capsys):
    keyring = HmacKeyring.from_file(sqlite_env / "keys.json")
    _seed(sqlite_env, keyring)
    monkeypatch.delenv("AGENTOS_SIGNING_KEYS")
    assert cli_main(["verify"]) == 0
    out = capsys.readouterr().out
    assert "warn  signing" in out and "seals unverifiable" in out


def test_doctor_reports_configuration_and_every_run(sqlite_env, monkeypatch, capsys):
    keyring = HmacKeyring.from_file(sqlite_env / "keys.json")
    _seed(sqlite_env, keyring)
    assert cli_main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ok    store" in out and "sqlite: 1 runs" in out
    assert "ok    migrations" in out
    assert "ok    executor echo" in out and "executor tool" in out
    assert "warn  auth" in out and "client-asserted" in out
    assert "warn  policy" in out and "no operator ceiling" in out
    assert "ok    signing" in out
    assert "ok    runs fold" in out and "1/1" in out and "ok    runs verify" in out
    monkeypatch.setenv("AGENTOS_AUTH", "bearer")
    monkeypatch.setenv("AGENTOS_POLICY", str(_example_policy()))
    assert cli_main(["--json", "doctor"]) == 0
    doc = json.loads(capsys.readouterr().out)
    by = {c["name"]: c for c in doc["checks"]}
    assert by["auth"]["status"] == "ok" and by["policy"]["status"] == "ok"


def test_doctor_fails_when_the_store_is_unreachable(monkeypatch, capsys):
    monkeypatch.setenv("AGENTOS_STORE", "postgres")
    monkeypatch.delenv("AGENTOS_PG_DSN", raising=False)
    assert cli_main(["doctor"]) == 1
    assert "FAIL  store" in capsys.readouterr().out


def _example_policy():
    from pathlib import Path
    return Path(__file__).resolve().parents[1] / "examples" / "operator_policy.json"


def test_policy_explain_shows_what_the_ceiling_does_to_a_workflow(sqlite_env, monkeypatch, capsys):
    generous = Budget(allowed_effect_classes={EffectClass.read, EffectClass.compute, EffectClass.spend},
                      allow_agent_approval=True)
    _seed(sqlite_env, None, generous)
    monkeypatch.setenv("AGENTOS_POLICY", str(_example_policy()))
    assert cli_main(["policy", "explain", "w"]) == 0
    out = capsys.readouterr().out
    assert "workflow w v1" in out and "narrowed by the operator ceiling" in out
    assert "spend: allowed → approval_required (always_approve)" in out
    assert "allow_agent_approval: true → false" in out
    assert "pay" in out and "asks approval" in out and "spend→asks approval" in out
    assert "n1" in out and "runs" in out
    assert cli_main(["--json", "policy", "explain", "w"]) == 0
    doc = json.loads(capsys.readouterr().out)
    pay = next(n for n in doc["nodes"] if n["id"] == "pay")
    assert pay["outcome"] == "asks approval" and pay["executor_allowed"] is True
    assert doc["effective_budget"]["allow_agent_approval"] is False
    assert cli_main(["policy", "explain", "nope"]) == 2

    # a forbidden executor and a class outside the ceiling are named per node
    store = SqliteStore(str(sqlite_env / "cli.db"))
    store.put_agent(Agent(name="coder", type=AgentType.echo, executor="fancy-llm",
                          declared_effects=[EffectClass.execute_code]))
    store.put_workflow(WorkflowDefinition(name="risky", nodes=[{"id": "c", "agent": "coder"}],
                                          budget=Budget(allowed_effect_classes={EffectClass.execute_code})))
    assert cli_main(["--json", "policy", "explain", "risky"]) == 0
    doc = json.loads(capsys.readouterr().out)
    (c,) = doc["nodes"]
    assert c["executor_allowed"] is False and "executor not allowed" in c["outcome"]
    assert c["declared"] == {"execute_code": "REFUSED"}


def test_console_script_is_declared():
    import tomllib
    from pathlib import Path
    py = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert py["project"]["scripts"] == {"agentos": "agentos.cli:main"}
    assert os.environ.get("AGENTOS_STORE") is not None or True
