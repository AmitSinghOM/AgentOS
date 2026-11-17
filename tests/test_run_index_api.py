"""Audit E-07 through the API, the worker sweep and `agentos doctor`: the index chooses which
runs to fold; the fold still decides what the caller sees.

- `GET /runs?status=&workflow=&cursor=` filters on the index, pages on an opaque cursor and
  returns every run exactly once while runs change status between pages; `?limit` alone
  behaves as before; `counts` is per indexed status.
- `GET /approvals` returns exactly what the old fold-every-run implementation returned, but
  folds only the suspended runs.
- The worker sweep enqueues the same runs as before and folds only the candidates.
- A row that disagrees with the fold is never served under the wrong filter, and
  `agentos doctor` counts it.
"""
from __future__ import annotations

import dataclasses
import importlib
import json

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from dagentos.core.fold import fold
from dagentos.core.models import RunStatus
from dagentos.store.sqlite import SqliteStore
from dagentos.worker import Worker
from tests.test_run_index import seed


def _client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR", "AGENTOS_TRIGGERS"):
        monkeypatch.delenv(k, raising=False)
    from dagentos.api import main
    importlib.reload(main)
    return TestClient(main.app), main


def _seeded(monkeypatch):
    c, main = _client(monkeypatch)
    eng = seed(main.store)                         # 7 runs, every status the engine reaches
    return c, main, eng


def _status_of(main) -> dict[str, str]:
    return {rid: fold(main.store.read_events(rid)).status.value for rid in main.store.list_run_ids()}


def _counting_folds(monkeypatch, main) -> list[str]:
    folded: list[str] = []
    real = main.store.read_events

    def counting(run_id, after_seq=0, **kw):
        folded.append(run_id)
        return real(run_id, after_seq, **kw)

    monkeypatch.setattr(main.store, "read_events", counting)
    return folded


# --------------------------------------------------------------------------- GET /runs

def test_runs_filter_by_status_and_workflow_on_the_server(monkeypatch):
    c, main, _ = _seeded(monkeypatch)
    status = _status_of(main)
    newest_first = list(reversed(main.store.list_run_ids()))
    for s in ("suspended", "completed", "failed", "running", "paused", "cancelled"):
        got = [r["id"] for r in c.get("/runs", params={"status": s}).json()["data"]]
        assert got == [rid for rid in newest_first if status[rid] == s], s
        assert got, f"seed reaches {s}"
    gated = [r["id"] for r in c.get("/runs", params={"workflow": "gated"}).json()["data"]]
    assert len(gated) == 2 and all(main.engine.get_run(r).workflow == "gated" for r in gated)
    both = c.get("/runs", params={"workflow": "gated", "status": "suspended"}).json()["data"]
    assert [r["workflow"] for r in both] == ["gated"] and both[0]["status"] == "suspended"
    assert c.get("/runs", params={"status": "bogus"}).status_code == 422


def test_runs_counts_are_per_indexed_status(monkeypatch):
    c, _main, _ = _seeded(monkeypatch)
    body = c.get("/runs", params={"limit": 1}).json()
    assert body["counts"] == {"completed": 2, "suspended": 1, "failed": 1, "cancelled": 1,
                              "paused": 1, "running": 1}
    assert c.get("/runs", params={"workflow": "gated"}).json()["counts"] == {
        "completed": 1, "suspended": 1}


def test_runs_cursor_walks_every_run_exactly_once_while_one_changes_status(monkeypatch):
    c, main, eng = _seeded(monkeypatch)
    everyone = list(reversed(main.store.list_run_ids()))
    page = c.get("/runs", params={"limit": 2}).json()
    seen = [r["id"] for r in page["data"]]
    unseen = next(rid for rid in everyone[2:] if eng.get_run(rid).status is RunStatus.suspended)
    eng.request_cancel(unseen)                                     # changes status mid-walk
    while page["next_cursor"] is not None:
        page = c.get("/runs", params={"limit": 2, "cursor": page["next_cursor"]}).json()
        seen += [r["id"] for r in page["data"]]
    assert seen == everyone
    assert c.get("/runs", params={"cursor": "garbage"}).status_code == 400


def test_runs_with_only_limit_behaves_as_before(monkeypatch):
    c, main, _ = _seeded(monkeypatch)
    body = c.get("/runs", params={"limit": 3}).json()
    assert [r["id"] for r in body["data"]] == list(reversed(main.store.list_run_ids()))[:3]
    assert body["next_cursor"] is not None
    assert c.get("/runs", params={"limit": 500}).json()["next_cursor"] is None


def test_runs_never_serve_a_row_under_a_filter_its_fold_disagrees_with(monkeypatch):
    """FAIL_MODES: the index disagrees with the fold -> the API still serves the fold."""
    c, main, _ = _seeded(monkeypatch)
    done = next(rid for rid, s in _status_of(main).items() if s == "completed")
    main.store._index[done] = dataclasses.replace(main.store._index[done],
                                                  status=RunStatus.suspended)   # drifted row
    got = c.get("/runs", params={"status": "suspended"}).json()["data"]
    assert done not in [r["id"] for r in got]
    assert all(r["status"] == "suspended" for r in got)
    assert done not in [a["run_id"] for a in c.get("/approvals").json()["data"]]


# ---------------------------------------------------------------------- GET /approvals

def _old_inbox(main) -> list[dict]:
    """The implementation this PR replaced, verbatim: fold every run in the store."""
    out = []
    for run_id in main.store.list_run_ids():
        run = main.engine.get_run(run_id, hydrate=False)
        if run is None or run.status.value != "suspended":
            continue
        out += [{"run_id": run_id, "workflow": run.workflow, **a.model_dump()}
                for a in run.approvals.values() if a.status.value == "pending"]
    return out


def test_approvals_inbox_is_unchanged_and_folds_only_suspended_runs(monkeypatch):
    c, main, eng = _seeded(monkeypatch)
    second = eng.create_run("gated")
    eng.advance(second)                                            # two suspended runs now
    expected = TypeAdapter(list).dump_python(_old_inbox(main), mode="json")
    suspended = {rid for rid, s in _status_of(main).items() if s == "suspended"}
    folded = _counting_folds(monkeypatch, main)
    got = c.get("/approvals").json()["data"]
    assert got == expected and len(got) == 4                       # 2 pending in each run
    assert set(folded) == suspended and len(suspended) == 2


# --------------------------------------------------------------------------- the sweep

def test_sweep_enqueues_the_same_runs_as_before_and_folds_only_candidates(monkeypatch):
    _c, main, eng = _seeded(monkeypatch)
    old = [rid for rid in main.store.list_run_ids() if eng.needs_worker(rid)]
    assert old, "seed leaves a running run"
    candidates = {rid for rid, s in _status_of(main).items() if s in ("running", "suspended")}
    worker = Worker(eng, main.store, lease=main.store, queue=main.store, holder="w")
    folded = _counting_folds(monkeypatch, main)
    assert worker.recover() == old
    assert set(folded) == candidates


# ----------------------------------------------------------------------------- doctor

def test_doctor_counts_rows_that_disagree_with_the_fold(monkeypatch, tmp_path, capsys):
    from dagentos.cli import main as cli_main
    path = tmp_path / "doc.db"
    monkeypatch.setenv("AGENTOS_STORE", "sqlite")
    monkeypatch.setenv("AGENTOS_SQLITE_PATH", str(path))
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_AUTH"):
        monkeypatch.delenv(k, raising=False)
    store = SqliteStore(path)
    seed(store)
    assert cli_main(["--json", "doctor"]) == 0
    by = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert by["runs index"]["status"] == "ok" and by["runs index"]["detail"].startswith("7/7")

    done = next(rid for rid in store.list_run_ids()
                if fold(store.read_events(rid)).status is RunStatus.completed)
    store._conn.execute("UPDATE runs SET status = 'suspended' WHERE run_id = ?", (done,))
    store.close()
    assert cli_main(["--json", "doctor"]) == 1
    by = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert by["runs index"]["status"] == "FAIL"
    assert by["runs index"]["detail"].startswith("6/7") and done[:8] in by["runs index"]["detail"]
    assert by["runs fold"]["status"] == "ok"                       # the log itself is fine
