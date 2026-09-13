"""Workflow engine.

Phase 0 executes synchronously in topological order. Phase 1 makes execution durable
and asynchronous; the public shape stays the same.

The engine depends only on the ports in `agentos.core.ports`. Stores and executors are
injected; the engine never imports an adapter. `import-linter` enforces this in CI.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from agentos.core.models import RunStatus, StepResult, WorkflowDefinition, WorkflowRun
from agentos.core.ports import Executor, Store


class Engine:
    def __init__(self, store: Store, executors: Mapping[str, Executor]) -> None:
        """`executors` maps an AgentType value (e.g. "echo") to the adapter that runs it."""
        self._store = store
        self._executors = executors

    def run_workflow(self, wf_name: str) -> WorkflowRun:
        wf = self._store.get_workflow(wf_name)
        if wf is None:
            raise KeyError(f"unknown workflow {wf_name!r}")

        run = WorkflowRun(workflow=wf_name, status=RunStatus.running)
        self._store.put_run(run)
        by_id = {n.id: n for n in wf.nodes}
        outputs: dict[str, dict] = {}

        try:
            for node_id in wf.topological_order():
                node = by_id[node_id]
                agent = self._store.get_agent(node.agent)
                if agent is None:
                    raise KeyError(
                        f"node {node_id!r} references unknown agent {node.agent!r}"
                    )
                executor = self._executors.get(agent.type.value)
                if executor is None:
                    raise KeyError(
                        f"no executor registered for agent type {agent.type.value!r}"
                    )
                upstream = {dep: outputs[dep] for dep in node.depends_on}
                output = executor.execute(agent, upstream)
                outputs[node_id] = output
                run.steps.append(StepResult(node_id=node_id, output=output))
            run.status = RunStatus.completed
        except Exception as exc:  # noqa: BLE001 — top-level run boundary
            run.status = RunStatus.failed
            run.error = str(exc)
        finally:
            run.ended_at = datetime.now(UTC)
            self._store.put_run(run)
        return run


__all__ = ["Engine", "WorkflowDefinition"]
