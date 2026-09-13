"""Fold and upcast are pure; these tests pin their contract."""
from __future__ import annotations

import pytest

from agentos.core import upcast as upcast_mod
from agentos.core.events import (
    CURRENT_SCHEMA_VERSION,
    RunCompleted,
    RunFailed,
    RunStarted,
    StepCompleted,
    StepStarted,
    from_record,
)
from agentos.core.fold import FoldError, fold
from agentos.core.models import BlobRef, RunStatus

REF = BlobRef(sha256="a" * 64, size=2)


def _seq(events):
    return [e.model_copy(update={"seq": i}) for i, e in enumerate(events, start=1)]


def _happy():
    return _seq([
        RunStarted(run_id="r", workflow="wf", workflow_version=2, request_id="q"),
        StepStarted(run_id="r", step_id="a", attempt=1, agent="e", idempotency_key="k1"),
        StepCompleted(run_id="r", step_id="a", attempt=1, idempotency_key="k1", output_ref=REF),
        StepStarted(run_id="r", step_id="b", attempt=1, agent="e", idempotency_key="k2"),
        StepCompleted(run_id="r", step_id="b", attempt=1, idempotency_key="k2", output_ref=REF),
        RunCompleted(run_id="r"),
    ])


def test_fold_happy_path_is_deterministic():
    events = _happy()
    run1, run2 = fold(events), fold(events)
    assert run1 == run2
    # And the fold of a JSON round-trip equals the fold of the originals.
    assert fold(from_record(e.to_record()) for e in events) == run1
    assert run1.status is RunStatus.completed
    assert run1.workflow_version == 2 and run1.request_id == "q"
    assert [s.node_id for s in run1.steps] == ["a", "b"]
    assert run1.steps[0].output_ref == REF and run1.steps[0].output == {}
    assert run1.attempts == {"a": 1, "b": 1} and run1.last_seq == 6


def test_fold_partial_log_is_running_with_completed_prefix():
    run = fold(_happy()[:4])  # a done, b started
    assert run.status is RunStatus.running
    assert [s.node_id for s in run.steps] == ["a"]
    assert run.attempts["b"] == 1


def test_fold_failed_run():
    ev = _seq([_happy()[0], RunFailed(run_id="r", error="boom", step_id="a")])
    run = fold(ev)
    assert run.status is RunStatus.failed and run.error == "boom" and run.ended_at


@pytest.mark.parametrize("mutate,match", [
    (lambda ev: ev[1:], "must begin with run.started"),
    (lambda ev: ev[:2] + ev[3:], "seq gap"),
    (lambda ev: ev[:3] + [ev[2].model_copy(update={"seq": 4})] + ev[4:], "completed twice"),
    (lambda ev: [], "empty"),
])
def test_fold_rejects_invalid_logs(mutate, match):
    with pytest.raises(FoldError, match=match):
        fold(mutate(_happy()))


def test_records_round_trip_through_json_and_stay_typed():
    for ev in _happy():
        rec = ev.to_record()
        assert rec["event_type"] and rec["schema_version"] == CURRENT_SCHEMA_VERSION
        back = from_record(rec)
        assert type(back) is type(ev) and back == ev


def test_unknown_event_type_is_an_error_not_a_skip():
    rec = _happy()[0].to_record() | {"event_type": "run.teleported"}
    with pytest.raises(ValueError, match="unknown event type"):
        from_record(rec)


def test_upcast_refuses_newer_and_missing_versions():
    rec = _happy()[0].to_record()
    with pytest.raises(ValueError, match="newer than this build"):
        upcast_mod.upcast(rec | {"schema_version": CURRENT_SCHEMA_VERSION + 1})
    if CURRENT_SCHEMA_VERSION > 1:
        pytest.skip("only meaningful while no v0 upcasters exist")
    # Version 0 never existed; there is no upcaster for it and there must not be.
    with pytest.raises(ValueError, match="no upcaster"):
        upcast_mod.upcast(rec | {"schema_version": 0})


def test_upcaster_registry_applies_chain(monkeypatch):
    """Simulate a future v2 with a renamed field to prove the chain mechanism works."""
    monkeypatch.setattr(upcast_mod, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(upcast_mod, "_REGISTRY", {})

    @upcast_mod.upcaster("run.started", from_version=1)
    def _v1_to_v2(record):
        record["principal"] = record.get("principal")  # additive default
        return record

    rec = _happy()[0].to_record()
    out = upcast_mod.upcast(rec)
    assert out["schema_version"] == 2 and "principal" in out
    with pytest.raises(ValueError, match="duplicate upcaster"):
        upcast_mod.upcaster("run.started", from_version=1)(_v1_to_v2)
