"""Property (c): `POST /workflows/{name}/runs` is idempotent on `Idempotency-Key` (DESIGN §6).

Strategy: a sequence of 1–16 calls; each call carries one of five keys (so repeats are the
common case) or no key at all, and is randomly `?sync=true` (runs in-process, 201) or queued
(202). Keys are prefixed with the example's workflow name because the request_id → run map is
store-wide.

Properties, over the whole sequence:
  * every call with the same key returns the same run id, whatever the sync/queued mix and
    whatever happened in between;
  * distinct keys give distinct runs; every key-less call gives a brand-new run;
  * the number of runs that exist equals distinct keys + key-less calls — no phantom run was
    created and then returned as "existing";
  * each run's log carries exactly one `run.started`, whose `request_id` is the key.
"""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dagentos.core.events import RunStarted

from .conftest import PROPERTY, fresh_name

calls = st.lists(st.tuples(st.one_of(st.none(), st.integers(0, 4)), st.booleans()),
                 min_size=1, max_size=16)


@PROPERTY
@given(calls=calls)
def test_one_run_per_distinct_key_whatever_the_repeat_pattern(api, calls):
    main, c = api
    wf = fresh_name("idem")
    r = c.post("/workflows", json={"name": wf, "nodes": [{"id": "a", "agent": "calc"}]})
    assert r.status_code in (200, 201), r.text
    before = set(main.store.list_run_ids())

    by_key: dict[str, str] = {}
    keyless: list[str] = []
    for key, sync in calls:
        headers = {"Idempotency-Key": f"{wf}:{key}"} if key is not None else {}
        r = c.post(f"/workflows/{wf}/runs", headers=headers,
                   params={"sync": "true"} if sync else None)
        assert r.status_code == (201 if sync else 202), r.text
        run = r.json()
        if key is None:
            assert run["id"] not in keyless and run["id"] not in by_key.values()
            keyless.append(run["id"])
        else:
            assert run["request_id"] == headers["Idempotency-Key"]
            assert by_key.setdefault(headers["Idempotency-Key"], run["id"]) == run["id"], \
                f"key {key} returned a second run"

    ids = set(by_key.values()) | set(keyless)
    assert len(ids) == len(by_key) + len(keyless)                   # distinct keys → distinct runs
    assert set(main.store.list_run_ids()) - before == ids           # and no phantom run exists
    for key, rid in by_key.items():
        events = main.store.read_events(rid)
        started = [e for e in events if isinstance(e, RunStarted)]
        assert len(started) == 1 and started[0].request_id == key and events[0] is started[0]
