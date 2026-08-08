"""Workflow engine.

Phase 0 executes synchronously in topological order with the echo agent.
Phase 1 makes execution durable and asynchronous; the public run() shape
stays the same.
"""
from __future__ import annotations

from datetime import UTC, datetime

from agentos.agents.echo import run_echo
from agentos.core.models import RunStatus, StepResult, WorkflowRun
from agentos.store.memory import store


def _topo_order(wf) -> list[str]:
    """Return node IDs in topological order and reject cycles."""
    indegree = {n.id: len(n.depends_on) for n in wf.nodes}
    ready = [nid for nid, d in indegree.items() if d == 0]
    order: list[str] = []
    while ready:
        nid = ready.pop()
        order.append(nid)
        for n in wf.nodes:
            if nid in n.depends_on:
                indegree[n.id] -= 1
                if indegree[n.id] == 0:
                    ready.append(n.id)
    if len(order) != len(wf.nodes):
        raise ValueError("cycle detected at execution time")
    return order


def run_workflow(wf_name: str) -> WorkflowRun:
    wf = store.get_workflow(wf_name)
    if wf is None:
        raise KeyError(f"unknown workflow {wf_name!r}")

    run = WorkflowRun(workflow=wf_name, status=RunStatus.running)
    store.put_run(run)
    by_id = {n.id: n for n in wf.nodes}
    outputs: dict[str, dict] = {}

    try:
        for node_id in _topo_order(wf):
            node = by_id[node_id]
            agent = store.get_agent(node.agent)
            if agent is None:
                raise KeyError(
                    f"node {node_id!r} references unknown agent {node.agent!r}"
                )
            upstream = {dep: outputs[dep] for dep in node.depends_on}
            # Phase 1 replaces this with AgentRouter.dispatch(...).
            output = run_echo(agent, upstream)
            outputs[node_id] = output
            run.steps.append(StepResult(node_id=node_id, output=output))
        run.status = RunStatus.completed
    except Exception as exc:  # noqa: BLE001 — top-level run boundary
        run.status = RunStatus.failed
        run.error = str(exc)
    finally:
        run.ended_at = datetime.now(UTC)
        store.put_run(run)
    return run
