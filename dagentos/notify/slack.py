"""Slack approval notifier — an `Observer` that posts to a Slack incoming webhook when a run
suspends for a human (`approval.requested`) and when the gate closes (`approval.granted` /
`approval.rejected`).

What it deliberately is not:

- **Not an approval surface.** The message carries a deep link to the run page and nothing
  else that acts. No approve/reject buttons, no token, no API URL that decides anything. A
  Slack incoming webhook can verify nobody; decisions stay in the UI or API, where the bearer
  token names the principal (Phase 8 #1). A Slack *app* with interactivity and signed
  requests could carry the decision one day; that is a different adapter with its own trust
  boundary and it is not this one.
- **Not on the engine's path.** `observe()` enqueues and returns; one daemon thread posts.
  The engine's observer guard (`engine.py:_notify`) already swallows exceptions, but a
  blocking HTTP call inside `observe()` would still stall every append — so the call never
  happens on the caller's thread. The queue is bounded; on overflow the OLDEST pending
  notification is dropped with a warning, because the newest is the one nobody has seen.
- **Not a source of state.** Every field in the message comes from the event (and the fold
  the resolver returns for context), including the timestamp, so a replayed log posts the
  same text (§11 A9).

Configuration:

    AGENTOS_SLACK_WEBHOOK   https://hooks.slack.com/services/…   opt-in; unset → no notifier
    AGENTOS_UI_URL          http://127.0.0.1:8000/ui             base of the run-page deep link

The webhook URL is a credential: it is validated (https, host exactly hooks.slack.com,
`/services/` path), never logged, and `describe()` reports only "set".
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from urllib.parse import quote, urlsplit

from dagentos.core.events import ApprovalGranted, ApprovalRejected, ApprovalRequested, Event
from dagentos.core.models import ApprovalKind, Principal, WorkflowRun

log = logging.getLogger("agentos.notify.slack")

SLACK_HOST = "hooks.slack.com"
DEFAULT_UI_URL = "http://127.0.0.1:8000/ui"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_PENDING = 100

Transport = Callable[[str, dict, float], int]        # (url, payload, timeout) -> status
Resolver = Callable[[str], object]


class ConfigError(ValueError):
    """Bad notifier configuration, naming the variable to fix."""


def _httpx_transport(url: str, payload: dict, timeout: float) -> int:
    import httpx
    return httpx.post(url, json=payload, timeout=timeout).status_code


class SlackNotifier:
    def __init__(self, *, webhook_url: str, ui_url: str = DEFAULT_UI_URL,
                 transport: Transport | None = None, resolve: Resolver | None = None,
                 timeout: float = DEFAULT_TIMEOUT, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._url = webhook_url
        self.ui_url = ui_url.rstrip("/")
        self._transport = transport or _httpx_transport
        self._resolve = resolve
        self._timeout = timeout
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, max_pending))
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None      # started on the first enqueue
        self._lock = threading.Lock()

    def _ensure_pump(self) -> None:
        """Start the posting thread lazily: an API process that reloads the module in tests,
        or a worker that never sees an approval, owns no idle thread (self-review R5)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._pump, name="agentos-slack-notifier",
                                                daemon=True)
                self._thread.start()

    # -- the port -----------------------------------------------------------------------
    def observe(self, event: Event, run: WorkflowRun | None = None) -> None:
        """Enqueue a message for the three approval events; ignore everything else. Returns
        immediately; never raises."""
        try:
            payload = self._payload(event, run)
        except Exception:  # a rendering bug must not reach the engine
            log.exception("slack notifier could not render %s seq=%s", type(event).event_type, event.seq)
            return
        if payload is None:
            return
        item = (payload, f"{type(event).event_type} {getattr(event, 'approval_id', '')} run={event.run_id}")
        self._ensure_pump()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                log.warning("slack notifier queue full (%d); dropping oldest: %s",
                            self._queue.maxsize, dropped[1])
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                log.warning("slack notifier queue still full; dropping %s", item[1])

    def close(self, timeout: float = 10.0) -> None:
        """Drain pending posts (bounded) and stop the thread. Safe to call more than once and
        when nothing was ever enqueued."""
        self._closed.set()
        t = self._thread
        if t is None or not t.is_alive():
            return
        self._queue.put((None, "stop"))
        t.join(timeout=timeout)

    def describe(self) -> dict:
        return {"webhook": "set", "ui_url": self.ui_url, "pending": self._queue.qsize(),
                "thread": bool(self._thread and self._thread.is_alive())}

    def __repr__(self) -> str:
        return f"SlackNotifier(webhook=set, ui_url={self.ui_url!r})"

    # -- internals ----------------------------------------------------------------------
    def _pump(self) -> None:
        while True:
            payload, label = self._queue.get()
            if payload is None:
                return
            try:
                status = self._transport(self._url, payload, self._timeout)
                if not (200 <= int(status) < 300):
                    log.warning("slack notifier: webhook answered %s for %s", status, label)
            except Exception as exc:  # noqa: BLE001 — telemetry never breaks anything
                log.warning("slack notifier: post failed for %s: %s", label, exc)

    def _run_for(self, event: Event, run: WorkflowRun | None) -> WorkflowRun | None:
        if run is not None:
            return run
        if self._resolve is None:
            return None
        try:
            got = self._resolve(event.run_id)
        except Exception:  # noqa: BLE001
            return None
        return got if isinstance(got, WorkflowRun) else None

    def _payload(self, event: Event, run: WorkflowRun | None) -> dict | None:
        if not isinstance(event, ApprovalRequested | ApprovalGranted | ApprovalRejected):
            return None
        run = self._run_for(event, run)
        wf = run.workflow if run is not None else "(unknown workflow)"
        link = f"{self.ui_url}/runs/{quote(event.run_id, safe='')}"
        when = event.occurred_at.isoformat(timespec="seconds")
        if isinstance(event, ApprovalRequested):
            if event.kind is ApprovalKind.cost:
                head = (f"Run *{wf}* is waiting on a human: step `{event.step_id}` would take the run's "
                        f"cost to {event.cost_at_request} against a ceiling; approving raises the ceiling "
                        f"to {event.proposed_ceiling}.")
            else:
                classes = ", ".join(c.value for c in event.effect_classes) or "an effect"
                head = (f"Run *{wf}* is waiting on a human: step `{event.step_id}` declares "
                        f"`{classes}`.")
            reason = f" Reason: {event.reason}" if event.reason else ""
            expiry = f" Expires {event.expires_at.isoformat(timespec='seconds')}." \
                if event.expires_at else ""
            text = f"{head}{reason}{expiry} Decide in the operator UI: {link}"
        else:
            verb = "approved" if isinstance(event, ApprovalGranted) else "rejected"
            who = _who(event.principal)
            reason = f" — {event.reason}" if event.reason else ""
            text = f"Step `{event.step_id}` of run *{wf}* was {verb} by {who}{reason}. {link}"
        # Block Kit body with `text` as the notification fallback; identical content.
        return {
            "text": _plain(text),
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": f"run `{event.run_id}` · approval `{event.approval_id}` "
                                               f"· seq {event.seq} · {when}"}]},
            ],
        }


def _who(p: Principal | None) -> str:
    if p is None:
        return "an unrecorded principal"
    return f"{p.kind.value} {p.id}"                     # attestation stays out of chat


def _plain(mrkdwn: str) -> str:
    return mrkdwn.replace("*", "").replace("`", "")


def validate_webhook_url(url: str, *, var: str = "AGENTOS_SLACK_WEBHOOK") -> str:
    """https, host exactly hooks.slack.com, a /services/ path. Errors never echo the path
    (it is the secret part)."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ConfigError(f"{var} must be an https URL (https://hooks.slack.com/services/...)")
    if parts.hostname != SLACK_HOST:
        raise ConfigError(f"{var} host must be exactly {SLACK_HOST}, got {parts.hostname!r}")
    if not parts.path.startswith("/services/") or parts.path.count("/") < 4:
        raise ConfigError(f"{var} must be a Slack incoming-webhook URL under /services/T…/B…/…")
    return url.strip()


def notifier_from_env(env: dict[str, str] | None = None, *, transport: Transport | None = None,
                      resolve: Resolver | None = None) -> SlackNotifier | None:
    import os
    e = os.environ if env is None else env
    raw = (e.get("AGENTOS_SLACK_WEBHOOK") or "").strip()
    if not raw:
        return None
    url = validate_webhook_url(raw)
    ui = (e.get("AGENTOS_UI_URL") or DEFAULT_UI_URL).strip()
    return SlackNotifier(webhook_url=url, ui_url=ui, transport=transport, resolve=resolve)


__all__: list[str] = ["ConfigError", "SlackNotifier", "notifier_from_env", "validate_webhook_url"]
