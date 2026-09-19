"""`agentos` — the operator's command line (Phase 8 #3 / #4).

    agentos verify [--run ID | --all]      hash chain + seals for one run or every run
    agentos doctor                         store, migrations, executors, every run folds and
                                           verifies, auth / policy / signing configured
    agentos policy explain <workflow>      the workflow's budget, the effective budget under
                                           the operator ceiling, and per node what runs freely,
                                           asks, or is refused

All three read the same environment as the API and the worker (`dagentos.store.factory`,
`AGENTOS_POLICY`, `AGENTOS_SIGNING_KEYS`, `AGENTOS_AUTH`). Exit status is 0 only when nothing
failed; warnings (unconfigured but not wrong) do not fail. Output is plain text a human reads
in a terminal; `--json` gives the same facts as one JSON document for scripts.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field

from dagentos.core.fold import FoldError, fold
from dagentos.core.integrity import IntegrityError, verify
from dagentos.core.models import Budget, EffectClass, WorkflowDefinition
from dagentos.core.policy import OperatorPolicy, apply_ceiling, executor_allowed, policy_sha256
from dagentos.core.seal import HmacKeyring, verify_seals
from dagentos.store import migrations
from dagentos.store.factory import store_from_env

OK, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = [f"{c.status:<5} {c.name:<{width}}  {c.detail}".rstrip() for c in self.checks]
        n_fail = sum(c.status == FAIL for c in self.checks)
        n_warn = sum(c.status == WARN for c in self.checks)
        lines.append(f"{len(self.checks)} checks: {n_fail} failed, {n_warn} warnings")
        return "\n".join(lines)


# ------------------------------------------------------------------ shared

def _keyring(report: Report | None = None) -> HmacKeyring | None:
    path = os.environ.get("AGENTOS_SIGNING_KEYS")
    if not path:
        if report is not None:
            report.add("signing", WARN, "AGENTOS_SIGNING_KEYS unset: chains are hash-linked but "
                                        "unsigned; seals cannot be judged")
        return None
    keyring = HmacKeyring.from_file(path)
    if report is not None:
        report.add("signing", OK, f"keyring active={keyring.active} keys={keyring.key_ids}")
    return keyring


def _policy(report: Report | None = None) -> OperatorPolicy | None:
    from dagentos.core.policy import load_policy

    path = os.environ.get("AGENTOS_POLICY")
    if not path:
        if report is not None:
            report.add("policy", WARN, "AGENTOS_POLICY unset: no operator ceiling")
        return None
    policy = load_policy(path)
    if report is not None:
        report.add("policy", OK, f"sha256 {policy_sha256(policy)[:12]} from {path}")
    return policy


def _verify_run(store, run_id: str, keyring: HmacKeyring | None) -> Check:
    events = store.read_events(run_id)
    if not events:
        return Check(run_id, FAIL, "no events")
    try:
        hashed = verify(events)
    except IntegrityError as exc:
        return Check(run_id, FAIL, f"chain: {exc}")
    seals = verify_seals(events, keyring)
    if seals.state == "INVALID":
        return Check(run_id, FAIL, "; ".join(seals.problems))
    tail = f", unsigned tail {seals.unsigned_tail}" if seals.seals and seals.unsigned_tail else ""
    status = OK if seals.state == "verified" else WARN
    return Check(run_id, status, f"{len(events)} events, {hashed} hashed, seals {seals.state} "
                                 f"({seals.valid}/{seals.seals}{tail})")


# ------------------------------------------------------------------ verify

def cmd_verify(args: argparse.Namespace) -> int:
    store = store_from_env()
    report = Report()
    keyring = _keyring(report)
    run_ids = [args.run] if args.run else store.list_run_ids()
    if not run_ids:
        report.add("runs", WARN, "no runs in the store")
    for run_id in run_ids:
        report.checks.append(_verify_run(store, run_id, keyring))
    return _emit(report, args)


# ------------------------------------------------------------------ doctor

def cmd_doctor(args: argparse.Namespace) -> int:
    """Each check appends one row; a doctor reports, it never crashes — so the broad catches
    here are the point, not an oversight. Only an unreachable store stops the sequence."""
    report = Report()
    try:
        store = store_from_env()
        run_ids = store.list_run_ids()
        report.add("store", OK, f"{os.environ.get('AGENTOS_STORE', 'sqlite')}: {len(run_ids)} runs")
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        report.add("store", FAIL, str(exc))
        return _emit(report, args)
    _check_migrations(store, report)
    _check_executors(report)
    _check_configuration(report)
    keyring = _configured_keyring(report)
    _check_runs(store, run_ids, keyring, report)
    return _emit(report, args)


def _check_migrations(store, report: Report) -> None:
    latest = max(v for v, _, _ in migrations.MIGRATIONS)
    version_fn = getattr(store, "schema_version", None)
    if version_fn is None:
        report.add("migrations", OK, "not applicable (memory store)")
        return
    have = version_fn()
    report.add("migrations", OK if have == latest else FAIL,
               f"schema {have}, latest {latest}" + ("" if have == latest else
                                                    " — restart the API or worker to migrate"))


def _check_executors(report: Report) -> None:
    from dagentos.agents.echo import EchoExecutor
    from dagentos.agents.tool import ToolExecutor
    from dagentos.core.models import AgentType
    from dagentos.plugins import describe, discover_executors
    executors = {AgentType.echo.value: EchoExecutor(), AgentType.tool.value: ToolExecutor(),
                 **discover_executors()}
    for item in describe(executors):
        health = item.get("health")
        if health is None:
            report.add(f"executor {item['name']}", OK, "registered (no health hook)")
            continue
        unhealthy = isinstance(health, dict) and (health.get("error")
                                                  or health.get("reachable") is False)
        report.add(f"executor {item['name']}", WARN if unhealthy else OK,
                   json.dumps(health, sort_keys=True))


def _check_configuration(report: Report) -> None:
    auth = os.environ.get("AGENTOS_AUTH", "asserted")
    report.add("auth", OK if auth == "bearer" else WARN,
               f"AGENTOS_AUTH={auth}" + ("" if auth == "bearer" else
                                         ": principals are client-asserted"))
    try:
        _policy(report)
    except Exception as exc:  # noqa: BLE001 — reported as a FAIL row, not a crash
        report.add("policy", FAIL, str(exc))


def _configured_keyring(report: Report) -> HmacKeyring | None:
    try:
        return _keyring(report)
    except Exception as exc:  # noqa: BLE001 — reported as a FAIL row, not a crash
        report.add("signing", FAIL, str(exc))
        return None


def _check_runs(store, run_ids: list[str], keyring: HmacKeyring | None, report: Report) -> None:
    bad_fold = bad_chain = 0
    for run_id in run_ids:
        try:
            fold(store.read_events(run_id))
        except FoldError:
            bad_fold += 1
        if _verify_run(store, run_id, keyring).status == FAIL:
            bad_chain += 1
    n = len(run_ids)
    report.add("runs fold", OK if not bad_fold else FAIL, f"{n - bad_fold}/{n}")
    report.add("runs verify", OK if not bad_chain else FAIL, f"{n - bad_chain}/{n} (chain + seals)")


# ------------------------------------------------------------------ policy explain

def _tier(cls: EffectClass, budget: Budget) -> str:
    if cls in budget.allowed_effect_classes:
        return "runs"
    if cls in budget.approval_required_for:
        return "asks approval"
    return "REFUSED"


def explain(wf: WorkflowDefinition, store, policy: OperatorPolicy | None) -> dict:
    effective, narrowed = apply_ceiling(wf.budget, policy)
    nodes = [_explain_node(node, store, policy, effective) for node in wf.nodes]
    return {"workflow": wf.name, "version": wf.version,
            "policy_sha256": policy_sha256(policy) if policy else None,
            "workflow_budget": wf.budget.model_dump(mode="json"),
            "effective_budget": effective.model_dump(mode="json"),
            "narrowed": narrowed, "nodes": nodes}


def _explain_node(node, store, policy: OperatorPolicy | None, effective: Budget) -> dict:
    agent = store.get_agent(node.agent)
    if agent is None:
        return {"id": node.id, "agent": node.agent, "problem": "unknown agent"}
    exec_name = agent.executor or agent.type.value
    allowed = executor_allowed(policy, exec_name)
    classes = {c.value: _tier(c, effective)
               for c in sorted(agent.declared_effects, key=lambda c: c.value)}
    outcome = ("fails at dispatch (executor not allowed)" if not allowed else
               "refused" if "REFUSED" in classes.values() else
               "asks approval" if "asks approval" in classes.values() else "runs")
    return {"id": node.id, "agent": f"{agent.name} v{agent.version}", "executor": exec_name,
            "executor_allowed": allowed, "declared": classes, "outcome": outcome}


def cmd_policy_explain(args: argparse.Namespace) -> int:
    store = store_from_env()
    wf = store.get_workflow(args.workflow)
    if wf is None:
        print(f"unknown workflow {args.workflow!r}", file=sys.stderr)
        return 2
    doc = explain(wf, store, _policy())
    if args.json:
        print(json.dumps(doc, indent=2, default=sorted))
    else:
        _print_explain(doc)
    return 0


def _print_explain(doc: dict) -> None:
    print(f"workflow {doc['workflow']} v{doc['version']}  policy "
          f"{(doc['policy_sha256'] or 'none')[:12]}")
    print("narrowed by the operator ceiling:" if doc["narrowed"] else
          "operator ceiling narrows nothing" if doc["policy_sha256"] else "no operator ceiling")
    for line in doc["narrowed"]:
        print(f"  - {line}")
    eb = doc["effective_budget"]
    print(f"effective: runs freely {sorted(eb['allowed_effect_classes'])}; asks approval "
          f"{sorted(eb['approval_required_for'])}; agent approval "
          f"{'allowed' if eb['allow_agent_approval'] else 'no'}; max step/run cost "
          f"{eb['max_step_cost']}/{eb['max_run_cost']}; max step wall {eb['max_step_wall_seconds']}")
    for n in doc["nodes"]:
        if "problem" in n:
            print(f"  {n['id']:<12} {n['agent']:<20} {n['problem']}")
            continue
        classes = ", ".join(f"{c}→{t}" for c, t in n["declared"].items()) or "no declared effects"
        print(f"  {n['id']:<12} {n['agent']:<20} {n['executor']:<14} {n['outcome']:<40} {classes}")


# ------------------------------------------------------------------ main

def _emit(report: Report, args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"checks": [asdict(c) for c in report.checks], "failed": report.failed},
                         indent=2))
    else:
        print(report.render())
    return 1 if report.failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentos", description=__doc__.split("\n\n")[1])
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="verify hash chains and seals")
    g = v.add_mutually_exclusive_group()
    g.add_argument("--run", help="one run id")
    g.add_argument("--all", action="store_true", help="every run (default)")
    v.set_defaults(fn=cmd_verify)
    d = sub.add_parser("doctor", help="operability checks")
    d.set_defaults(fn=cmd_doctor)
    pol = sub.add_parser("policy", help="operator policy tools")
    pol_sub = pol.add_subparsers(dest="policy_command", required=True)
    ex = pol_sub.add_parser("explain", help="what the ceiling does to one workflow")
    ex.add_argument("workflow")
    ex.set_defaults(fn=cmd_policy_explain)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("AGENTOS_LOG", "WARNING"),
                        format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
