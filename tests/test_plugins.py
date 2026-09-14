"""Executor plugins (docs/DEVELOPMENT_STRUCTURE.md §2.2, §11 A3): entry-point discovery
in the composition root, executor routing by name, model substitution recorded in the
log, run inputs, and the endpoints a developer reaches for first."""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentos import plugins
from agentos.core.engine import Engine
from agentos.core.models import (
    Agent,
    AgentType,
    Cost,
    Effect,
    EffectClass,
    Provenance,
    RetryPolicy,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
)
from agentos.store.memory import MemoryStore


class AliasedExecutor:
    """Stand-in for a provider plugin: resolves an alias through a mutable table and can
    be told to fail once so a retry sees a different resolution."""

    name, version = "aliased", "t"

    def __init__(self, table: dict[str, str], fail_once: set[str] = frozenset()):
        self.table, self.fail_once, self.calls = table, set(fail_once), []

    def resolve(self, req: StepRequest) -> str:
        return self.table[req.agent.config["model"]]

    def execute(self, req: StepRequest, progress) -> StepResult:
        self.calls.append((req.step_id, req.attempt))
        if req.step_id in self.fail_once:
            self.fail_once.discard(req.step_id)
            self.table["chat.fast"] = "model-B"          # alias re-pointed mid-run
            raise RuntimeError("transient")
        return StepResult(
            output={"model": self.resolve(req), "inputs": req.inputs},
            effects=[Effect(effect_class=EffectClass.compute)], cost=Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=self.resolve(req), model_alias="chat.fast"))


def _wf(store, *, retry=1):
    store.put_agent(Agent(name="w", type=AgentType.llm, executor="aliased",
                          config={"model": "chat.fast"}))
    store.put_workflow(WorkflowDefinition(name="f", nodes=[
        {"id": "s1", "agent": "w"},
        {"id": "s2", "agent": "w", "depends_on": ["s1"],
         "retry": RetryPolicy(max_attempts=retry)}]))


# ------------------------------------------------------------------ A3 substitution

def test_alias_repointed_mid_run_is_recorded_before_the_step_runs():
    store = MemoryStore()
    _wf(store, retry=2)
    ex = AliasedExecutor({"chat.fast": "model-A"}, fail_once={"s2"})
    eng = Engine(store=store, blobs=store, executors={"aliased": ex}, lease=store)
    run = eng.start_run("f")
    assert run.status is RunStatus.completed
    assert [s.provenance.model_id for s in run.steps] == ["model-A", "model-B"]
    (sub,) = run.substitutions
    assert (sub.step_id, sub.agent, sub.from_model, sub.to_model) == ("s2", "w", "model-A", "model-B")
    assert "was 'model-A'" in sub.reason
    types = [(type(e).__name__, getattr(e, "attempt", None)) for e in store.read_events(run.id)]
    i_sub = types.index(("ExecutorSubstituted", None))
    assert types[i_sub + 1] == ("StepStarted", 2)                  # before the retry starts
    names = [t[0] for t in types]
    assert names.index("StepCompleted") < i_sub < names.index("RunCompleted")


def test_stable_resolution_records_no_substitution_and_echo_never_resolves():
    store = MemoryStore()
    _wf(store)
    ex = AliasedExecutor({"chat.fast": "model-A"})
    eng = Engine(store=store, blobs=store, executors={"aliased": ex}, lease=store)
    run = eng.start_run("f")
    assert run.status is RunStatus.completed and run.substitutions == []
    assert "ExecutorSubstituted" not in [type(e).__name__ for e in store.read_events(run.id)]


def test_resolver_that_raises_does_not_stop_the_engine(caplog):
    store = MemoryStore()
    _wf(store)
    ex = AliasedExecutor({})                                         # KeyError in resolve()
    ex.execute = lambda req, progress: StepResult(                   # type: ignore[method-assign]
        output={}, provenance=Provenance(executor="aliased", executor_version="t"))
    eng = Engine(store=store, blobs=store, executors={"aliased": ex}, lease=store)
    with caplog.at_level(logging.WARNING):
        run = eng.start_run("f")
    assert run.status is RunStatus.completed
    assert any("resolve() failed" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ routing by name

def test_missing_named_executor_fails_the_run_with_install_hint():
    store = MemoryStore()
    _wf(store)
    eng = Engine(store=store, blobs=store, executors={"echo": object()}, lease=store)
    run = eng.start_run("f")
    assert run.status is RunStatus.failed
    assert "needs executor 'aliased' but only [echo] are registered" in run.error
    assert "pip install agentos-provider" in run.error


# ------------------------------------------------------------------ discovery

def test_discover_executors_loads_good_skips_broken_and_dedupes(monkeypatch, caplog):
    good = AliasedExecutor({"chat.fast": "m"})

    def broken():
        raise ImportError("vendor sdk missing")

    eps = [SimpleNamespace(name="good", value="pkg:load", load=lambda: (lambda: good)),
           SimpleNamespace(name="broken", value="bad:load", load=lambda: broken),
           SimpleNamespace(name="dup", value="pkg:load", load=lambda: (lambda: good)),
           SimpleNamespace(name="notexec", value="x:y", load=lambda: (lambda: object()))]
    monkeypatch.setattr(plugins, "entry_points", lambda group: eps)
    with caplog.at_level(logging.WARNING):
        found = plugins.discover_executors()
    assert found == {"aliased": good}
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "'broken' (bad:load) failed to load and was skipped: vendor sdk missing" in msgs
    assert "registered twice" in msgs and "did not produce an Executor" in msgs


def test_installed_providers_are_discovered_through_their_entry_points():
    pytest.importorskip("agentos_provider_openai_compat")
    found = plugins.discover_executors()
    assert "openai-compat" in found
    by_name = {d["name"]: d for d in plugins.describe(found)}
    d = by_name["openai-compat"]
    assert d["describe"]["aliases"]["chat.fast"] and "reachable" in d["health"]
    if "anthropic" in found:                                 # second provider, same seam
        assert by_name["anthropic"]["describe"]["wire_format"] == "anthropic-messages"


def test_pricing_snapshot_is_stored_as_a_blob():
    store = MemoryStore()

    class Priced:
        name, version = "p", "1"
        def pricing_snapshot(self): return b'{"schema":"agentos.pricing/1","models":{}}'
        def execute(self, req, progress): ...

    stored = plugins.store_pricing_snapshots({"p": Priced(), "echo": object()}, store)
    import hashlib
    assert stored == {"p": hashlib.sha256(Priced().pricing_snapshot()).hexdigest()}
    from agentos.core.models import BlobRef
    assert store.get(BlobRef(sha256=stored["p"], size=0)) == Priced().pricing_snapshot()


def test_core_imports_no_provider_or_http_client():
    code = ("import sys, agentos.core.engine, agentos.core.ports, agentos.core.fold; "
            "bad=[m for m in sys.modules if m.startswith(('agentos_provider','httpx','agentos.plugins'))]; "
            "print(len(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"


# ------------------------------------------------------------------ API: inputs, endpoints

def _client(monkeypatch):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    from agentos.api import main
    importlib.reload(main)
    return TestClient(main.app), main


def test_run_inputs_reach_every_step_and_are_stored_by_hash(monkeypatch):
    c, _main = _client(monkeypatch)
    c.post("/agents", json={"name": "g", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [
        {"id": "a", "agent": "g"}, {"id": "b", "agent": "g", "depends_on": ["a"]}]})
    r = c.post("/workflows/w/runs?sync=true", json={"inputs": {"topic": "event logs"}})
    assert r.status_code == 201
    run = r.json()
    assert run["status"] == "completed" and run["inputs"] == {"topic": "event logs"}
    a, b = run["steps"]
    assert a["output"]["received"] == {"run": {"topic": "event logs"}}
    assert b["output"]["received"]["run"] == {"topic": "event logs"} and "a" in b["output"]["received"]
    blob = c.get(f"/blobs/{run['inputs_ref']['sha256']}")
    assert blob.status_code == 200 and blob.json() == {"topic": "event logs"}
    assert c.get("/blobs/" + "0" * 64).status_code == 404
    # A workflow with no inputs is unchanged: no reserved key, same idempotency keys.
    r2 = c.post("/workflows/w/runs?sync=true").json()
    assert r2["inputs"] == {} and r2["inputs_ref"] is None
    assert r2["steps"][0]["output"]["received"] == {}


def test_reserved_node_id_is_rejected_when_inputs_are_given(monkeypatch):
    c, _ = _client(monkeypatch)
    c.post("/agents", json={"name": "g", "type": "echo"})
    c.post("/workflows", json={"name": "w", "nodes": [{"id": "run", "agent": "g"}]})
    r = c.post("/workflows/w/runs", json={"inputs": {"x": 1}})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]


def test_executors_endpoint_lists_what_can_run(monkeypatch):
    c, _ = _client(monkeypatch)
    names = [e["name"] for e in c.get("/executors").json()]
    assert "echo" in names
