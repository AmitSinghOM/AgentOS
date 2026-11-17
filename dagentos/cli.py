"""`agentos` — the operator's command line (Phase 8 #3 / #4, A12).

    agentos verify [--run ID | --all]      hash chain + seals for one run or every run
    agentos doctor                         store, migrations, executors, every run folds and
                                           verifies, auth / policy / signing configured
    agentos policy explain <workflow>      the workflow's budget, the effective budget under
                                           the operator ceiling, and per node what runs freely,
                                           asks, or is refused
    agentos pause --all [--reason ..]      kill switch: workers finish in-flight steps and
    agentos resume --all                   dispatch nothing new until resumed; runs are not
    agentos pause --status                 cancelled or paused individually (A12 / E-03)
    agentos init <dir>                     a minimal project: .env, agent.json, workflow.json,
                                           README.md — the quick start's files (E-04)
    agentos run <workflow.json>            register the agents + workflow and advance one run
        [--agents a.json ...]              in-process to a terminal state (the API's `?sync=true`
        [--input k=v] [--store URL]        path, no server); exit 3 when it suspends for approval
    agentos export-run --run ID | --all    one run (or every run, one stream) as JSON Lines for
        --format jsonl [--out PATH|-]      an auditor or evaluator: header, events as stored,
        [--include-blobs]                  fold summary; exit 1 if a chain fails verify (A11)
    agentos archive --older-than 30d       bound disk: export every terminal run last updated
        --out DIR [--status completed,..]  before the cutoff to DIR/<run_id>.jsonl, read it back,
        [--include-blobs] [--dry-run|--yes] then purge its rows; DIR/ARCHIVE.jsonl records each.
                                           Dry run by default; a run whose chain fails verify is
                                           exported but KEPT (exit 1); non-terminal never touched

All of them read the same environment as the API and the worker (`dagentos.store.factory`,
`AGENTOS_POLICY`, `AGENTOS_SIGNING_KEYS`, `AGENTOS_AUTH`). Exit status is 0 only when nothing
failed; warnings (unconfigured but not wrong) do not fail. Output is plain text a human reads
in a terminal; `--json` gives the same facts as one JSON document for scripts.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dagentos.core.events import SchedulerState
from dagentos.core.fold import FoldError, fold
from dagentos.core.integrity import IntegrityError, verify
from dagentos.core.models import (
    Agent,
    Budget,
    EffectClass,
    Principal,
    PrincipalKind,
    RunStatus,
    WorkflowDefinition,
)
from dagentos.core.policy import OperatorPolicy, apply_ceiling, executor_allowed, policy_sha256
from dagentos.core.ports import ConflictError
from dagentos.core.seal import HmacKeyring, verify_seals
from dagentos.export import TERMINAL_STATUSES, ArchiveReport, archive_runs, write_run
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
    indexed = {r.run_id: r.status for r in store.list_runs(limit=None).runs}
    bad_fold = bad_chain = 0
    drifted: list[str] = []
    for run_id in run_ids:
        try:
            if fold(store.read_events(run_id)).status is not indexed.get(run_id):
                drifted.append(run_id)
        except FoldError:
            bad_fold += 1                 # counted here; its index row is not judged
        if _verify_run(store, run_id, keyring).status == FAIL:
            bad_chain += 1
    n = len(run_ids)
    report.add("runs fold", OK if not bad_fold else FAIL, f"{n - bad_fold}/{n}")
    report.add("runs verify", OK if not bad_chain else FAIL, f"{n - bad_chain}/{n} (chain + seals)")
    # E-07: read-only. A disagreeing row can hide a run from a status filter, the approvals
    # inbox or the sweep; every per-run view still serves the fold (docs/FAIL_MODES.md).
    judged = n - bad_fold
    report.add("runs index", OK if not drifted else FAIL,
               f"{judged - len(drifted)}/{judged} runs.status rows agree with the fold"
               + (f"; disagree: {', '.join(r[:8] for r in drifted[:5])}"
                  + (f" (+{len(drifted) - 5} more)" if len(drifted) > 5 else "")
                  + " (API and worker still act on the fold; docs/FAIL_MODES.md, run index)"
                  if drifted else ""))


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


# ------------------------------------------------------------------ pause / resume (A12)

def _operator(name: str | None) -> Principal:
    """`--principal NAME` names the human at the keyboard; otherwise the record says the CLI
    itself did it (system:cli), never an invented human."""
    return (Principal(kind=PrincipalKind.human, id=name) if name
            else Principal(kind=PrincipalKind.system, id="cli"))


def _emit_scheduler(state: SchedulerState, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(state.model_dump(mode="json")))
        return 0
    line = "scheduler: " + ("paused" if state.paused else "running")
    if state.changed_at is None:
        print(line + " (never paused)")
        return 0
    print(f"{line}  since {state.changed_at.isoformat(timespec='seconds')}  by {state.who()}"
          + (f"  reason: {state.reason}" if state.reason else ""))
    return 0


def _set_scheduler(args: argparse.Namespace, *, paused: bool) -> int:
    store = store_from_env()
    store.set_scheduler_state(SchedulerState(
        paused=paused, principal=_operator(args.principal), reason=args.reason or "",
        changed_at=datetime.now(UTC)))
    return _emit_scheduler(store.get_scheduler_state(), args)   # read back: what workers see


def cmd_pause(args: argparse.Namespace) -> int:
    if args.status:
        return _emit_scheduler(store_from_env().get_scheduler_state(), args)
    return _set_scheduler(args, paused=True)


def cmd_resume(args: argparse.Namespace) -> int:
    return _set_scheduler(args, paused=False)


# ------------------------------------------------------------------ init / run (E-04)

# The README quick start's two example files (examples/echo_agent.json, hello_workflow.json),
# embedded because the wheel ships `dagentos/` only; tests/test_cli_init.py pins them equal.
INIT_AGENT = {"name": "greeter", "type": "echo", "config": {"message": "hello from agentos"}}
INIT_WORKFLOW = {"name": "hello", "nodes": [{"id": "a", "agent": "greeter", "depends_on": []},
                                            {"id": "b", "agent": "greeter", "depends_on": ["a"]}]}
INIT_ENV = """\
# Store: AGENTOS_STORE=memory|sqlite|postgres (default sqlite -> ./agentos.db, AGENTOS_SQLITE_PATH)
AGENTOS_STORE=sqlite
# Authentication. `asserted` records the principal a request claims, UNVERIFIED (fine on your
# laptop; the API warns at startup). Before anyone else can reach the API switch to bearer:
# tokens resolve to principals through a file of SHA-256 hashes (README "Authentication").
AGENTOS_AUTH=asserted
# AGENTOS_AUTH=bearer
# AGENTOS_AUTH_TOKENS=./tokens.json
"""
INIT_README = """\
# AgentOS demo

`agent.json` is an `echo` agent; `workflow.json` runs it twice, `b` after `a`.

```bash
agentos run workflow.json --agents agent.json      # one run, in-process, to a terminal state
uvicorn dagentos.api.main:app                      # or the API ... (reads .env via your shell)
python -m dagentos.worker                          # ... with a worker, in another terminal
```

`agentos run` exits 0 when the run completed, 3 when it suspended for a human's approval
(decide at http://127.0.0.1:8000/ui once the API is up), 1 when a step failed.
"""


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        print(f"error: {target} is not empty; refusing to overwrite", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)
    (target / ".env").write_text(INIT_ENV)
    (target / "agent.json").write_text(json.dumps(INIT_AGENT, indent=2) + "\n")
    (target / "workflow.json").write_text(json.dumps(INIT_WORKFLOW, indent=2) + "\n")
    (target / "README.md").write_text(INIT_README)
    print(f"wrote {target}/{{.env, agent.json, workflow.json, README.md}}\n"
          f"next: cd {target} && agentos run workflow.json --agents agent.json")
    return 0


def _store_from_arg(url: str | None):
    """`--store sqlite:///path | memory | postgresql://...`; unset → the environment, as the
    API and worker read it."""
    if url is None:
        return store_from_env()
    if url == "memory":
        from dagentos.store.memory import MemoryStore
        return MemoryStore()
    if url.startswith("sqlite:///"):
        from dagentos.store.sqlite import SqliteStore
        return SqliteStore(url[len("sqlite:///"):])
    if url.startswith(("postgresql://", "postgres://")):
        from dagentos.store.postgres import PostgresStore
        return PostgresStore(url)
    raise RuntimeError(f"--store must be sqlite:///PATH, memory or postgresql://..., got {url!r}")


def _inputs(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise RuntimeError(f"--input expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def _engine(store):
    """The API's composition (dagentos/api/main.py) minus observers: built-in executors plus
    discovered plugins, the operator ceiling and keyring when the environment names them."""
    from dagentos.agents.echo import EchoExecutor
    from dagentos.agents.tool import ToolExecutor
    from dagentos.core.engine import Engine
    from dagentos.core.models import AgentType
    from dagentos.plugins import discover_executors
    executors = {AgentType.echo.value: EchoExecutor(), AgentType.tool.value: ToolExecutor(),
                 **discover_executors()}
    return Engine(store=store, blobs=store, executors=executors,
                  lease=store if hasattr(store, "acquire") else None,
                  policy=_policy(), keyring=_keyring())


def cmd_run(args: argparse.Namespace) -> int:
    store = _store_from_arg(args.store)
    inputs = _inputs(args.input)
    for path in args.agents:
        agent = Agent.model_validate_json(Path(path).read_text())
        try:
            store.put_agent(agent)                 # identical re-post is a no-op (API rule)
        except ConflictError as exc:
            raise RuntimeError(f"{path}: {exc}") from exc
    wf = WorkflowDefinition.model_validate_json(Path(args.workflow).read_text())
    wf.validate_dag()
    store.put_workflow(wf)
    run = _engine(store).start_run(wf.name, principal=_operator(None), inputs=inputs or None)
    events = len(store.read_events(run.id))
    pending = sum(1 for a in run.approvals.values() if a.status.value == "pending")
    if args.json:
        print(json.dumps({"run_id": run.id, "status": run.status.value, "events": events,
                          "pending_approvals": pending,
                          "dead_lettered": run.dead_lettered, "failed_steps": run.failed_steps}))
    else:
        print(f"run {run.id}: {run.status.value}, {events} events")
        if run.status is RunStatus.suspended:
            print(f"  suspended: {pending} approval(s) pending — decide in the UI or with "
                  f"POST /runs/{run.id}/approvals/{{approval_id}}/approve")
        for step_id, cause in run.dead_lettered.items():
            print(f"  step {step_id} dead-lettered: {cause}")
        for step_id, error in run.failed_steps.items():
            print(f"  step {step_id} failed: {error}")
    if run.status is RunStatus.completed:
        return 0
    return 3 if run.status is RunStatus.suspended else 1


# ------------------------------------------------------------------ export-run (A11 / E-08)

@contextmanager
def _export_target(path: str) -> Iterator:
    """`-` is stdout. A file is created 0600, like the SQLite store: it holds run data."""
    if path == "-":
        yield sys.stdout
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8", newline="\n") as fh:
        yield fh


def cmd_export_run(args: argparse.Namespace) -> int:
    """Read-only. A run whose chain or seals fail is STILL exported — an auditor needs the
    tampered record too — with `chain.verified: false` in its header, and the exit is 1."""
    store = store_from_env()
    if args.run is not None and not store.read_events(args.run, limit=1):
        print(f"unknown run {args.run!r}", file=sys.stderr)
        return 2
    run_ids = [args.run] if args.run is not None else store.list_run_ids()
    keyring = _keyring()
    exported_at = datetime.now(UTC).isoformat(timespec="seconds")
    failed = False
    with _export_target(args.out) as out:
        for run_id in run_ids:
            chain, missing = write_run(out, store, store, run_id, keyring=keyring,
                                       include_blobs=args.include_blobs,
                                       exported_at=exported_at)
            if not chain["verified"]:
                failed = True
                print(f"run {run_id}: exported with verified=false: {chain['error']}",
                      file=sys.stderr)
            for sha in missing:
                print(f"warning: run {run_id}: blob {sha} is referenced but not in the store",
                      file=sys.stderr)
    return 1 if failed else 0


# ------------------------------------------------------------------ archive (E-08)

def _now() -> datetime:
    """The archive's clock (cutoff and `exported_at`); one seam so tests can set it."""
    return datetime.now(UTC)


_DURATION = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _duration(text: str) -> timedelta:
    """`--older-than`: an integer and one unit — m(inutes), h(ours), d(ays), w(eeks)."""
    m = _DURATION.match(text)
    if m is None:
        raise ValueError(f"--older-than expects <N>m|h|d|w (e.g. 30d), got {text!r}")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def _statuses(text: str | None) -> list[RunStatus]:
    """`--status`: a comma-separated subset of the terminal statuses; default all of them."""
    if text is None:
        return list(TERMINAL_STATUSES)
    allowed = {s.value: s for s in TERMINAL_STATUSES}
    chosen = [part.strip() for part in text.split(",") if part.strip()]
    bad = [c for c in chosen if c not in allowed]
    if bad or not chosen:
        raise ValueError(f"--status accepts {', '.join(allowed)} (comma-separated), got {text!r}")
    return [allowed[c] for c in chosen]


def _print_archive(report: ArchiveReport, older_than: str) -> None:
    mode = "dry run" if report.dry_run else "archive"
    verb = "would archive" if report.dry_run else "archived"
    print(f"{mode}: runs last updated before {report.cutoff} (older than {older_than}) "
          f"→ {report.out}")
    for a in report.archived:
        print(f"  {verb:<14} {a.run_id}  {a.status}  {a.events} events"
              + (f"  → {a.file}" if a.file else "")
              + (f"  {len(a.blobs_removed)} blob(s) removed" if a.blobs_removed else ""))
    for k in report.kept:
        print(f"  {'KEPT' if not report.dry_run else 'would keep':<14} {k.run_id}  "
              f"chain unverified: {k.error}" + (f"  → {k.file}" if k.file else ""))
    for s in report.skipped:
        print(f"  {'skipped':<14} {s.run_id}  {s.status}  ({s.reason})")
    print(f"{len(report.archived)} {verb}, {len(report.kept)} kept (chain unverified), "
          f"{len(report.skipped)} skipped"
          + (" — dry run: nothing written, nothing removed; add --yes to perform"
             if report.dry_run else f"; manifest {report.manifest}" if report.manifest else ""))


def cmd_archive(args: argparse.Namespace) -> int:
    """Export-then-purge, dry run by default (dagentos.export.archive_runs). Exit 1 when a run
    was exported but kept because its chain or seals failed: the store never loses the only
    evidence of a tampering, and the operator is told which run to look at."""
    older_than = _duration(args.older_than)
    statuses = _statuses(args.status)
    store = store_from_env()
    now = _now()
    report = archive_runs(store, store, Path(args.out), cutoff=now - older_than,
                          statuses=statuses, include_blobs=args.include_blobs,
                          keyring=_keyring(), dry_run=not args.yes,
                          exported_at=now.isoformat(timespec="seconds"))
    for kept in report.kept:
        print(f"run {kept.run_id}: exported with verified=false, kept in the store: {kept.error}",
              file=sys.stderr)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_archive(report, args.older_than)
    return 1 if report.failed else 0


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
    pa = sub.add_parser("pause", help="global pause (kill switch): dispatch nothing new")
    pg = pa.add_mutually_exclusive_group(required=True)
    pg.add_argument("--all", action="store_true", help="pause every worker's dispatch")
    pg.add_argument("--status", action="store_true", help="show the state and its last change")
    pa.add_argument("--reason", default="", help="why (recorded with the change)")
    pa.add_argument("--principal", default=None, help="operator name to record (default system:cli)")
    pa.set_defaults(fn=cmd_pause)
    re_ = sub.add_parser("resume", help="clear the global pause; parked runs continue")
    re_.add_argument("--all", action="store_true", required=True, help="resume every worker")
    re_.add_argument("--reason", default="", help="why (recorded with the change)")
    re_.add_argument("--principal", default=None, help="operator name to record (default system:cli)")
    re_.set_defaults(fn=cmd_resume)
    ini = sub.add_parser("init", help="write a minimal project (.env, agent.json, workflow.json)")
    ini.add_argument("dir", help="target directory; must not exist or be empty")
    ini.set_defaults(fn=cmd_init)
    run = sub.add_parser("run", help="register agents + workflow and advance one run in-process")
    run.add_argument("workflow", help="workflow definition JSON file")
    run.add_argument("--agents", nargs="+", default=[], metavar="FILE",
                     help="agent definition JSON file(s) to register first")
    run.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                     help="run input, reaches steps as {run.KEY}; repeatable")
    run.add_argument("--store", default=None, metavar="URL",
                     help="sqlite:///PATH | memory | postgresql://... (default: AGENTOS_STORE env)")
    run.set_defaults(fn=cmd_run)
    exp = sub.add_parser("export-run", help="one run (or --all) as JSON Lines for auditors and "
                                            "external evaluators")
    eg = exp.add_mutually_exclusive_group(required=True)
    eg.add_argument("--run", help="one run id")
    eg.add_argument("--all", action="store_true", help="every run, one stream, oldest first")
    exp.add_argument("--format", required=True, choices=["jsonl"], help="output format")
    exp.add_argument("--out", default="-", metavar="PATH",
                     help="file to write (created 0600); - for stdout (default)")
    exp.add_argument("--include-blobs", action="store_true",
                     help="append every referenced blob (inputs, outputs, pricing) as base64")
    exp.set_defaults(fn=cmd_export_run)
    arc = sub.add_parser("archive", help="export terminal runs older than a cutoff to files, "
                                         "then purge them from the store (dry run by default)")
    arc.add_argument("--older-than", required=True, metavar="DURATION",
                     help="runs last updated before now minus this: <N>m|h|d|w, e.g. 30d")
    arc.add_argument("--out", required=True, metavar="DIR",
                     help="archive directory (created 0700): <run_id>.jsonl per run + ARCHIVE.jsonl")
    arc.add_argument("--status", default=None, metavar="LIST",
                     help="comma-separated subset of completed,failed,cancelled (default: all)")
    arc.add_argument("--include-blobs", action="store_true",
                     help="write referenced blobs into each file and remove those no remaining "
                          "run references; without it every blob stays in the store")
    ag = arc.add_mutually_exclusive_group()
    ag.add_argument("--dry-run", action="store_true", help="list what would leave (default)")
    ag.add_argument("--yes", action="store_true", help="perform the export and the purge")
    arc.set_defaults(fn=cmd_archive)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("AGENTOS_LOG", "WARNING"),
                        format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (RuntimeError, OSError, ValueError) as exc:
        # ValueError: a workflow/agent file that does not validate, or a DAG with a cycle;
        # OSError: a file the command could not read or write (archive dir, export target)
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
