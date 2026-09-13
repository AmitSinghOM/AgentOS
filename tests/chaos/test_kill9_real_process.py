"""The Phase 1 headline demo as a test: a REAL worker process is killed (exit 137, the
SIGKILL status, via os._exit so nothing is flushed or cleaned up) right after step 2 is
committed; a second real process picks the run up and finishes it without repeating
step 2. Everything crosses a process boundary through the SQLite file only."""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter

from agentos.core.events import StepCompleted, StepStarted
from agentos.core.fold import fold

from .conftest import assert_invariants, make_worker


def _worker(db_path, *, fault: str | None, holder: str, ttl: float = 0.5):
    env = os.environ | {
        "AGENTOS_STORE": "sqlite",
        "AGENTOS_SQLITE_PATH": str(db_path),
        "AGENTOS_LOG": "WARNING",
    }
    if fault:
        env["AGENTOS_FAULT"] = fault
    else:
        env.pop("AGENTOS_FAULT", None)
    return subprocess.run(
        [sys.executable, "-m", "agentos.worker", "--once", "--holder", holder,
         "--lease-ttl", str(ttl)],
        env=env, capture_output=True, text=True, timeout=60, check=False,  # we assert rc
    )


def test_kill_9_after_step_2_then_restart_finishes_without_repeating_step_2(store, executor,
                                                                              db_path):
    engine = make_worker(store, executor, holder="api")._engine
    run_id = engine.create_run("three", request_id="demo")
    store.push(run_id)
    store.close()                                   # hand the file to the subprocesses

    first = _worker(db_path, fault="after_effect_commit:s2", holder="worker-1")
    assert first.returncode == 137, (first.returncode, first.stderr[-800:])

    # Re-open read-only from the test to look at the wreckage.
    from agentos.store.sqlite import SqliteStore
    s = SqliteStore(db_path)
    mid = s.read_events(run_id)
    assert [e.step_id for e in mid if isinstance(e, StepCompleted)] == ["s1", "s2"]
    assert not any(isinstance(e, StepStarted) and e.step_id == "s3" for e in mid)
    assert fold(mid).status.value == "running"
    assert s.acquire(run_id, "probe", 0.01) is None, "dead worker's lease should still be live"
    s.close()

    import time
    time.sleep(0.6)                                 # worker-1's 0.5 s lease expires

    second = _worker(db_path, fault=None, holder="worker-2")
    assert second.returncode == 0, second.stderr[-800:]
    assert second.stdout.strip() == run_id          # --once prints the run it handled

    s = SqliteStore(db_path)
    events = s.read_events(run_id)
    run = fold(events)
    assert run.status.value == "completed"
    completed = [e.step_id for e in events if isinstance(e, StepCompleted)]
    assert completed == ["s1", "s2", "s3"]          # s2 exactly once
    attempts = Counter(e.step_id for e in events if isinstance(e, StepStarted))
    assert attempts == {"s1": 1, "s2": 1, "s3": 1}  # and never even *started* again
    # Second process wrote with a higher fence than the dead one.
    assert_invariants(s, run_id)
    s.close()
