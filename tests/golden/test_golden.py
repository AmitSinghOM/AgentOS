"""Golden replay corpus: every run log ever recorded by a released version must fold to
the same state under the current build. This is the seven-year test
(docs/DEVELOPMENT_STRUCTURE.md §5.3). Files are immutable; add new ones per release with
scripts/record_golden.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dagentos.core.events import from_record
from dagentos.core.fold import fold

GOLDEN = Path(__file__).resolve().parent
FILES = sorted(GOLDEN.glob("*.json"))


def _subset_drift(recorded, folded, path="$") -> list[str]:
    """Recorded must be a recursive subset of folded: every recorded key present with an
    equal value; lists compared element-wise with equal length. Extra keys in `folded`
    are additive and allowed. Returns human-readable drift descriptions."""
    if isinstance(recorded, dict) and isinstance(folded, dict):
        out = []
        for k, v in recorded.items():
            if k not in folded:
                out.append(f"{path}.{k}: recorded field vanished")
            else:
                out += _subset_drift(v, folded[k], f"{path}.{k}")
        return out
    if isinstance(recorded, list) and isinstance(folded, list):
        if len(recorded) != len(folded):
            return [f"{path}: length {len(recorded)} → {len(folded)}"]
        out = []
        for i, (r, f) in enumerate(zip(recorded, folded, strict=True)):
            out += _subset_drift(r, f, f"{path}[{i}]")
        return out
    return [] if recorded == folded else [f"{path}: {recorded!r} → {folded!r}"]


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_golden_log_folds_to_recorded_state(path: Path):
    doc = json.loads(path.read_text())
    events = [from_record(r) for r in doc["events"]]
    folded = fold(events).model_dump(mode="json")
    recorded = doc["expected"]
    # Every field the recording build knew about must fold identically, at any depth.
    # Fields added since (derived views such as `progress`, `total_cost`, per-step
    # `effects`/`cost`/`provenance`) may appear — that is additive and allowed. A
    # recorded field going missing or changing value is the compatibility break this
    # test exists to catch.
    drift = _subset_drift(recorded, folded)
    assert not drift, f"{path.name}: fold drifted from recorded state:\n  " + "\n  ".join(drift)
    # Structural invariants every corpus entry must satisfy.
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert len({(e.run_id, e.seq) for e in events}) == len(events)


def test_corpus_is_not_empty():
    assert FILES, "record at least one golden log: python scripts/record_golden.py <label>"


def test_subset_drift_allows_additive_fields_but_catches_changes():
    """The comparator must tolerate fields added since a recording, and nothing else."""
    rec = {"status": "completed", "steps": [{"node_id": "a", "attempt": 1}]}
    assert _subset_drift(rec, {"status": "completed", "progress": {},
                               "steps": [{"node_id": "a", "attempt": 1, "cost": {}}]}) == []
    assert _subset_drift(rec, {"status": "failed", "steps": [{"node_id": "a", "attempt": 1}]}) \
        == ["$.status: 'completed' → 'failed'"]
    assert _subset_drift(rec, {"steps": [{"node_id": "a", "attempt": 1}]}) \
        == ["$.status: recorded field vanished"]
    assert _subset_drift(rec, {"status": "completed", "steps": []}) == ["$.steps: length 1 → 0"]
    assert _subset_drift(rec, {"status": "completed", "steps": [{"node_id": "b", "attempt": 1}]}) \
        == ["$.steps[0].node_id: 'a' → 'b'"]


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_fold_from_any_cut_point_equals_the_full_fold(path: Path):
    """C15 invariant (review finding F9): for EVERY k, continuing a fold from the state at k
    yields exactly the full fold. Run over the whole golden corpus so `_apply`'s seeded
    accumulators are checked against every event type and log shape we have ever
    released, and so a WorkflowRun field added later but not re-seeded fails here."""
    from dagentos.core.fold import fold_from
    doc = json.loads(path.read_text())
    events = [from_record(r) for r in doc["events"]]
    full = fold(events).model_dump(mode="json")
    for k in range(1, len(events) + 1):
        snapshot = fold(events[:k])
        continued = fold_from(snapshot, events[k:]).model_dump(mode="json")
        assert continued == full, f"{path.stem}: fold_from at k={k} diverges from fold"


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_seals_in_golden_logs_verify_with_the_fixture_key_or_are_absent(path: Path):
    """From v0.9.0 the corpus carries `integrity.sealed` events signed with the golden
    FIXTURE key (scripts/record_golden.py). Older logs have none and report `unsigned`.
    Either way a seal must never be INVALID against the recorded log."""
    from dagentos.core.integrity import verify
    from dagentos.core.seal import verify_seals
    from scripts.record_golden import GOLDEN_KEYRING

    events = [from_record(r) for r in json.loads(path.read_text())["events"]]
    verify(events)                                          # the chain itself
    report = verify_seals(events, GOLDEN_KEYRING)
    assert report.state in ("unsigned", "verified"), report.as_dict()
    if report.seals:
        assert report.valid == report.seals and report.unsigned_tail == 0, report.as_dict()
