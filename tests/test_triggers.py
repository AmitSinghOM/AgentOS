"""Triggers: cron and authenticated webhooks that start runs through the public API.

Nothing here touches the engine directly. A cron slot or a webhook delivery becomes
`POST /workflows/{name}/runs` with an `Idempotency-Key` derived from the slot or the
delivery, so a restarted runner or a re-delivered webhook cannot start a second run.

Boundaries under test:
- the cron parser: strictly-after semantics, steps/ranges/lists, the Vixie day-of-month OR
  day-of-week rule, timezone-aware slots, and definition errors that name the field;
- webhook signatures: HMAC-SHA256 over `{timestamp}.{body}`, a 5-minute replay window,
  constant-time comparison, and the delivery-derived idempotency key;
- the triggers file: every error names the trigger and the field; a webhook whose secret
  variable is unset refuses to START (closed at startup, like AGENTOS_POLICY / _AUTH_TOKENS);
- the runner: fires each due slot exactly once with the right key, body and principal,
  retries a failed post with bounded backoff inside the slot, never backfills missed slots;
- the API route `POST /triggers/webhooks/{name}`: 202 with the run, replay-safe, 401 on a
  missing/bad/stale signature, 404 unknown trigger, 413 over the cap, 415 non-JSON; it is
  exempt from the bearer middleware because it verifies its own credential, and it records
  a `system` principal `webhook:{name}` on `run.started`;
- when AGENTOS_TRIGGERS is unset the route does not exist (404) and OPEN_PATHS is unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from dagentos.api import auth as auth_mod
from dagentos.triggers import (
    CronTrigger,
    Runner,
    TriggerConfigError,
    TriggersConfig,
    WebhookTrigger,
    delivery_key,
    load_triggers,
    next_fire,
    sign,
    slot_key,
    verify,
)

IST = ZoneInfo("Asia/Kolkata")
SECRET = "whsec_test_0123456789abcdef"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ------------------------------------------------------------------------------------ cron
@pytest.mark.parametrize("expr,after,expected", [
    ("*/15 * * * *", utc(2026, 9, 28, 10, 7), utc(2026, 9, 28, 10, 15)),
    ("*/15 * * * *", utc(2026, 9, 28, 10, 15), utc(2026, 9, 28, 10, 30)),   # strictly after
    ("0 9 * * 1-5", utc(2026, 9, 26, 12, 0), utc(2026, 9, 28, 9, 0)),        # Sat -> Mon
    ("30 23 * * *", utc(2026, 9, 28, 23, 31), utc(2026, 9, 29, 23, 30)),
    ("0 0 1 * *", utc(2026, 9, 28, 0, 0), utc(2026, 10, 1, 0, 0)),
    ("0 0 29 2 *", utc(2026, 1, 1, 0, 0), utc(2028, 2, 29, 0, 0)),           # skips non-leap years
    ("5,35 8-10 * * *", utc(2026, 9, 28, 9, 35), utc(2026, 9, 28, 10, 5)),
    ("0 0 * * 0", utc(2026, 9, 28, 0, 0), utc(2026, 10, 4, 0, 0)),           # 0 = Sunday
    ("0 0 * * 7", utc(2026, 9, 28, 0, 0), utc(2026, 10, 4, 0, 0)),           # 7 = Sunday too
])
def test_next_fire_matches_cron_semantics(expr, after, expected):
    assert next_fire(expr, after) == expected


def test_next_fire_applies_the_vixie_or_rule_when_both_day_fields_are_restricted():
    # "0 0 13 * 5": the 13th of any month OR any Friday. After Mon 2026-09-28 the first
    # match is Fri 2026-10-02, before Tue 2026-10-13.
    assert next_fire("0 0 13 * 5", utc(2026, 9, 28)) == utc(2026, 10, 2)
    # Only day-of-month restricted: day-of-week `*` does not widen it.
    assert next_fire("0 0 13 * *", utc(2026, 9, 28)) == utc(2026, 10, 13)


def test_next_fire_is_timezone_aware_and_the_slot_key_carries_the_offset():
    after = datetime(2026, 9, 28, 20, 0, tzinfo=IST)            # 20:00 IST Monday
    nxt = next_fire("0 9 * * 1-5", after, tz=IST)
    assert nxt == datetime(2026, 9, 29, 9, 0, tzinfo=IST)
    assert nxt.utcoffset() == timedelta(hours=5, minutes=30)
    assert slot_key("nightly", nxt) == "cron:nightly:2026-09-29T09:00:00+05:30"


def test_next_fire_converts_a_utc_after_into_the_schedule_timezone():
    # 03:31 UTC = 09:01 IST, so the 09:00 IST slot is gone; the next is tomorrow's.
    nxt = next_fire("0 9 * * *", utc(2026, 9, 29, 3, 31), tz=IST)
    assert nxt == datetime(2026, 9, 30, 9, 0, tzinfo=IST)


@pytest.mark.parametrize("expr,field", [
    ("* * * *", "5 fields"), ("* * * * * *", "5 fields"),
    ("60 * * * *", "minute"), ("* 24 * * *", "hour"), ("* * 0 * *", "day-of-month"),
    ("* * 32 * *", "day-of-month"), ("* * * 13 *", "month"), ("* * * * 8", "day-of-week"),
    ("*/0 * * * *", "minute"), ("a * * * *", "minute"), ("5-3 * * * *", "minute"),
    ("* * 31 2 *", "never"),
])
def test_bad_cron_expressions_name_the_field(expr, field):
    with pytest.raises(TriggerConfigError, match=field):
        next_fire(expr, utc(2026, 1, 1))


# --------------------------------------------------------------------------------- webhook
def test_sign_and_verify_round_trip_and_reject_tampering():
    ts, body = 1_790_000_000, b'{"event":"push"}'
    sig = sign(SECRET, ts, body)
    assert sig.startswith("v1=") and len(sig) == 3 + 64
    expected = hmac.new(SECRET.encode(), f"{ts}.0..".encode() + body, hashlib.sha256).hexdigest()
    assert sig == f"v1={expected}"                                # no delivery: length 0, empty
    now = ts + 10
    assert verify(SECRET, str(ts), body, sig, now=now) is True
    assert verify("other-secret", str(ts), body, sig, now=now) is False
    assert verify(SECRET, str(ts), body + b" ", sig, now=now) is False
    assert verify(SECRET, str(ts + 1), body, sig, now=now) is False         # ts is signed too
    assert verify(SECRET, str(ts), body, "v2=" + expected, now=now) is False  # unknown scheme
    assert verify(SECRET, str(ts), body, expected, now=now) is False          # no scheme
    assert verify(SECRET, "soon", body, sig, now=now) is False                # malformed ts
    assert verify(SECRET, "", body, sig, now=now) is False
    assert verify(SECRET, str(ts), body, "", now=now) is False


def test_the_delivery_id_is_inside_the_signed_string():
    ts, body = 1_790_000_000, b"{}"
    with_id = sign(SECRET, ts, body, "d-1")
    assert with_id == "v1=" + hmac.new(SECRET.encode(), f"{ts}.3.d-1.".encode() + body,
                                       hashlib.sha256).hexdigest()
    assert with_id != sign(SECRET, ts, body, "d-2") != sign(SECRET, ts, body)
    assert verify(SECRET, str(ts), body, with_id, now=ts, delivery="d-1") is True
    assert verify(SECRET, str(ts), body, with_id, now=ts, delivery="d-2") is False
    assert verify(SECRET, str(ts), body, with_id, now=ts, delivery=None) is False
    # A delivery id containing the separator cannot collide with a shifted body.
    assert sign(SECRET, ts, b"x", "a.b") != sign(SECRET, ts, b"b.x", "a")


def test_verify_enforces_the_replay_window_both_ways():
    ts, body = 1_790_000_000, b"{}"
    sig = sign(SECRET, ts, body)
    assert verify(SECRET, str(ts), body, sig, now=ts + 299) is True
    assert verify(SECRET, str(ts), body, sig, now=ts + 301) is False
    assert verify(SECRET, str(ts), body, sig, now=ts - 301) is False         # from the future
    assert verify(SECRET, str(ts), body, sig, now=ts + 3600, tolerance=7200) is True


def test_delivery_key_prefers_the_delivery_id_and_falls_back_to_the_body_hash():
    assert delivery_key("gh", "d-123", b"x") == "webhook:gh:d-123"
    h = hashlib.sha256(b'{"a":1}').hexdigest()
    assert delivery_key("gh", None, b'{"a":1}') == f"webhook:gh:sha256:{h}"
    assert delivery_key("gh", "", b'{"a":1}') == f"webhook:gh:sha256:{h}"


# ---------------------------------------------------------------------------------- config
def _write(tmp_path, doc: dict):
    p = tmp_path / "triggers.json"
    p.write_text(json.dumps(doc))
    return p


GOOD = {"triggers": [
    {"name": "nightly", "kind": "cron", "schedule": "0 9 * * 1-5", "tz": "Asia/Kolkata",
     "workflow": "report", "inputs": {"topic": "yesterday"}},
    {"name": "gh", "kind": "webhook", "workflow": "review", "secret_env": "GH_WEBHOOK_SECRET"},
]}


def test_load_triggers_reads_both_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_WEBHOOK_SECRET", SECRET)
    cfg = load_triggers(_write(tmp_path, GOOD), env={"GH_WEBHOOK_SECRET": SECRET})
    assert isinstance(cfg, TriggersConfig)
    [cron] = cfg.crons
    assert isinstance(cron, CronTrigger)
    assert (cron.name, cron.schedule, cron.workflow, cron.inputs) == \
        ("nightly", "0 9 * * 1-5", "report", {"topic": "yesterday"})
    assert cron.tz == IST
    [wh] = cfg.webhooks.values()
    assert isinstance(wh, WebhookTrigger)
    assert (wh.name, wh.workflow, wh.secret) == ("gh", "review", SECRET)
    assert wh.max_body_bytes == 262_144
    assert cfg.sha256 == hashlib.sha256(_write(tmp_path, GOOD).read_bytes()).hexdigest()


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["triggers"][0].pop("schedule"), "nightly.*schedule"),
    (lambda d: d["triggers"][0].__setitem__("schedule", "99 * * * *"), "nightly.*minute"),
    (lambda d: d["triggers"][0].__setitem__("tz", "Mars/Olympus"), "nightly.*tz"),
    (lambda d: d["triggers"][0].__setitem__("kind", "heartbeat"), "nightly.*kind"),
    (lambda d: d["triggers"][0].pop("workflow"), "nightly.*workflow"),
    (lambda d: d["triggers"][0].__setitem__("inputs", ["x"]), "nightly.*inputs"),
    (lambda d: d["triggers"][1].pop("secret_env"), "gh.*secret_env"),
    (lambda d: d["triggers"][1].__setitem__("secret", "literal"), "gh.*secret_env.*never the value"),
    (lambda d: d["triggers"][1].__setitem__("name", "nightly"), "duplicate.*nightly"),
    (lambda d: d["triggers"][1].__setitem__("name", "has space"), "'has space'.*name must match"),
    (lambda d: d.__setitem__("triggers", {}), "triggers.*list"),
    (lambda d: d.__setitem__("version", 2), "version"),
])
def test_bad_triggers_file_names_the_trigger_and_field(tmp_path, mutate, needle):
    doc = json.loads(json.dumps(GOOD))
    mutate(doc)
    with pytest.raises(TriggerConfigError, match=needle):
        load_triggers(_write(tmp_path, doc), env={"GH_WEBHOOK_SECRET": SECRET})


def test_webhook_with_unset_secret_variable_refuses_to_load_naming_it(tmp_path):
    with pytest.raises(TriggerConfigError, match="GH_WEBHOOK_SECRET.*unset"):
        load_triggers(_write(tmp_path, GOOD), env={})
    with pytest.raises(TriggerConfigError, match="GH_WEBHOOK_SECRET.*16"):
        load_triggers(_write(tmp_path, GOOD), env={"GH_WEBHOOK_SECRET": "short"})


def test_missing_or_malformed_file_names_the_variable(tmp_path):
    with pytest.raises(TriggerConfigError, match="AGENTOS_TRIGGERS.*not found"):
        load_triggers(tmp_path / "nope.json", env={})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(TriggerConfigError, match="AGENTOS_TRIGGERS.*JSON"):
        load_triggers(bad, env={})


# ---------------------------------------------------------------------------------- runner
class FakePoster:
    """Stands in for the HTTP client: records every start, fails when told to."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next = 0

    def __call__(self, workflow: str, *, idempotency_key: str, inputs: dict,
                 principal: dict | None) -> dict:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("api down")
        self.calls.append({"workflow": workflow, "key": idempotency_key, "inputs": inputs,
                           "principal": principal})
        return {"id": f"run-{len(self.calls)}", "status": "pending"}


def _runner(tmp_path, poster, *, now: datetime, token: bool = False, sleeps=None):
    cfg = load_triggers(_write(tmp_path, GOOD), env={"GH_WEBHOOK_SECRET": SECRET})
    clock = {"now": now}
    slept: list[float] = sleeps if sleeps is not None else []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["now"] = clock["now"] + timedelta(seconds=s)

    r = Runner(cfg.crons, post=poster, now=lambda: clock["now"], sleep=sleep,
               principal=None if token else "asserted")
    return r, clock, slept


def test_runner_fires_each_slot_once_with_slot_key_inputs_and_principal(tmp_path):
    poster = FakePoster()
    start = datetime(2026, 9, 28, 8, 59, 30, tzinfo=IST)            # Monday, 30 s before 09:00
    r, clock, _ = _runner(tmp_path, poster, now=start)
    r.tick()                                                          # not due yet
    assert poster.calls == []
    clock["now"] = datetime(2026, 9, 28, 9, 0, 0, tzinfo=IST)
    r.tick()
    r.tick()                                                          # same slot: no second post
    [call] = poster.calls
    assert call["workflow"] == "report"
    assert call["key"] == "cron:nightly:2026-09-28T09:00:00+05:30"
    assert call["inputs"] == {"topic": "yesterday",
                              "trigger": {"kind": "cron", "name": "nightly",
                                          "slot": "2026-09-28T09:00:00+05:30",
                                          "schedule": "0 9 * * 1-5", "tz": "Asia/Kolkata"}}
    assert call["principal"] == {"kind": "system", "id": "cron:nightly",
                                 "attestation": "dagentos.triggers"}
    clock["now"] = datetime(2026, 9, 29, 9, 0, 0, tzinfo=IST)
    r.tick()
    assert [c["key"] for c in poster.calls][-1] == "cron:nightly:2026-09-29T09:00:00+05:30"


def test_runner_sends_no_body_principal_when_a_bearer_token_is_configured(tmp_path):
    poster = FakePoster()
    r, _clock, _ = _runner(tmp_path, poster, now=datetime(2026, 9, 28, 9, 0, tzinfo=IST), token=True)
    r.tick()
    assert poster.calls[0]["principal"] is None                       # the token names it


def test_runner_never_backfills_slots_missed_while_it_was_down(tmp_path):
    poster = FakePoster()
    # Started Wednesday 11:00: Monday's and Tuesday's 09:00 slots are in the past and stay unfired.
    r, _clock, _ = _runner(tmp_path, poster, now=datetime(2026, 9, 30, 11, 0, tzinfo=IST))
    r.tick()
    assert poster.calls == []
    assert r.next_fire_times() == {"nightly": datetime(2026, 10, 1, 9, 0, tzinfo=IST)}


def test_runner_retries_a_failed_post_with_bounded_backoff_inside_the_slot(tmp_path):
    poster = FakePoster()
    poster.fail_next = 2
    slept: list[float] = []
    r, _clock, _ = _runner(tmp_path, poster, now=datetime(2026, 9, 28, 9, 0, tzinfo=IST), sleeps=slept)
    r.tick()
    [call] = poster.calls                                             # third attempt landed
    assert call["key"] == "cron:nightly:2026-09-28T09:00:00+05:30"    # same key every attempt
    assert slept == [1.0, 2.0]                                        # 1, 2, 4, ... capped


def test_runner_gives_up_a_slot_at_the_retry_horizon_and_reports_it(tmp_path, caplog):
    """Self-review R2. Retrying until the NEXT slot meant a daily cron could hold the
    single-threaded runner for 24 h, starving every other trigger. The horizon is bounded
    (RETRY_HORIZON_SECONDS) and never past the next slot."""
    from dagentos.triggers.runner import RETRY_HORIZON_SECONDS
    poster = FakePoster()
    poster.fail_next = 10_000
    start = datetime(2026, 9, 28, 9, 0, tzinfo=IST)
    r, clock, slept = _runner(tmp_path, poster, now=start)
    r.tick()
    assert poster.calls == []
    assert slept[-1] == 60.0                                          # capped at 60 s per retry
    assert timedelta(seconds=RETRY_HORIZON_SECONDS - 60) <= clock["now"] - start \
        <= timedelta(seconds=RETRY_HORIZON_SECONDS)                   # gave up at the horizon
    assert RETRY_HORIZON_SECONDS <= 15 * 60                           # not "until tomorrow"
    assert r.next_fire_times() == {"nightly": datetime(2026, 9, 29, 9, 0, tzinfo=IST)}
    assert any("nightly" in rec.getMessage() and "gave up" in rec.getMessage()
               for rec in caplog.records)


def test_a_slot_whose_window_passed_during_a_stall_is_missed_not_fired_late(tmp_path, caplog):
    """Two triggers; the first stalls (API down) long enough that the second's slot AND the
    slot after it both pass. The second trigger must not fire two stale slots when the
    runner comes back — that is backfill through the back door. It skips to the next
    future slot and logs what it missed."""
    doc = {"triggers": [
        {"name": "slow", "kind": "cron", "schedule": "0 9 * * *", "tz": "UTC", "workflow": "a"},
        {"name": "fast", "kind": "cron", "schedule": "*/2 * * * *", "tz": "UTC", "workflow": "b"},
    ]}
    cfg = load_triggers(_write(tmp_path, doc), env={})
    poster = FakePoster()
    clock = {"now": utc(2026, 9, 28, 9, 0)}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["now"] = clock["now"] + timedelta(seconds=s)

    r = Runner(cfg.crons, post=poster, now=lambda: clock["now"], sleep=sleep)
    poster.fail_next = 10_000                                         # `slow` stalls to its horizon
    with caplog.at_level(logging.WARNING, logger="agentos.triggers"):
        r.tick()
    # `fast` had slots at 09:00, 09:02, 09:04 … while `slow` was retrying. At most one
    # `fast` run may have started (the slot current when the runner reached it); the
    # others are reported as missed, never fired.
    fast_keys = [c["key"] for c in poster.calls if c["workflow"] == "b"]
    assert len(fast_keys) <= 1
    assert any("fast" in rec.getMessage() and "missed" in rec.getMessage() for rec in caplog.records)
    assert r.next_fire_times()["fast"] > clock["now"]


def test_runner_dry_run_lists_the_next_fire_times(tmp_path, capsys):
    from dagentos.triggers.__main__ import main
    p = _write(tmp_path, GOOD)
    rc = main(["--triggers", str(p), "--dry-run", "--count", "2", "--now", "2026-09-28T09:00:00+05:30"],
              env={"GH_WEBHOOK_SECRET": SECRET})
    assert rc == 0
    out = capsys.readouterr().out
    assert "nightly" in out and "2026-09-29T09:00:00+05:30" in out and "2026-09-30T09:00:00+05:30" in out
    assert "gh" in out and "webhook" in out and "POST /triggers/webhooks/gh" in out


def test_the_shipped_example_file_loads_and_dry_runs(capsys):
    from pathlib import Path

    from dagentos.triggers.__main__ import main
    example = Path(__file__).resolve().parents[1] / "examples" / "triggers.json"
    cfg = load_triggers(example, env={"AGENTOS_WEBHOOK_SECRET_GH": SECRET})
    assert {c.name for c in cfg.crons} == {"nightly-haiku"} and set(cfg.webhooks) == {"gh"}
    assert main(["--triggers", str(example), "--dry-run"], env={"AGENTOS_WEBHOOK_SECRET_GH": SECRET}) == 0
    assert "nightly-haiku" in capsys.readouterr().out
    with pytest.raises(TriggerConfigError, match="AGENTOS_WEBHOOK_SECRET_GH.*unset"):
        load_triggers(example, env={})


# -------------------------------------------------------------------------------- API route
def _app(monkeypatch, tmp_path, *, triggers: dict | None = GOOD, mode: str = "asserted",
         tokens=None):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", mode)
    if tokens is None:
        monkeypatch.delenv("AGENTOS_AUTH_TOKENS", raising=False)
    else:
        monkeypatch.setenv("AGENTOS_AUTH_TOKENS", str(tokens))
    if triggers is None:
        monkeypatch.delenv("AGENTOS_TRIGGERS", raising=False)
    else:
        monkeypatch.setenv("AGENTOS_TRIGGERS", str(_write(tmp_path, triggers)))
        monkeypatch.setenv("GH_WEBHOOK_SECRET", SECRET)
    from dagentos.api import main
    importlib.reload(main)
    return main


def _define_review(c: TestClient) -> None:
    assert c.post("/agents", json={"name": "echoer", "type": "echo"}).status_code == 201
    assert c.post("/workflows", json={"name": "review",
                                      "nodes": [{"id": "r", "agent": "echoer"}]}).status_code == 201


def _signed(body: bytes, *, ts: int | None = None, secret: str = SECRET, delivery: str | None = "d-1",
            now: int | None = None) -> dict:
    ts = int(datetime.now(UTC).timestamp()) if ts is None else ts
    h = {"Content-Type": "application/json", "X-AgentOS-Timestamp": str(ts),
         "X-AgentOS-Signature": sign(secret, ts, body, delivery)}
    if delivery is not None:
        h["X-AgentOS-Delivery"] = delivery
    return h


def test_webhook_starts_a_run_replay_safe_with_a_system_principal(monkeypatch, tmp_path):
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)
    _define_review(c)
    body = json.dumps({"pull_request": {"number": 7}}).encode()
    r = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body))
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["workflow"] == "review" and run["request_id"] == "webhook:gh:d-1"

    again = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body))
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    assert len(c.get("/runs").json()["data"]) == 1

    started = c.get(f"/runs/{run['id']}/events").json()["data"][0]
    assert started["event_type"] == "run.started"
    assert started["principal"] == {"kind": "system", "id": "webhook:gh",
                                    "attestation": "hmac-sha256:v1"}
    # The delivery is the run's input, under the reserved `run` key for prompts.
    inputs = c.get(f"/blobs/{started['inputs_ref']['sha256']}").json()
    assert inputs["trigger"] == {"kind": "webhook", "name": "gh", "delivery": "d-1",
                                 "body": {"pull_request": {"number": 7}}}


def test_webhook_without_a_delivery_id_is_idempotent_on_the_body(monkeypatch, tmp_path):
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)
    _define_review(c)
    body = b'{"x":1}'
    a = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body, delivery=None))
    b = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body, delivery=None))
    assert a.status_code == 202 and a.json()["id"] == b.json()["id"]
    assert a.json()["request_id"] == f"webhook:gh:sha256:{hashlib.sha256(body).hexdigest()}"


def test_a_captured_signature_cannot_be_replayed_under_a_new_delivery_id(monkeypatch, tmp_path):
    """Self-review R1. The delivery id is the idempotency key; if it were outside the signed
    string, an attacker holding one valid request could replay it inside the window with a
    fresh X-AgentOS-Delivery each time and start a run per replay. The id is signed."""
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)
    _define_review(c)
    body = b'{"x":1}'
    good = _signed(body, delivery="d-1")
    assert c.post("/triggers/webhooks/gh", content=body, headers=good).status_code == 202
    replay = {**good, "X-AgentOS-Delivery": "d-2"}            # same ts + signature, new id
    r = c.post("/triggers/webhooks/gh", content=body, headers=replay)
    assert r.status_code == 401 and "signature" in r.json()["detail"]
    dropped = {k: v for k, v in good.items() if k != "X-AgentOS-Delivery"}   # id removed
    assert c.post("/triggers/webhooks/gh", content=body, headers=dropped).status_code == 401
    assert len(c.get("/runs").json()["data"]) == 1


@pytest.mark.parametrize("headers_fn,status,needle", [
    (lambda b: {"Content-Type": "application/json"}, 401, "signature"),
    (lambda b: {**_signed(b), "X-AgentOS-Signature": "v1=" + "0" * 64}, 401, "signature"),
    (lambda b: _signed(b, secret="wrong-secret-wrong-secret"), 401, "signature"),
    (lambda b: _signed(b, ts=int(datetime.now(UTC).timestamp()) - 600), 401, "timestamp"),
    (lambda b: {**_signed(b), "Content-Type": "text/plain"}, 415, "application/json"),
])
def test_webhook_refuses_bad_credentials_before_touching_the_engine(monkeypatch, tmp_path,
                                                                    headers_fn, status, needle):
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)
    _define_review(c)
    body = b'{"x":1}'
    r = c.post("/triggers/webhooks/gh", content=body, headers=headers_fn(body))
    assert r.status_code == status, r.text
    assert needle in r.json()["detail"]
    assert c.get("/runs").json()["data"] == []
    if status == 401:
        assert r.headers["www-authenticate"].startswith("AgentOS-Webhook")


def test_webhook_unknown_trigger_is_404_and_oversized_body_is_413(monkeypatch, tmp_path):
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)
    body = b'{"x":1}'
    assert c.post("/triggers/webhooks/nope", content=body, headers=_signed(body)).status_code == 404
    big = b'{"pad":"' + b"a" * 262_200 + b'"}'
    r = c.post("/triggers/webhooks/gh", content=big, headers=_signed(big))
    assert r.status_code == 413 and "262144" in r.json()["detail"]


def test_webhook_for_a_workflow_that_does_not_exist_is_404_after_auth(monkeypatch, tmp_path):
    main = _app(monkeypatch, tmp_path)
    c = TestClient(main.app)                                # no `review` workflow defined
    body = b'{"x":1}'
    r = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body))
    assert r.status_code == 404 and "review" in r.json()["detail"]


def test_webhook_needs_no_bearer_token_but_everything_else_still_does(monkeypatch, tmp_path):
    tokens = tmp_path / "tokens.json"
    tok = "human-token-human-token-1234"
    tokens.write_text(json.dumps({"principals": [
        {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "kind": "human", "id": "amit"}]}))
    main = _app(monkeypatch, tmp_path, mode="bearer", tokens=tokens)
    c = TestClient(main.app)
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/agents", json={"name": "echoer", "type": "echo"}, headers=h).status_code == 201
    assert c.post("/workflows", json={"name": "review", "nodes": [{"id": "r", "agent": "echoer"}]},
                  headers=h).status_code == 201
    body = b'{"x":1}'
    r = c.post("/triggers/webhooks/gh", content=body, headers=_signed(body))   # no bearer
    assert r.status_code == 202, r.text
    assert c.post("/triggers/webhooks/gh", content=body,
                  headers={"Content-Type": "application/json"}).status_code == 401
    assert c.get("/runs").status_code == 401                                  # unchanged elsewhere
    assert auth_mod.OPEN_PATHS == frozenset({"/health", "/ready", "/metrics"})
    assert auth_mod.SELF_AUTHENTICATED_PREFIXES == ("/triggers/webhooks/",)


def test_without_agentos_triggers_the_route_does_not_exist(monkeypatch, tmp_path):
    main = _app(monkeypatch, tmp_path, triggers=None)
    c = TestClient(main.app)
    body = b'{"x":1}'
    assert c.post("/triggers/webhooks/gh", content=body, headers=_signed(body)).status_code == 404
    assert "/triggers/webhooks/{name}" not in c.get("/openapi.json").json()["paths"]


def test_webhook_route_is_declared_self_authenticated_in_openapi(monkeypatch, tmp_path):
    """In bearer mode every operation carries the bearer requirement except the probes and
    the self-authenticated webhook, whose own headers are in its description."""
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"principals": [
        {"sha256": hashlib.sha256(b"human-token-human-token-1234").hexdigest(),
         "kind": "human", "id": "amit"}]}))
    main = _app(monkeypatch, tmp_path, mode="bearer", tokens=tokens)
    c = TestClient(main.app)
    assert c.get("/openapi.json").status_code == 401          # the document itself is protected
    paths = c.get("/openapi.json",
                  headers={"Authorization": "Bearer human-token-human-token-1234"}).json()["paths"]
    op = paths["/triggers/webhooks/{name}"]["post"]
    assert op["security"] == []
    assert "X-AgentOS-Signature" in json.dumps(op)
    assert "security" not in paths["/runs"]["get"]          # inherits the global bearer requirement


def test_bad_triggers_file_fails_api_startup_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    monkeypatch.setenv("AGENTOS_TRIGGERS", str(tmp_path / "missing.json"))
    from dagentos.api import main
    with pytest.raises(TriggerConfigError, match="AGENTOS_TRIGGERS"):
        importlib.reload(main)
    monkeypatch.delenv("AGENTOS_TRIGGERS")
    importlib.reload(main)                                  # leave the module healthy for others
