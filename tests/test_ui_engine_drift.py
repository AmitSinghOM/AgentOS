"""Drift lock between the engine's approval rule and the UI that now states it in words.

#96 gave `ui/src/ApprovalCard.tsx` a copy of HUMAN_ONLY_EFFECTS ("requires a human principal") and
#98 gave `ui/src/permission.ts` a re-implementation of `policy.apply_ceiling`'s agent-approval
narrowing ("The API will accept / refuse this"). Both are correct today, and nothing kept them so:
`debate2.test.tsx` exercised the sentence against mocked inputs, i.e. the UI's own rule. This
module (1) parses the TS constant and compares it with the engine's, and (2) drives `engine.approve`
with a non-human principal through `tests/fixtures/agent_approval_matrix.json` -- the same table
the UI test renders -- so a change to either side turns one of the two runners red.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dagentos.core.engine import ControlNotAllowed
from dagentos.core.models import HUMAN_ONLY_EFFECTS, Budget, RunStatus
from dagentos.core.policy import OperatorPolicy
from tests.test_policy import AGENT, _engine

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "tests" / "fixtures" / "agent_approval_matrix.json"
CARD = ROOT / "ui" / "src" / "ApprovalCard.tsx"
UI_TEST = ROOT / "ui" / "src" / "debate2.test.tsx"

ROWS = json.loads(MATRIX.read_text())["rows"]


def test_ui_human_only_effects_equal_the_engines():
    src = CARD.read_text()
    m = re.search(r"export const HUMAN_ONLY_EFFECTS = new Set\(\[(.*?)\]\);", src)
    assert m, "ApprovalCard.tsx no longer declares HUMAN_ONLY_EFFECTS as a Set literal"
    ui = {s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()}
    assert ui == {c.value for c in HUMAN_ONLY_EFFECTS}, (
        f"UI human-only classes {sorted(ui)} drifted from the engine's "
        f"{sorted(c.value for c in HUMAN_ONLY_EFFECTS)}")


def test_matrix_covers_every_workflow_x_policy_combination():
    combos = {(r["workflow_allow_agent_approval"], r["policy_agent_approval_allowed"]) for r in ROWS}
    assert combos == {(w, p) for w in (False, True) for p in (None, False, True)}


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"wf={r['workflow_allow_agent_approval']}-pol={r['policy_agent_approval_allowed']}")
def test_engine_approve_matches_the_matrix_for_a_non_human(row):
    budget = Budget(allow_agent_approval=row["workflow_allow_agent_approval"])
    pol = row["policy_agent_approval_allowed"]
    policy = None if pol is None else OperatorPolicy(agent_approval_allowed=pol)
    _store, eng, _ex = _engine(budget, policy)
    run = eng.start_run("w")
    assert run.status is RunStatus.suspended          # pay declares spend: human-only class, gated
    (approval,) = run.approvals.values()
    if row["accept"]:
        assert eng.approve(run.id, approval.approval_id, principal=AGENT).status is RunStatus.running
    else:
        with pytest.raises(ControlNotAllowed, match="human principal"):
            eng.approve(run.id, approval.approval_id, principal=AGENT)


def test_the_ui_test_reads_the_same_matrix():
    """The vitest side must consume the fixture, not restate it -- otherwise this is two tables."""
    assert "agent_approval_matrix.json" in UI_TEST.read_text(), (
        "debate2.test.tsx does not read tests/fixtures/agent_approval_matrix.json")
