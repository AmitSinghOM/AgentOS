"""docs/tutorial.md, executed: the first-hour walk through the diamond run, driven by the
literal example files, through the real API. Define, start, watch it suspend, decide, verify
the log, time-travel. If this passes, the tutorial's commands produce what the page says --
including the numbers it prints (status codes, the 17-row seq table, the `?at=` states), so
the page cannot go stale while the test stays green. The engine is advanced in-process where
the tutorial tells the reader to run the worker; every HTTP call is the one the page shows."""
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

HUMAN = {"kind": "human", "id": "you"}

# §6 of the tutorial, verbatim: (seq, event_type, step_id). Any change to the engine's event
# order or count changes this table, and the doc must change with it.
TUTORIAL_LOG = [
    (1, "run.started", None),
    (2, "step.started", "a"), (3, "step.progress", "a"), (4, "step.completed", "a"),
    (5, "approval.requested", "pay"),
    (6, "step.started", "b"), (7, "step.progress", "b"), (8, "step.completed", "b"),
    (9, "run.suspended", None),
    (10, "approval.granted", "pay"),
    (11, "step.started", "pay"), (12, "step.progress", "pay"), (13, "step.completed", "pay"),
    (14, "step.started", "d"), (15, "step.progress", "d"), (16, "step.completed", "d"),
    (17, "run.completed", None),
]


@pytest.fixture
def api(monkeypatch):
    """The app plus its module, so a test can stand in for the worker (`engine.advance`)."""
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_SIGNING_KEYS", raising=False)
    from dagentos.api import main
    importlib.reload(main)
    return TestClient(main.app), main


def _post_file(c, path, file):
    r = c.post(path, content=(EXAMPLES / file).read_text(),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text                      # §2: "each answers 201"
    return r.json()


@pytest.fixture
def defined(api):
    """§2 define: two agents (one declares spend) and the diamond."""
    c, main = api
    _post_file(c, "/agents", "calc_agent.json")
    payer = _post_file(c, "/agents", "payer_agent.json")
    assert payer["declared_effects"] == ["compute", "spend"]
    _post_file(c, "/workflows", "diamond_workflow.json")
    return c, main


@pytest.fixture
def suspended(defined):
    """§3 start: 202 and a run id; the worker (here: the engine, in-process) runs what it can
    and the run suspends BEFORE `pay`."""
    c, main = defined
    r = c.post("/workflows/diamond/runs", headers={"Idempotency-Key": "tutorial-1"})
    assert r.status_code == 202, r.text                      # §3: "202 and a run id"
    rid = r.json()["id"]
    main.engine.advance_until_terminal(rid)                  # the worker's job
    return c, main, rid


@pytest.fixture
def completed(suspended):
    """§5 decide as a human; the worker dispatches `pay` then `d`."""
    c, main, rid = suspended
    aid = c.get("/approvals").json()["data"][0]["approval_id"]
    ok = c.post(f"/runs/{rid}/approvals/{aid}/approve",
                json={"principal": HUMAN, "reason": "within budget"})
    assert ok.status_code == 202, ok.text                    # §5: "202. The worker dispatches"
    main.engine.advance_until_terminal(rid)
    return c, rid, aid


def test_define_requires_the_content_type_header(api):
    """§2: `curl -d` without the header sends form-encoding and the API answers 422."""
    c, _ = api
    r = c.post("/agents", content=(EXAMPLES / "calc_agent.json").read_text(),
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422, r.text


def test_start_suspends_before_pay_and_the_key_dedups(suspended):
    c, _, rid = suspended
    run = c.get(f"/runs/{rid}").json()
    assert run["status"] == "suspended"
    done = {s["node_id"] for s in run["steps"]}
    assert done == {"a", "b"}, done                          # pay never started; d waits on it
    again = c.post("/workflows/diamond/runs", headers={"Idempotency-Key": "tutorial-1"})
    assert again.status_code == 202 and again.json()["id"] == rid   # same key -> same run


def test_inbox_names_the_gate(suspended):
    c, _, rid = suspended
    inbox = c.get("/approvals").json()["data"]
    assert len(inbox) == 1
    gate = inbox[0]
    assert gate["run_id"] == rid and gate["step_id"] == "pay" and gate["effect_classes"] == ["spend"]


def test_non_human_is_refused_with_403_and_nothing_recorded(suspended):
    c, _, rid = suspended
    aid = c.get("/approvals").json()["data"][0]["approval_id"]
    refused = c.post(f"/runs/{rid}/approvals/{aid}/approve",
                     json={"principal": {"kind": "agent", "id": "bot"}, "reason": "auto"})
    assert refused.status_code == 403 and "human" in refused.json()["detail"]
    assert c.get(f"/runs/{rid}").json()["approvals"][aid]["status"] == "pending"
    types = [e["event_type"] for e in c.get(f"/runs/{rid}/events", params={"after": 0}).json()["data"]]
    assert "approval.granted" not in types and "approval.rejected" not in types


def test_human_decision_completes_the_run_and_is_recorded(completed):
    c, rid, aid = completed
    final = c.get(f"/runs/{rid}").json()
    assert final["status"] == "completed"
    assert [s["node_id"] for s in final["steps"]] == ["a", "b", "pay", "d"]
    decided = final["approvals"][aid]
    assert decided["status"] == "granted"
    assert decided["decided_by"] == {**HUMAN, "attestation": None}
    assert decided["decision_reason"] == "within budget"


def test_the_log_matches_the_tutorials_table(completed):
    """§6: seventeen events, in this order, seq numbers matching -- pinned row by row."""
    c, rid, _ = completed
    events = c.get(f"/runs/{rid}/events", params={"after": 0}).json()["data"]
    assert [(e["seq"], e["event_type"], e.get("step_id")) for e in events] == TUTORIAL_LOG
    assert events[9]["principal"]["id"] == "you"             # seq 10 carries principal + reason
    assert events[9]["reason"] == "within budget"
    integrity = c.get(f"/runs/{rid}/integrity").json()
    assert integrity["ok"] is True and integrity["hashed"] == 17
    assert integrity["seals"]["state"] == "unsigned"         # no keys configured


def test_time_travel_states_named_in_the_tutorial(completed):
    """§7: `?at=9` suspended with the gate pending; `at=5` and `at=10` still running; live done."""
    c, rid, aid = completed
    at9 = c.get(f"/runs/{rid}", params={"at": 9}).json()
    assert at9["status"] == "suspended" and at9["approvals"][aid]["status"] == "pending"
    assert {s["node_id"] for s in at9["steps"]} == {"a", "b"}
    at5 = c.get(f"/runs/{rid}", params={"at": 5}).json()
    assert at5["status"] == "running" and {s["node_id"] for s in at5["steps"]} == {"a"}
    at10 = c.get(f"/runs/{rid}", params={"at": 10}).json()
    assert at10["status"] == "running" and at10["approvals"][aid]["status"] == "granted"
    assert {s["node_id"] for s in at10["steps"]} == {"a", "b"}   # pay has not started
    assert c.get(f"/runs/{rid}").json()["status"] == "completed"


def test_tutorial_prose_quotes_the_numbers_the_test_pins():
    """The page's literal numbers are the ones this file asserts, so neither can drift alone."""
    text = TUTORIAL.read_text()
    for needle in ("`202`", "`422`", "`403`", "`201`", "Seventeen events",
                   "`\"hashed\": 17`", "?at=9", "`at=5`", "`at=10`"):
        assert needle in text, f"tutorial no longer says {needle}"
    for seq, event, step in TUTORIAL_LOG:
        if event.startswith("step."):
            continue                                         # grouped as 2–4 etc. in the table
        assert re.search(rf"^\| {seq} \| `{re.escape(event)}`", text, re.M), \
            f"tutorial table lacks row seq {seq} {event}"


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
