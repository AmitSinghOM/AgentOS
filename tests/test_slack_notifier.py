"""Slack approval notifier — an Observer that tells a channel a run is waiting on a human,
and later who decided. It carries NO authority: the message has a deep link to the run page,
never an approve button, never a token. Decisions still happen in the UI or API where the
principal is verified (Phase 8 #1); a Slack incoming webhook can verify nobody.

Boundaries under test:
- posts on `approval.requested` (kind effect and kind cost) with workflow, step, classes,
  reason, the run-page link, and a plain-text fallback; nothing else in the log posts;
- posts on `approval.granted` / `approval.rejected` naming the principal (kind and id) and
  the reason, so the channel sees the gate close;
- the payload never contains a bearer token, the webhook URL, or an approve/reject link;
- the HTTP call runs off the engine's thread and a failing/slow/raising transport never
  propagates: the engine's own observer guard is not even reached;
- the webhook URL is loaded from AGENTOS_SLACK_WEBHOOK only, must be an https Slack hooks
  URL (a wrong host is a ConfigError naming the variable), and is never in a log line;
- `build_observers` includes the notifier only when the variable is set; the resolver
  supplies the run when the observer did not witness `run.started` (worker process);
- `AGENTOS_UI_URL` builds the deep link (default http://127.0.0.1:8000/ui) with the run id
  URL-encoded.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime

import pytest

from dagentos.core.events import ApprovalGranted, ApprovalRejected, ApprovalRequested, RunStarted
from dagentos.core.models import (
    ApprovalKind,
    EffectClass,
    Principal,
    PrincipalKind,
    WorkflowRun,
)
from dagentos.notify.slack import ConfigError, SlackNotifier, notifier_from_env

HOOK = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX"
UI = "http://127.0.0.1:8000/ui"


class FakeTransport:
    """Records posts; can fail, raise, or block."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.status = 200
        self.raise_exc: Exception | None = None
        self.block = threading.Event()
        self.block.set()                                   # not blocking by default
        self.lock = threading.Lock()

    def __call__(self, url: str, payload: dict, timeout: float) -> int:
        self.block.wait(timeout=5)
        if self.raise_exc:
            raise self.raise_exc
        with self.lock:
            self.posts.append((url, payload))
        return self.status


def run(run_id: str = "run-1", workflow: str = "payments") -> WorkflowRun:
    return WorkflowRun(id=run_id, workflow=workflow)


def requested(run_id: str = "run-1", **over) -> ApprovalRequested:
    base = {"run_id": run_id, "seq": 5, "approval_id": "apr-1", "step_id": "pay",
            "effect_classes": [EffectClass.spend], "reason": "spend needs a human"}
    base.update(over)
    return ApprovalRequested(**base)


def notifier(transport: FakeTransport, **kw) -> SlackNotifier:
    n = SlackNotifier(webhook_url=HOOK, ui_url=UI, transport=transport, **kw)
    return n


def wait_for(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


# --------------------------------------------------------------------------- what is posted
def test_approval_requested_posts_the_gate_with_a_deep_link_and_no_authority():
    t = FakeTransport()
    n = notifier(t)
    n.observe(requested(), run())
    wait_for(lambda: len(t.posts) == 1)
    url, payload = t.posts[0]
    assert url == HOOK
    text = payload["text"]
    assert "payments" in text and "pay" in text and "spend" in text and "spend needs a human" in text
    assert f"{UI}/runs/run-1" in json.dumps(payload)
    blob = json.dumps(payload).lower()
    assert "approve" not in blob.replace("approval", "") and "reject" not in blob
    assert "bearer" not in blob and "hooks.slack.com" not in blob and "token" not in blob
    assert payload["blocks"][0]["type"] == "section"       # Block Kit body, text as fallback
    n.close()


def test_cost_ceiling_approval_says_so_with_the_numbers():
    t = FakeTransport()
    n = notifier(t)
    n.observe(requested(kind=ApprovalKind.cost, cost_at_request="12.50", proposed_ceiling="25",
                        effect_classes=[], reason="run cost hit the ceiling"), run())
    wait_for(lambda: len(t.posts) == 1)
    text = t.posts[0][1]["text"]
    assert "cost" in text.lower() and "12.50" in text and "25" in text
    n.close()


def test_decisions_post_who_decided_and_why():
    t = FakeTransport()
    n = notifier(t)
    amit = Principal(kind=PrincipalKind.human, id="amit", attestation="token:sha256:abcdef012345")
    n.observe(ApprovalGranted(run_id="run-1", seq=10, approval_id="apr-1", step_id="pay",
                              principal=amit, reason="within budget"), run())
    n.observe(ApprovalRejected(run_id="run-2", seq=7, approval_id="apr-9", step_id="wire",
                               principal=Principal(kind=PrincipalKind.system, id="expiry"),
                               reason="nobody decided in time"), run("run-2", "treasury"))
    wait_for(lambda: len(t.posts) == 2)
    texts = sorted(p["text"] for _, p in t.posts)
    granted = next(x for x in texts if "approved" in x.lower() or "granted" in x.lower())
    rejected = next(x for x in texts if "rejected" in x.lower())
    assert "human amit" in granted and "within budget" in granted and "pay" in granted
    assert "abcdef012345" not in granted                    # attestation stays out of chat
    assert "system expiry" in rejected and "nobody decided in time" in rejected and "treasury" in rejected
    n.close()


def test_other_events_post_nothing():
    t = FakeTransport()
    n = notifier(t)
    n.observe(RunStarted(run_id="run-1", seq=1, workflow="payments", workflow_version=1,
                         request_id="r"), run())
    n.close()                                               # drains the queue
    assert t.posts == []


def test_run_context_comes_from_the_resolver_when_the_observer_did_not_see_run_started():
    t = FakeTransport()
    n = notifier(t, resolve=lambda rid: run(rid, "resolved-wf"))
    n.observe(requested(), None)                            # worker process: run arrives as None
    wait_for(lambda: len(t.posts) == 1)
    assert "resolved-wf" in t.posts[0][1]["text"]
    n.close()


def test_unknown_run_still_posts_with_the_id_only():
    t = FakeTransport()
    n = notifier(t, resolve=lambda rid: None)
    n.observe(requested(), None)
    wait_for(lambda: len(t.posts) == 1)
    assert "run-1" in t.posts[0][1]["text"]
    n.close()


def test_run_id_is_url_encoded_in_the_deep_link():
    t = FakeTransport()
    n = notifier(t)
    n.observe(requested(run_id="a b/c"), run("a b/c"))
    wait_for(lambda: len(t.posts) == 1)
    assert f"{UI}/runs/a%20b%2Fc" in json.dumps(t.posts[0][1])
    n.close()


# ------------------------------------------------------------------------ never into engine
def test_observe_returns_immediately_even_when_slack_blocks():
    t = FakeTransport()
    t.block.clear()                                         # transport hangs
    n = notifier(t)
    t0 = time.monotonic()
    n.observe(requested(), run())
    assert time.monotonic() - t0 < 0.2                      # engine thread not held
    t.block.set()
    wait_for(lambda: len(t.posts) == 1)
    n.close()


def test_transport_failure_is_logged_not_raised(caplog):
    t = FakeTransport()
    t.raise_exc = ConnectionError("slack down")
    n = notifier(t)
    with caplog.at_level(logging.WARNING, logger="agentos.notify.slack"):
        n.observe(requested(), run())
        n.close()
    msgs = [r.getMessage() for r in caplog.records if r.name == "agentos.notify.slack"]
    assert any("slack down" in m and "apr-1" in m for m in msgs)
    assert not any(HOOK in m for m in msgs)                 # the URL is a credential


def test_non_2xx_from_slack_is_logged_with_the_status(caplog):
    t = FakeTransport()
    t.status = 404
    n = notifier(t)
    with caplog.at_level(logging.WARNING, logger="agentos.notify.slack"):
        n.observe(requested(), run())
        n.close()
    assert any("404" in r.getMessage() for r in caplog.records if r.name == "agentos.notify.slack")


def test_queue_is_bounded_and_drops_oldest_with_one_warning(caplog):
    t = FakeTransport()
    t.block.clear()
    n = notifier(t, max_pending=3)
    with caplog.at_level(logging.WARNING, logger="agentos.notify.slack"):
        for i in range(6):
            n.observe(requested(approval_id=f"apr-{i}"), run())
    t.block.set()
    n.close()
    ids = [json.dumps(p) for _, p in t.posts]
    assert len(t.posts) <= 4                                # 3 queued + at most 1 in flight
    assert any("dropp" in r.getMessage() for r in caplog.records if r.name == "agentos.notify.slack")
    assert any("apr-5" in x for x in ids)                   # newest kept (it is in the context block)


def test_close_drains_pending_posts_within_its_timeout():
    t = FakeTransport()
    n = notifier(t)
    for i in range(5):
        n.observe(requested(approval_id=f"apr-{i}"), run())
    n.close(timeout=3.0)
    assert len(t.posts) == 5


# -------------------------------------------------------------------------------- config
def test_notifier_from_env_reads_only_the_documented_variables():
    n = notifier_from_env({"AGENTOS_SLACK_WEBHOOK": HOOK, "AGENTOS_UI_URL": "https://ops.example/ui/",
                           "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/IGNORED"},
                          transport=FakeTransport())
    assert n is not None
    assert n.ui_url == "https://ops.example/ui"             # trailing slash stripped
    n.close()


def test_notifier_from_env_is_none_when_unset():
    assert notifier_from_env({}) is None
    assert notifier_from_env({"AGENTOS_SLACK_WEBHOOK": ""}) is None


@pytest.mark.parametrize("url,needle", [
    ("http://hooks.slack.com/services/T/B/SECRETPART", "https"),
    ("https://example.com/services/T/B/SECRETPART", "hooks.slack.com"),
    ("https://hooks.slack.com.evil.example/services/T/B/SECRETPART", "hooks.slack.com"),
    ("not a url", "https"),
    ("https://hooks.slack.com/", "services"),
])
def test_bad_webhook_url_is_a_config_error_naming_the_variable(url, needle):
    with pytest.raises(ConfigError) as exc:
        notifier_from_env({"AGENTOS_SLACK_WEBHOOK": url})
    assert "AGENTOS_SLACK_WEBHOOK" in str(exc.value) and needle in str(exc.value)
    assert "SECRETPART" not in str(exc.value)                # the path is the secret; never echoed


def test_build_observers_includes_the_notifier_only_when_configured(monkeypatch):
    from dagentos.observability import build_observers
    monkeypatch.delenv("AGENTOS_SLACK_WEBHOOK", raising=False)
    obs, _ = build_observers()
    assert not any(type(o).__name__ == "SlackNotifier" for o in obs)
    monkeypatch.setenv("AGENTOS_SLACK_WEBHOOK", HOOK)
    obs, _ = build_observers()
    [n] = [o for o in obs if type(o).__name__ == "SlackNotifier"]
    n.close()


def test_repr_and_describe_never_reveal_the_url():
    t = FakeTransport()
    n = notifier(t)
    assert HOOK not in repr(n) and HOOK not in str(n.describe())
    assert n.describe()["webhook"] == "set" and n.describe()["ui_url"] == UI
    n.close()


def test_occurred_at_is_what_the_message_shows_not_wall_clock():
    """Observers derive everything from the event (A9), so a replayed log posts the same text."""
    t = FakeTransport()
    n = notifier(t)
    when = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    n.observe(requested(occurred_at=when), run())
    wait_for(lambda: len(t.posts) == 1)
    assert "2026-09-28T20:00:00" in json.dumps(t.posts[0][1])
    n.close()


def test_through_the_engine_with_the_store_resolver_names_the_workflow():
    """The engine hands observers no run (`_notify(observers, ev)`), so the workflow name
    can only come from the resolver `build_observers` wires; without it every notice would
    read '(unknown workflow)'. Both notices, suspend and grant, through a real Engine."""
    from dagentos.core.engine import Engine
    from dagentos.core.models import Agent, AgentType, WorkflowDefinition
    from dagentos.observability import store_resolver
    from dagentos.store.memory import MemoryStore
    from tests.test_approvals import RecordingExecutor

    t = FakeTransport()
    store = MemoryStore()
    n = SlackNotifier(webhook_url=HOOK, ui_url=UI, transport=t, resolve=store_resolver(store))
    store.put_agent(Agent(name="payer", type=AgentType.echo,
                          declared_effects=[EffectClass.compute, EffectClass.spend]))
    store.put_workflow(WorkflowDefinition(name="payments", nodes=[{"id": "pay", "agent": "payer"}]))
    eng = Engine(store=store, blobs=store, executors={"echo": RecordingExecutor()}, observers=[n])
    r = eng.start_run("payments")
    eng.approve(r.id, next(iter(r.approvals)),
                principal=Principal(kind=PrincipalKind.human, id="amit"), reason="ok")
    n.close()
    texts = [p["text"] for _, p in t.posts]
    assert len(texts) == 2 and all("payments" in x for x in texts)
    assert "(unknown workflow)" not in json.dumps(t.posts)
    assert f"{UI}/runs/{r.id}" in texts[0] and "approved by human amit" in texts[1]
