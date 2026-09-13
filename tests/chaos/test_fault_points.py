"""Layer 1 chaos: deterministic fault points, in-process. Each test is one row of the
fault-point table in ROADMAP.md. `Crash` derives from BaseException, so it passes
through every `except Exception` in the engine exactly like SIGKILL would."""
from __future__ import annotations

import pytest

from agentos.core import faults
from agentos.core.events import StepCompleted
from agentos.core.faults import Crash, CrashAt

from .conftest import assert_invariants, make_worker


def _start(store, executor, request_id="q") -> str:
    engine = make_worker(store, executor, holder="api")._engine
    run_id = engine.create_run("three", request_id=request_id)
    store.push(run_id)
    return run_id


def test_crash_before_effect_commit_reruns_step_but_records_one_completion(store, executor):
    """C1 / #1: output produced, step.completed NOT yet appended. The step must re-run
    on resume (the ledger never saw it) and the log must still end with exactly one
    completion per step. The executor sees s2 twice — which is why executors must be
    idempotent or, in the full StepResult model, record their effects first."""
    run_id = _start(store, executor)
    injector = CrashAt(faults.BEFORE_EFFECT_COMMIT, when=lambda c: c["step_id"] == "s2")
    w1 = make_worker(store, executor, holder="w1", faults=injector)

    with pytest.raises(Crash):
        w1.run_once()
    assert injector.fired
    mid = store.read_events(run_id)
    assert not any(isinstance(e, StepCompleted) and e.step_id == "s2" for e in mid)  # s2 not done
    assert [e.step_id for e in mid if isinstance(e, StepCompleted)] == ["s1"]
    assert executor.calls == {"s1": 1, "s2": 1}

    # w1 is "dead": its lease is still live (TTL 30 s) so a second worker must wait…
    w2 = make_worker(store, executor, holder="w2", lease_ttl=30.0)
    assert w2.run_once(timeout=0.2) is None or store.read_events(run_id) == mid
    # …until the lease expires. Simulate expiry by releasing w1's token as the OS would
    # eventually let happen (the real-process test below uses a short TTL instead).
    store.release(store.acquire(run_id, "w1", 0.01))  # w1's fence re-acquired then expired

    w2.recover()
    assert w2.run_once() == run_id
    run = w2._engine.get_run(run_id)
    assert run.status.value == "completed"
    assert executor.calls == {"s1": 1, "s2": 2, "s3": 1}
    assert [e.step_id for e in store.read_events(run_id) if isinstance(e, StepCompleted)] \
        == ["s1", "s2", "s3"]
    assert_invariants(store, run_id)


def test_crash_after_effect_commit_replays_without_reexecuting(store, executor):
    """C1 / #1, the other side: step.completed IS appended, then the process dies.
    Resume must skip s2 entirely — the executor is never called for it again."""
    run_id = _start(store, executor)
    injector = CrashAt(faults.AFTER_EFFECT_COMMIT, when=lambda c: c["step_id"] == "s2")
    with pytest.raises(Crash):
        make_worker(store, executor, holder="w1", faults=injector).run_once()
    assert executor.calls == {"s1": 1, "s2": 1}

    store.release(store.acquire(run_id, "w1", 0.01))
    w2 = make_worker(store, executor, holder="w2")
    w2.recover()
    assert w2.run_once() == run_id
    assert executor.calls == {"s1": 1, "s2": 1, "s3": 1}                # s2 NOT re-run
    assert w2._engine.get_run(run_id).status.value == "completed"
    assert_invariants(store, run_id)


def test_crash_after_run_commit_before_ack_is_a_noop_on_redelivery(store, executor):
    """C2 / #2: run.completed committed, queue ack lost. The redelivery must find the
    run terminal and write nothing."""
    run_id = _start(store, executor)
    store.visibility_seconds = 0.1                                       # fast redelivery
    injector = CrashAt(faults.AFTER_RUN_COMMIT_BEFORE_ACK)
    with pytest.raises(Crash):
        make_worker(store, executor, holder="w1", faults=injector).run_once()
    before = [e.to_record() for e in store.read_events(run_id)]
    assert before[-1]["event_type"] == "run.completed"

    import time
    time.sleep(0.15)                                                     # visibility lapses
    w2 = make_worker(store, executor, holder="w2")
    assert w2.run_once() == run_id                                       # redelivered
    assert [e.to_record() for e in store.read_events(run_id)] == before  # nothing appended
    assert executor.calls == {"s1": 1, "s2": 1, "s3": 1}
    assert store.pull(0.05) is None                                      # acked this time
    assert_invariants(store, run_id)


def test_two_workers_one_run_exactly_one_advancement_per_step(store, executor):
    """C6 / #6: a second worker cannot advance a run someone else holds, and a worker
    whose lease lapsed is fenced out of writing even though it is still alive."""
    run_id = _start(store, executor)
    w1 = make_worker(store, executor, holder="w1", lease_ttl=0.2)
    w2 = make_worker(store, executor, holder="w2", lease_ttl=30.0)

    # Freeze w1 right after it takes the lease (simulating a stall), let it expire,
    # let w2 take over and finish, then let w1 wake up and try to write.
    class StallThenContinue:
        def __init__(self):
            self.stalled = False

        def at(self, point, **ctx):
            if point == faults.AFTER_LOCK_ACQUIRE and not self.stalled:
                self.stalled = True
                import time
                time.sleep(0.35)                                        # > w1.lease_ttl
                w2.recover()
                assert w2.run_once() == run_id                          # w2 finishes the run

    w1._faults = StallThenContinue()
    w1._engine._faults = w1._faults
    w1.run_once()                                                        # w1 wakes: fenced/terminal, no write

    assert executor.calls == {"s1": 1, "s2": 1, "s3": 1}
    assert w2._engine.get_run(run_id).status.value == "completed"
    assert_invariants(store, run_id)


def test_definition_changed_between_crash_and_resume_is_refused(store, executor):
    """C3 / #3: never resume against a definition the run did not start with."""
    run_id = _start(store, executor)
    injector = CrashAt(faults.AFTER_EFFECT_COMMIT, when=lambda c: c["step_id"] == "s1")
    with pytest.raises(Crash):
        make_worker(store, executor, holder="w1", faults=injector).run_once()
    store.release(store.acquire(run_id, "w1", 0.01))

    wf = store.get_workflow("three")
    store.put_workflow(wf.model_copy(update={"version": 2, "nodes": wf.nodes[::-1]}))
    w2 = make_worker(store, executor, holder="w2")
    w2.recover()
    w2.run_once()
    run = w2._engine.get_run(run_id)
    assert run.status.value == "failed" and "pinned v1" in run.error
    assert executor.calls == {"s1": 1}                                   # nothing re-ran
    assert_invariants(store, run_id)
