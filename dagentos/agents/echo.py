"""Deterministic stand-in executor — proves the pipe without a provider.
Implements `dagentos.core.ports.Executor`. Reports zero cost, `compute` as its only
effect, and honest provenance. Phase 2 adds a `tool` executor (HTTP/subprocess) here;
model-vendor executors live in separate `agentos-provider-*` distributions."""
from __future__ import annotations

from dagentos.core.models import Cost, Effect, EffectClass, Provenance, StepRequest, StepResult
from dagentos.core.ports import ProgressFn

VERSION = "0.3.0"


class EchoExecutor:
    name = "echo"
    version = VERSION

    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        progress(0.0, "echo start")
        message = req.agent.config.get("message", "hello from agentos")
        output = {"agent": req.agent.name, "message": message, "received": req.inputs}
        return StepResult(
            output=output,
            effects=[Effect(effect_class=EffectClass.compute, description="echo")],
            cost=Cost(),
            provenance=Provenance(executor=self.name, executor_version=self.version),
        )
