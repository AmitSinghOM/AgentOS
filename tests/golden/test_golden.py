"""Golden replay corpus: every run log ever recorded by a released version must fold to
the same state under the current build. This is the seven-year test
(docs/DEVELOPMENT_STRUCTURE.md §5.3). Files are immutable; add new ones per release with
scripts/record_golden.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.core.events import from_record
from agentos.core.fold import fold
from agentos.core.models import WorkflowRun

GOLDEN = Path(__file__).resolve().parent
FILES = sorted(GOLDEN.glob("*.json"))


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_golden_log_folds_to_recorded_state(path: Path):
    doc = json.loads(path.read_text())
    events = [from_record(r) for r in doc["events"]]
    folded = fold(events)
    expected = WorkflowRun.model_validate(doc["expected"])
    assert folded == expected, f"{path.name}: fold drifted from recorded state"
    # Structural invariants every corpus entry must satisfy.
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert len({(e.run_id, e.seq) for e in events}) == len(events)


def test_corpus_is_not_empty():
    assert FILES, "record at least one golden log: python scripts/record_golden.py <label>"
