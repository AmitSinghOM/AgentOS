# Triggers — cron and webhooks that start runs

Runs start with `POST /workflows/{name}/runs`. Triggers are the two ways that call is made
without a person at a terminal, and both go **through the same API** — no second path into
the engine, the same auth, the same operator policy ceiling, the same log.

| trigger | who calls the API | idempotency key | principal on `run.started` |
|---|---|---|---|
| cron | `python -m dagentos.triggers` (a client process) | `cron:{name}:{slot ISO with offset}` | asserted mode: `system` / `cron:{name}`; bearer mode: the runner's token |
| webhook | the sender, hitting `POST /triggers/webhooks/{name}` on the API | `webhook:{name}:{X-AgentOS-Delivery}`, else `webhook:{name}:sha256:{body}` | `system` / `webhook:{name}`, attestation `hmac-sha256:v1` |

The key is the whole story of "exactly once": a restarted runner, a duplicated runner, a
retried post, or a sender that redelivers all land on the same run, because run start is
idempotent on `Idempotency-Key` (DESIGN §6; `tests/test_api_durability.py`).

## The triggers file

```json
{"triggers": [
  {"name": "nightly", "kind": "cron", "schedule": "0 9 * * 1-5", "tz": "Asia/Kolkata",
   "workflow": "report", "inputs": {"topic": "yesterday"}},
  {"name": "gh", "kind": "webhook", "workflow": "review",
   "secret_env": "GH_WEBHOOK_SECRET", "max_body_bytes": 262144}
]}
```

`AGENTOS_TRIGGERS=<path>` names it for both processes. Every load error names the trigger
and the field. Two things are refused at **startup**, not at request time: a webhook whose
`secret_env` variable is unset or shorter than 16 characters, and a secret *value* in the
file (`"secret": …` is an unknown key). A misconfigured deployment therefore never mounts a
route nobody can sign for — the same closed-at-startup shape as `AGENTOS_AUTH_TOKENS` and
`AGENTOS_POLICY`.

Cron is five fields (`minute hour day-of-month month day-of-week`): `*`, numbers, `a-b`,
`a,b`, `*/n`, `a-b/n`; day-of-week 0–7 with 0 and 7 both Sunday; when both day fields are
restricted, a day matches if either does (the Vixie rule). No names, no `@daily`. `tz` is an
IANA zone, default `UTC`; the slot key carries the offset so a schedule survives DST edges
without two runs or none. A schedule that can never fire (`0 0 31 2 *`) is a load error.

Inputs reach every step under the reserved `run` key, plus a `trigger` object the adapter
adds (`inputs` may not set `trigger` itself):

```
cron:     {"topic": "yesterday", "trigger": {"kind": "cron", "name": "nightly",
           "slot": "2026-09-29T09:00:00+05:30", "schedule": "0 9 * * 1-5", "tz": "Asia/Kolkata"}}
webhook:  {"trigger": {"kind": "webhook", "name": "gh", "delivery": "d-1", "body": {…the JSON body…}}}
```

The webhook body is **data** to the workflow's prompts (C12 delimiting applies), never
instructions.

## Cron runner

```bash
AGENTOS_TRIGGERS=triggers.json python -m dagentos.triggers --dry-run      # next fire times; contacts nothing
AGENTOS_TRIGGERS=triggers.json python -m dagentos.triggers                # runs; SIGTERM stops after the tick
```

| variable | default | meaning |
|---|---|---|
| `AGENTOS_API_URL` | `http://127.0.0.1:8000` | the API |
| `AGENTOS_API_TOKEN` | unset | bearer token when the API runs `AGENTOS_AUTH=bearer` (put the runner in the token file as a `system` principal); unset → asserted mode and the runner sends `system`/`cron:{name}` in the body |

Semantics, each pinned in `tests/test_triggers.py`:

- A slot fires **once**, at or after its minute. A runner started inside the slot's minute
  fires it (a cron daemon would); one started later does not.
- **No backfill.** Slots missed while the runner was down stay unfired — a scheduler whose
  runs can spend money must not surprise anyone with three catch-up runs at 11:00. Start a
  missed run by hand with the same key if you want it.
- A failed post is retried with bounded backoff (1, 2, 4 … 60 s) **inside its slot**, then
  given up with one ERROR line naming the slot; it is never fired late into the next slot.
- `404` from the API (workflow not defined) is reported, not retried forever.

## Webhook route

Mounted by the API only when `AGENTOS_TRIGGERS` is set: one `POST /triggers/webhooks/{name}`
per webhook trigger. The sender signs like Stripe / Slack:

```
X-AgentOS-Timestamp: 1790000000
X-AgentOS-Signature: v1=<hex HMAC-SHA256(secret, "1790000000." + raw body)>
X-AgentOS-Delivery:  <sender's delivery id>          # optional but recommended
Content-Type: application/json
```

Checks, in order, all before the engine is touched: 404 unknown trigger → 413 over
`max_body_bytes` (default 256 KiB, inside the global `AGENTOS_MAX_BODY_BYTES`) → 415 not
JSON → 401 missing / stale (±300 s) / invalid signature, with
`WWW-Authenticate: AgentOS-Webhook …`. Then 202 with the run, or 404 if the workflow the
trigger names is not defined.

The route is listed in `dagentos.api.auth.SELF_AUTHENTICATED_PREFIXES` and is the only
thing there: in bearer mode the middleware lets it through **to its own verifier**, because
the credential is the HMAC, not a token. It is not open — `OPEN_PATHS` is still exactly the
probes and metrics. The OpenAPI document marks it `security: []` and its description carries
the header names.

Signing from Python (what a sender does):

```python
import hmac, hashlib, time, json, httpx
body = json.dumps({"pull_request": {"number": 7}}).encode()
ts = int(time.time())
sig = "v1=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
httpx.post("http://127.0.0.1:8000/triggers/webhooks/gh", content=body,
           headers={"Content-Type": "application/json", "X-AgentOS-Timestamp": str(ts),
                    "X-AgentOS-Signature": sig, "X-AgentOS-Delivery": "d-1"})
```

## Not here, on purpose

- A heartbeat / poll-until-condition trigger: that is a workflow with a `read` step and a
  cron, not a new trigger kind.
- GitHub's / Slack's native signature headers: a shim that re-signs into this scheme is a
  ten-line proxy; the API speaks one scheme so there is one verifier to audit.
- Catch-up runs, cron names or `@aliases`: see above.
