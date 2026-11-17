"""Property (b): the tamper-evident chain (C12, dagentos/core/integrity.py) over random logs.

tests/test_trust_boundary.py shows one edit, one insert and one remove on one two-step log.
This is the same claim for every position of every random log: a log is produced by driving
a random workflow (`workflows()`) through the engine with random operator actions, then ONE
record is edited, inserted or removed at a random position, the seqs are renumbered so the
log looks legal, and `fold` must refuse it with `FoldError`.

Strategies: `workflows()` from conftest plus the interactive interleaving `drive()` draws for
the log; `tamper` draws the kind and a position (any record for edit; any gap for insert; any
record but the LAST for remove). Edits touch `occurred_at` or, when the record has one,
`step_id` / `error` / `approval_id` / `agent`.

Not covered, by design: removing the tail event. The chain cannot see a truncation that ends
on a valid record — that is what the seals are for (dagentos/core/seal.py, Phase 8 #3), and
the untampered control case asserts the chain fully verifies (`integrity_verified == len`).
"""
from __future__ import annotations

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from dagentos.core.events import from_record
from dagentos.core.fold import FoldError, fold
from dagentos.store.memory import MemoryStore

from .conftest import (
    PROPERTY,
    ScriptedExecutor,
    drive,
    make_engine,
    put_workflow,
    workflows,
)

PAYLOAD_FIELDS = ("step_id", "error", "approval_id", "agent")


def _log(wf, data) -> list[dict]:
    nodes, parallelism, crash_at = wf
    store = MemoryStore()
    put_workflow(store, "w", nodes, parallelism)
    engine = make_engine(store, ScriptedExecutor(crash_at))
    run_id, events = drive(engine, store, "w", data)
    assert fold(events).integrity_verified == len(events)         # control: untampered verifies
    return [e.to_record() for e in events]


def _renumber(records: list[dict]) -> list[dict]:
    for i, rec in enumerate(records, start=1):
        rec["seq"] = i
    return records


@PROPERTY
@given(wf=workflows(), data=st.data())
def test_any_single_edit_insert_or_remove_is_refused_by_the_fold(wf, data):
    records = _log(wf, data)
    n = len(records)
    kind = data.draw(st.sampled_from(["edit", "insert", "remove"]), label="tamper")
    event(f"tamper: {kind}")
    if kind == "edit":
        i = data.draw(st.integers(0, n - 1), label="position")
        fields = [f for f in PAYLOAD_FIELDS if f in records[i]] + ["occurred_at"]
        field = data.draw(st.sampled_from(fields), label="field")
        if field == "occurred_at":
            records[i][field] = "2001-01-01T00:00:00Z"            # before the project existed
        else:
            records[i][field] = f"{records[i][field]}-tampered"
    elif kind == "insert":
        i = data.draw(st.integers(0, n), label="position")
        source = data.draw(st.integers(0, n - 1), label="copied-from")
        records.insert(i, dict(records[source]))                    # a replayed event
    else:
        i = data.draw(st.integers(0, n - 2), label="position")      # never the tail (see docstring)
        del records[i]
    _renumber(records)

    with pytest.raises(FoldError):
        fold(from_record(r) for r in records)
