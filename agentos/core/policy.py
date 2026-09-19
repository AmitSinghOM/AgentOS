"""Operator policy ceiling (Phase 8 #2): a bound over EVERY workflow's budget.

Every limit the core enforces comes from the workflow's `Budget`, written by whoever defines
the workflow. That is the right place for per-workflow rules and the wrong place for the
operator's rules: a workflow author could allow `spend` freely or let agents approve it. The
operator policy is one document, loaded by the API and the worker from `AGENTOS_POLICY`,
that every workflow budget is intersected with before the gate sees it. **Tightest wins;
a policy can only narrow.** The gate itself is unchanged — it is correct against the
`Budget` it is given, and this module decides which `Budget` that is.

    {
      "version": 1,
      "allowed_executors": ["echo", "tool", "openai-compat"],   # null = any
      "effect_ceiling": ["read", "compute", "spend"],           # null = any; outside → refused
      "always_approve": ["spend"],                              # needs a decision even if a
                                                                #   workflow allows it freely
      "agent_approval_allowed": false,                          # false → workflows cannot
                                                                #   enable allow_agent_approval
      "max_step_cost": "0.50", "max_run_cost": "5.00",          # workflow values may only be lower
      "max_step_wall_seconds": 600
    }

What the policy changes is recorded: `governance.policy_applied` follows `run.started` with
the policy's sha256 and the list of narrowings, so a reader of the log knows which ceiling
governed the run without the file. Replay never consults the policy — gate outcomes are
already events.

No `AGENTOS_POLICY` → no ceiling (today's behaviour) and one WARNING at startup. Set and
unreadable or malformed → the process refuses to start, naming the variable and the entry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from agentos.core.models import Budget, EffectClass

logger = logging.getLogger("agentos.policy")


class PolicyError(RuntimeError):
    """Configuration error. The message names the variable or entry and the fix."""


class OperatorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    allowed_executors: frozenset[str] | None = None
    effect_ceiling: frozenset[EffectClass] | None = None
    always_approve: frozenset[EffectClass] = frozenset()
    agent_approval_allowed: bool = True
    max_step_cost: str | None = None
    max_run_cost: str | None = None
    max_step_wall_seconds: float | None = None

    @field_validator("max_step_cost", "max_run_cost")
    @classmethod
    def _decimal_string(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            if Decimal(v) < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError(f"must be a non-negative decimal string, got {v!r}") from None
        return v

    @field_validator("max_step_wall_seconds")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"must be > 0, got {v!r}")
        return v


def _min_decimal(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if Decimal(a) <= Decimal(b) else b


def apply_ceiling(budget: Budget, policy: OperatorPolicy | None) -> tuple[Budget, list[str]]:
    """The budget the gate will see, and a human-readable line per narrowing. A policy of
    None returns the budget untouched with no narrowings."""
    if policy is None:
        return budget, []
    narrowed: list[str] = []
    allowed = set(budget.allowed_effect_classes)
    approval = set(budget.approval_required_for)

    if policy.effect_ceiling is not None:
        for name, classes in (("allowed_effect_classes", allowed), ("approval_required_for", approval)):
            gone = classes - policy.effect_ceiling
            if gone:
                narrowed.append(f"{name}: removed {_names(gone)} (outside effect_ceiling)")
                classes -= gone

    for c in sorted(policy.always_approve, key=lambda c: c.value):
        if c in allowed:
            allowed.discard(c)
            approval.add(c)
            narrowed.append(f"{c.value}: allowed → approval_required (always_approve)")

    allow_agent = budget.allow_agent_approval
    if allow_agent and not policy.agent_approval_allowed:
        allow_agent = False
        narrowed.append("allow_agent_approval: true → false (agent_approval_allowed=false)")

    step_cost = _min_decimal(budget.max_step_cost, policy.max_step_cost)
    if step_cost != budget.max_step_cost:
        narrowed.append(f"max_step_cost: {budget.max_step_cost} → {step_cost}")
    run_cost = _min_decimal(budget.max_run_cost, policy.max_run_cost)
    if run_cost != budget.max_run_cost:
        narrowed.append(f"max_run_cost: {budget.max_run_cost} → {run_cost}")
    wall = budget.max_step_wall_seconds
    if policy.max_step_wall_seconds is not None and (wall is None or wall > policy.max_step_wall_seconds):
        narrowed.append(f"max_step_wall_seconds: {wall} → {policy.max_step_wall_seconds}")
        wall = policy.max_step_wall_seconds

    effective = budget.model_copy(update={
        "allowed_effect_classes": allowed, "approval_required_for": approval,
        "allow_agent_approval": allow_agent, "max_step_cost": step_cost,
        "max_run_cost": run_cost, "max_step_wall_seconds": wall})
    return effective, narrowed


def executor_allowed(policy: OperatorPolicy | None, executor_name: str) -> bool:
    return policy is None or policy.allowed_executors is None \
        or executor_name in policy.allowed_executors


def _names(classes: set[EffectClass]) -> str:
    return ", ".join(sorted(c.value for c in classes))


def policy_sha256(policy: OperatorPolicy) -> str:
    """Hash of the canonical policy document (sorted keys, sorted sets), so the same
    ceiling written two ways has one identity in the log."""
    doc = policy.model_dump(mode="json")
    for key in ("allowed_executors", "effect_ceiling", "always_approve"):
        if doc[key] is not None:
            doc[key] = sorted(doc[key])
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_policy(path: str | os.PathLike[str]) -> OperatorPolicy:
    p = Path(path)
    where = f"AGENTOS_POLICY={str(p)!r}"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"{where}: file not found") from None
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{where}: not valid JSON ({exc.msg})") from None
    try:
        return OperatorPolicy.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"]) or "<root>"
        raise PolicyError(f"{where}: {loc}: {first['msg']}") from None


def policy_from_env() -> OperatorPolicy | None:
    """Composition-root helper. None (with a WARNING) when AGENTOS_POLICY is unset."""
    path = os.environ.get("AGENTOS_POLICY")
    if not path:
        logger.warning("AGENTOS_POLICY unset: no operator ceiling — every workflow's own "
                       "budget is the only limit. Set AGENTOS_POLICY=<file> before exposing "
                       "this deployment to more than one workflow author.")
        return None
    policy = load_policy(path)
    logger.info("operator policy loaded from %s (sha256 %s)", path, policy_sha256(policy)[:12])
    return policy


__all__ = ["OperatorPolicy", "PolicyError", "apply_ceiling", "executor_allowed", "load_policy",
           "policy_from_env", "policy_sha256"]
