"""docs/tutorial.md, executed: the first-hour walk through the diamond run, driven by the
literal example files, through the real API. Define, start, watch it suspend, decide, verify
the log, time-travel. If this passes, the tutorial's commands produce what the page says.
(`?sync=true` stands in for the worker so the test needs no second process; the tutorial
tells the reader to run the worker and poll instead.)"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TUTORIAL = ROOT / "docs" / "tutorial.md"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_SIGNING_KEYS", raising=False)
    from dagentos.api import main
    importlib.reload(main)
    return TestClient(main.app)


def _post_file(c, path, file):
    r = c.post(path, content=(EXAMPLES / file).read_text(),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    return r.json()


def test_tutorial_as_written(client):
    c = client
    # §2 define: two agents (one declares spend) and the diamond
    _post_file(c, "/agents", "calc_agent.json")
    payer = _post_file(c, "/agents", "payer_agent.json")
    assert payer["declared_effects"] == ["compute", "spend"]
    _post_file(c, "/workflows", "diamond_workflow.json")

    # §3 start: the run suspends BEFORE `pay`; its siblings still ran
    run = c.post("/workflows/diamond/runs", params={"sync": "true"},
                 headers={"Idempotency-Key": "tutorial-1"}).json()
    assert run["status"] == "suspended"
    done = {s["node_id"] for s in run["steps"]}
    assert done == {"a", "b"}, done                   # pay never started; d waits on it
    rid = run["id"]
    again = c.post("/workflows/diamond/runs", params={"sync": "true"},
                   headers={"Idempotency-Key": "tutorial-1"}).json()
    assert again["id"] == rid                         # same key → same run, not a second one

    # §4 the inbox names the gate
    inbox = c.get("/approvals").json()["data"]
    assert len(inbox) == 1
    gate = inbox[0]
    assert gate["run_id"] == rid and gate["step_id"] == "pay" and gate["effect_classes"] == ["spend"]
    aid = gate["approval_id"]

    # §5 decide: a non-human is refused (403 with the reason); a human is recorded
    refused = c.post(f"/runs/{rid}/approvals/{aid}/approve",
                     json={"principal": {"kind": "agent", "id": "bot"}, "reason": "auto"})
    assert refused.status_code == 403 and "human" in refused.json()["detail"]
    ok = c.post(f"/runs/{rid}/approvals/{aid}/approve", params={"sync": "true"},
                json={"principal": {"kind": "human", "id": "you"}, "reason": "within budget"})
    assert ok.status_code == 200
    final = ok.json()
    assert final["status"] == "completed"
    assert [s["node_id"] for s in final["steps"]] == ["a", "b", "pay", "d"]
    decided = final["approvals"][aid]
    assert decided["status"] == "granted"
    assert decided["decided_by"] == {"kind": "human", "id": "you", "attestation": None}
    assert decided["decision_reason"] == "within budget"

    # §6 the log says so: the gate is requested, sibling `b` still runs, THEN the run suspends;
    # the grant precedes pay's start
    events = c.get(f"/runs/{rid}/events", params={"after": 0}).json()["data"]
    types = [e["event_type"] for e in events]
    i_req, i_susp, i_grant = (types.index("approval.requested"), types.index("run.suspended"),
                              types.index("approval.granted"))
    i_b = next(i for i, e in enumerate(events)
               if e["event_type"] == "step.completed" and e.get("step_id") == "b")
    i_pay = next(i for i, e in enumerate(events)
                 if e["event_type"] == "step.started" and e.get("step_id") == "pay")
    assert i_req < i_b < i_susp < i_grant < i_pay
    assert events[i_grant]["principal"]["id"] == "you"
    assert len(events) == 17 and events[i_susp]["seq"] == 9      # the table in §6
    integrity = c.get(f"/runs/{rid}/integrity").json()
    assert integrity["ok"] is True and integrity["hashed"] == 17
    assert integrity["seals"]["state"] == "unsigned"             # no keys configured

    # §7 time travel: the fold at `run.suspended` is suspended with the gate pending; the live one is done
    at = c.get(f"/runs/{rid}", params={"at": events[i_susp]["seq"]}).json()
    assert at["status"] == "suspended" and at["approvals"][aid]["status"] == "pending"
    assert c.get(f"/runs/{rid}").json()["status"] == "completed"


def test_tutorial_names_only_files_and_routes_that_exist():
    """The doc's `@examples/<file>` references must be real files and its routes must be
    routes the app serves, so the page cannot rot silently."""
    text = TUTORIAL.read_text()
    for name in set(re.findall(r"@examples/([\w.-]+)", text)):
        assert (EXAMPLES / name).exists(), f"tutorial references missing examples/{name}"
    from dagentos.api import main
    served = [r.path.split("/") for r in main.app.routes]   # type: ignore[attr-defined]

    def matches(path: str) -> bool:
        parts = path.split("/")
        return any(len(parts) == len(s) and all(
            seg.startswith("{") or seg == want for seg, want in zip(s, parts, strict=True))
            for s in served)

    for path in set(re.findall(r"localhost:8000(/[\w/{}.$-]+)", text)):
        if path.startswith("/ui"):
            continue                                    # the SPA mount, not a route object
        assert matches(path), f"tutorial names a route the API does not serve: {path}"
    # every example file the tutorial defines is valid JSON the API accepts as a body
    for name in ("calc_agent.json", "payer_agent.json", "diamond_workflow.json"):
        json.loads((EXAMPLES / name).read_text())
    # the "what you just used" table names test files (and optionally functions) that exist
    for ref in set(re.findall(r"`(tests/[\w/]+\.py)(?:::(\w+))?`", text)):
        path, func = ref
        assert (ROOT / path).exists(), f"tutorial names missing {path}"
        if func:
            assert f"def {func}(" in (ROOT / path).read_text(), f"{path} has no {func}"
    # docs it links to exist
    for target in set(re.findall(r"\]\((?!http)([\w.-]+\.md)\)", text)):
        assert (TUTORIAL.parent / target).exists(), f"tutorial links missing docs/{target}"
