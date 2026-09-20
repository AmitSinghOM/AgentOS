"""Worker shutdown (production pass 2, A1).

`docker stop`, a Kubernetes rollout and Ctrl-C all deliver SIGTERM/SIGINT to the worker
process, which is PID 1 in the image. Before this pass Python's default disposition killed
it mid-step: `_process`'s `finally: release(token)` never ran and the run sat leased-but-idle
until the lease TTL expired (30 s default). Now the first signal asks the loop to stop after
the delivery in flight, the lease is released on the way out, the exit code is 0; a second
signal exits immediately (the lease then expires by TTL, as before).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from dagentos.core.engine import Engine
from dagentos.core.models import (
    Agent,
    AgentType,
    Effect,
    EffectClass,
    Provenance,
    RunStatus,
    StepRequest,
    StepResult,
    WorkflowDefinition,
)
from dagentos.store.memory import MemoryStore
from dagentos.worker import Worker


class SlowEcho:
    """One step that takes long enough for a signal to land in the middle of it."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.started = threading.Event()

    def execute(self, req: StepRequest, progress) -> StepResult:
        self.started.set()
        time.sleep(self.seconds)
        return StepResult(output={"ok": True},
                          effects=[Effect(effect_class=EffectClass.compute, description="slow")],
                          provenance=Provenance(executor="slow-echo", executor_version="1"))


@pytest.fixture
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_sigterm_mid_step_finishes_the_delivery_releases_the_lease_and_returns(restore_signals):
    store = MemoryStore()
    store.put_agent(Agent(name="a", type=AgentType.echo))
    store.put_workflow(WorkflowDefinition(name="one", version=1, nodes=[{"id": "n1", "agent": "a"}]))
    slow = SlowEcho(0.4)
    eng = Engine(store=store, blobs=store, executors={"echo": slow}, lease=store)
    run_id = eng.create_run("one")
    store.push(run_id)
    worker = Worker(eng, store, lease=store, queue=store, holder="w")

    from dagentos.worker.__main__ import install_stop_signal
    stop = install_stop_signal()
    assert stop() is False

    def fire():
        assert slow.started.wait(5)              # the step is running...
        os.kill(os.getpid(), signal.SIGTERM)     # ...when the orchestrator says stop

    threading.Thread(target=fire, daemon=True).start()
    t0 = time.monotonic()
    worker.run_forever(stop=stop, sweep_interval=1000)   # returns; would loop forever before
    assert time.monotonic() - t0 < 5
    assert stop() is True
    assert worker.processed == 1
    assert eng.get_run(run_id).status == RunStatus.completed      # the step was not cut short
    assert store.acquire(run_id, "next-holder", 1.0) is not None  # lease released, not TTL-held


def test_a_second_signal_exits_immediately(restore_signals):
    from dagentos.worker.__main__ import install_stop_signal
    stop = install_stop_signal()
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.05)
    assert stop() is True
    with pytest.raises(SystemExit) as exc:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.5)                          # the handler runs between bytecodes
    assert exc.value.code == 128 + signal.SIGINT


def test_the_real_worker_process_exits_0_on_sigterm(tmp_path):
    env = os.environ | {
        "AGENTOS_STORE": "sqlite",
        "AGENTOS_SQLITE_PATH": str(tmp_path / "w.db"),
        "AGENTOS_LOG": "INFO",
        "AGENTOS_WORKER_METRICS_PORT": "0",
    }
    env.pop("AGENTOS_FAULT", None)
    proc = subprocess.Popen([sys.executable, "-m", "dagentos.worker", "--holder", "sig-test"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(2.0)                          # import + boot sweep; then idle in pull()
        assert proc.poll() is None, proc.stderr.read()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err             # was -15 (killed by the signal)
    assert "SIGTERM received" in err and "worker stopped" in err
