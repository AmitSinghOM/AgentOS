# Notifications — the Slack approval notifier

When a run suspends for a human, somebody has to find out. The operator UI's inbox is where
the decision is made; the notifier is how the person gets *told* — in the channel they
already watch — with a link back to the inbox.

```bash
AGENTOS_SLACK_WEBHOOK=https://hooks.slack.com/services/T…/B…/…   # opt-in; unset → nothing
AGENTOS_UI_URL=https://agentos.example/ui                        # default http://127.0.0.1:8000/ui
```

Set on the **worker** (it appends `approval.requested`) and, if you want decision notices
too, on the **API** (it appends `approval.granted` / `rejected`). Each process posts what it
appends; nothing is posted twice.

## What gets posted

| event | message |
|---|---|
| `approval.requested` (effect) | *Run **payments** is waiting on a human: step `pay` declares `spend`. Reason: … Expires … Decide in the operator UI: `<link>`* |
| `approval.requested` (cost) | *… step `pay` would take the run's cost to 12.50 against a ceiling; approving raises the ceiling to 25 …* |
| `approval.granted` | *Step `pay` of run **payments** was approved by human amit — within budget. `<link>`* |
| `approval.rejected` | *Step `wire` of run **treasury** was rejected by system expiry — nobody decided in time. `<link>`* |

Each is a Block Kit `section` with a `context` line (`run · approval · seq · occurred_at`) and
the same content in `text` as the notification fallback. Every field comes from the event
and the run fold, including the timestamp, so a replayed log posts identical text
(`DEVELOPMENT_STRUCTURE.md` §11 A9). The principal's `attestation` (the token digest) stays
out of chat.

## What is deliberately not there

**No approve or reject in Slack.** The message carries a link to `/ui/runs/{id}`, and
nothing else that acts. A Slack incoming webhook is one-way: it can post, it cannot tell
AgentOS who clicked. Putting a decision URL in the message would mean either an
unauthenticated decision path (which Phase 8 #1 exists to forbid) or a bearer token in a
chat message (a credential in a place with its own retention and search). The decision
happens where the principal is verified. A Slack *app* with signed interactive requests
could carry the decision honestly one day; that would be its own adapter with its own trust
boundary, and it is not this one.

**Not on the engine's path.** `observe()` puts the message on a bounded queue and returns;
one daemon thread posts with a 5 s timeout. Slack being down, slow, or returning 404 is a
WARNING that names the approval, never the URL (the URL is the credential), and never an
exception the engine sees. On overflow (default 100 pending) the **oldest** notice is
dropped with a warning — the newest is the one nobody has seen yet. The worker drains the
queue on clean shutdown so the last delivery's notice survives the SIGTERM that ended it.

**Not a second inbox.** Nothing is stored; if Slack was down, the inbox still shows the gate.

## Configuration checks

`AGENTOS_SLACK_WEBHOOK` must be `https://hooks.slack.com/services/…`. Anything else (http,
another host, a look-alike host) is a `ConfigError` naming the variable at startup — the
process refuses to start, the same closed-at-startup shape as `AGENTOS_AUTH_TOKENS`. The
error never echoes the path (the secret part). `GET /executors`-style introspection reports
`webhook: "set"`, never the value.

Pinned in `tests/test_slack_notifier.py`; fail modes in [`FAIL_MODES.md`](FAIL_MODES.md).
