"""Layer 2, scenario 4: `partition_during_approval_grant` (chaos plan Layer 2 row "during
approval wait"; C7, DESIGN §4.4; audit E-12).

The real API (`dagentos.api.main`, reloaded over a Postgres store that goes THROUGH Toxiproxy)
serves `POST /runs/{id}/approvals/{aid}/approve` for a run a direct-link worker suspended on a
`spend` gate. The link is cut inside the approve path at one of two points, placed
deterministically by wrapping the store's `append_events` for one call:

  before-commit   the proxy is disabled just before the grant's append → the write dies on
                  the wire → the client gets 500; nothing is in the log.
  after-commit    the grant is appended, THEN the proxy is disabled → the API's follow-up
                  read dies → the client gets 500 although the grant is in the log.

Either way the client cannot tell which happened, so it retries after the link heals. What
must be true (the idempotency contract of an approval decision):
  - before-commit: the retry is accepted (202) and the log holds exactly ONE
    `approval.granted`;
  - after-commit: the retry is refused with 409 ("is granted") and the log STILL holds
    exactly one `approval.granted` — the first, partitioned call was the grant;
  - never two grants; the gated step runs exactly once, after the grant (its `step.started`
    seq is above the grant's), carrying that approval id;
  - the run was never re-enqueued by the dead request in the after-commit case, so the
    worker's recovery sweep (not the API) is what resumes it — the sweep is the backstop C7
    relies on.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from dagentos.core.engine import Engine
from dagentos.core.events import ApprovalGranted, ApprovalRequested, StepStarted
from dagentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Provenance,
    StepResult,
    WorkflowDefinition,
)
from dagentos.store.postgres import PostgresStore
from dagentos.worker import Worker

from ..conftest import assert_invariants
from .conftest import LISTEN, PG_DSN, UPSTREAM, proxied_dsn, wait_until

pytestmark = pytest.mark.network

HUMAN = {"kind": "human", "id": "amit"}
GATED = WorkflowDefinition(name="gated", version=1, nodes=[
    {"id": "n1", "agent": "calc"},
    {"id": "pay", "agent": "payer", "depends_on": ["n1"]},
])


class RecordingExecutor:
    name, version = "rec", "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []       # (step_id, approval_id)

    def execute(self, req, progress):
        self.calls.append((req.step_id, req.approval_id))
        return StepResult(output={"step": req.step_id},
                          effects=[Effect(effect_class=c) for c in sorted(req.declared_effects)],
                          cost=Cost(amount="0.10"),
                          provenance=Provenance(executor="rec", executor_version="test"))


@pytest.fixture
def api_over_proxy(toxiproxy, pg_schema, monkeypatch):
    """The API module built over the PROXIED DSN in the test's schema (same env knobs as
    production: AGENTOS_STORE/PG_DSN/PG_SCHEMA), plus a client that turns an unhandled server
    error into a 500 response instead of re-raising it in the test — the client's view."""
    toxiproxy.proxy("pg", LISTEN, UPSTREAM)
    monkeypatch.setenv("AGENTOS_STORE", "postgres")
    monkeypatch.setenv("AGENTOS_PG_DSN", proxied_dsn(PG_DSN))
    monkeypatch.setenv("AGENTOS_PG_SCHEMA", pg_schema)
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR"):
        monkeypatch.delenv(k, raising=False)
    from dagentos.api import main
    importlib.reload(main)
    try:
        yield main, TestClient(main.app, raise_server_exceptions=False)
    finally:
        toxiproxy.enable("pg")
        main.store.close()


def _cut_once(toxiproxy, store: PostgresStore, *, when: str) -> dict:
    """Wrap `store.append_events` so its NEXT call partitions the link `when` ("before" or
    "after" the real append), then restores itself. Returns a dict with `fired`."""
    original = store.append_events
    state = {"fired": False}

    def cutting(*args, **kwargs):
        store.append_events = original                        # one shot
        state["fired"] = True
        if when == "before":
            toxiproxy.disable("pg")
            return original(*args, **kwargs)
        out = original(*args, **kwargs)
        toxiproxy.disable("pg")
        return out

    store.append_events = cutting                             # instance attribute shadows the method
    return state


@pytest.mark.parametrize("when,retry_status", [("before", 202), ("after", 409)],
                         ids=["cut-before-grant-commit", "cut-after-grant-commit"])
def test_a_grant_cut_by_the_network_is_recorded_at_most_once_and_the_retry_is_idempotent(
        toxiproxy, pg_direct, api_over_proxy, when, retry_status):
    main, c = api_over_proxy
    direct = pg_direct
    direct.put_agent(Agent(name="calc", type=AgentType.echo))
    direct.put_agent(Agent(name="payer", type=AgentType.echo,
                           declared_effects=[EffectClass.compute, EffectClass.spend]))
    direct.put_workflow(GATED)
    executor = RecordingExecutor()
    worker_engine = Engine(store=direct, blobs=direct, executors={"echo": executor})
    worker = Worker(worker_engine, direct, lease=direct, queue=direct, holder="W")

    # Queue through the API (over the proxy), run on the direct worker until the gate.
    r = c.post("/workflows/gated/runs", headers={"Idempotency-Key": f"grant-{when}"})
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    assert worker.run_once(timeout=5.0) == run_id
    run = worker_engine.get_run(run_id)
    assert run.status.value == "suspended" and [s for s, _ in executor.calls] == ["n1"]
    (approval,) = [a for a in run.approvals.values() if a.status.value == "pending"]
    url = f"/runs/{run_id}/approvals/{approval.approval_id}/approve"

    # The partitioned call: the client sees an error either way.
    cut = _cut_once(toxiproxy, main.store, when=when)
    first = c.post(url, json={"principal": HUMAN, "reason": "ok"})
    assert cut["fired"], "the approve path never reached append_events"
    assert first.status_code == 500, (first.status_code, first.text[:300])
    grants_now = [e for e in direct.read_events(run_id) if isinstance(e, ApprovalGranted)]
    assert len(grants_now) == (0 if when == "before" else 1), grants_now

    # Heal, let the API's pool reconnect, retry exactly as a client would.
    toxiproxy.enable("pg")
    wait_until(lambda: main.store.ping(5.0) is None, timeout=60, what="the API store to reconnect")
    second = c.post(url, json={"principal": HUMAN, "reason": "ok again"})
    assert second.status_code == retry_status, (second.status_code, second.text[:300])
    if when == "after":
        assert "is granted" in second.json()["detail"]

    # The worker finishes it. In the after-commit case the API never re-enqueued the run (the
    # request died first), so the recovery sweep is what finds it.
    assert run_id in worker.recover()
    assert worker.run_once(timeout=5.0) == run_id
    final = worker_engine.get_run(run_id)
    assert final.status.value == "completed", final.error

    events = direct.read_events(run_id)
    grants = [e for e in events if isinstance(e, ApprovalGranted)]
    assert len(grants) == 1, [type(e).event_type for e in events]       # never two grants
    assert grants[0].approval_id == approval.approval_id
    assert grants[0].reason == ("ok again" if when == "before" else "ok")   # which call won
    requested = [e for e in events if isinstance(e, ApprovalRequested)]
    assert len(requested) == 1                                            # never re-asked
    pay_started = [e for e in events if isinstance(e, StepStarted) and e.step_id == "pay"]
    assert len(pay_started) == 1 and pay_started[0].seq > grants[0].seq  # ran once, after
    assert executor.calls == [("n1", None), ("pay", approval.approval_id)]
    assert_invariants(direct, run_id)
