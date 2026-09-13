"""Deterministic fault points (ROADMAP.md → Chaos engineering plan, Layer 1).

The engine and worker call `faults.at("<point>")` at every boundary where a crash would
be interesting. Production binds `NoFaults`; the chaos suite binds an injector that raises
`Crash` (in-process tests) or calls `os._exit` (the real kill -9 test). Named points are
part of the test contract — add, never rename.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol


class Crash(BaseException):
    """Simulated process death. Derives from BaseException so `except Exception`
    boundaries in the engine cannot swallow it — exactly like SIGKILL."""


class FaultInjector(Protocol):
    def at(self, point: str, **ctx: object) -> None: ...


class NoFaults:
    def at(self, point: str, **ctx: object) -> None:
        return None


class CrashAt:
    """Raise `Crash` (or hard-exit) the Nth time `point` is reached with matching ctx."""

    def __init__(self, point: str, *, when: Callable[[dict], bool] | None = None,
                 nth: int = 1, hard_exit: bool = False) -> None:
        self.point, self.when, self.nth, self.hard_exit = point, when, nth, hard_exit
        self.hits = 0
        self.fired = False

    def at(self, point: str, **ctx: object) -> None:
        if point != self.point or (self.when and not self.when(ctx)):
            return
        self.hits += 1
        if self.hits == self.nth:
            self.fired = True
            if self.hard_exit:
                os._exit(137)  # SIGKILL's conventional exit status; no cleanup runs
            raise Crash(f"injected crash at {point} {ctx}")


def from_env(var: str = "AGENTOS_FAULT") -> FaultInjector:
    """Parse `AGENTOS_FAULT=<point>[:<step_id>]` into a hard-exit injector.
    Used by the real-process kill -9 test; unset → NoFaults."""
    spec = os.environ.get(var)
    if not spec:
        return NoFaults()
    point, _, step = spec.partition(":")
    when = (lambda ctx: ctx.get("step_id") == step) if step else None
    return CrashAt(point, when=when, hard_exit=True)


# Named points (documented here so tests and engine agree on spelling).
AFTER_LOCK_ACQUIRE = "after_lock_acquire"
BEFORE_EFFECT_COMMIT = "before_effect_commit"       # output produced, step.completed not yet appended
AFTER_EFFECT_COMMIT = "after_effect_commit"         # step.completed appended
AFTER_RUN_COMMIT_BEFORE_ACK = "after_run_commit_before_ack"
