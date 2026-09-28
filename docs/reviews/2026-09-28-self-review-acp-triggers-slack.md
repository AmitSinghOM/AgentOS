# Self-review — ACP provider, triggers, Slack notifier (PRs #105, #106, #107)

Date: 2026-09-28. Range: `f9364d6..slack-notifier-2026-09-28` (three features, three fix
commits). Process: code-reviewer skill — deterministic manifest (35 files, 19 bundles, two
SENSITIVE: `dagentos/api/{auth,main,triggers}.py` and `dagentos/triggers/*`), plan-first on
the sensitive and protocol bundles, verbatim anchors, reflection, then the closing gate.
Every accepted fix was written red-first and proven to discriminate against the pre-fix code.

## Result

| # | Sev | Where | Finding | Fix | Discrimination |
|---|---|---|---|---|---|
| R1 | medium / security | `dagentos/triggers/webhook.py`, `api/triggers.py` | `X-AgentOS-Delivery` is the run's idempotency key but was **outside** the signed string — one captured valid request, replayed inside the 300 s window with a fresh id each time, started a run per replay. The module's own docstring claimed in-window replay was "harmless". | Signed string is `{ts}.{len(delivery)}.{delivery}.{body}` (`42a2cfe`). The length prefix was forced by the lock test: the first cut `{ts}.{delivery}.{body}` collided on `"a.b"+"x" == "a"+"b.x"`. | Replay under a new id → 401; id dropped → 401; one run total. Both fail on the old code. |
| R2 | medium / bug | `dagentos/triggers/runner.py` | `_fire` retried until the trigger's **next** slot. A daily cron with the API down held the single-threaded runner for 24 h (1,440 attempts observed on the old code), starving every other trigger; on give-up the starved triggers' stale slots fired in a burst — backfill by another name. | `RETRY_HORIZON_SECONDS = 600`, never past the next slot; `tick()` fires a slot only while it is the current one and logs skipped slots as *missed* (`42a2cfe`). | Horizon test and stall test both fail on the old runner. |
| R4 | medium / bug | `providers/acp/…/executor.py` | `ran_classes` excluded every `status: failed` tool call, so a half-applied edit under a step declared `[compute, read]` passed `_settle`. Only a permission **rejection** means the tool never ran. | Exclude rejected only (`feceda7`), with a `tool_failed` fake-agent scenario. | `test_a_tool_that_ran_and_failed_is_still_an_effect` red before, green after. |
| R5 | low / maintainability | `dagentos/notify/slack.py`, `observability/__init__.py` | The notifier started its daemon thread in `__init__`; the API composes observers at import and tests `reload(main)` repeatedly (thread leak), and the API had no drain for a queued decision notice at shutdown. | Lazy pump (starts on first enqueue), `close()` safe-before-use and idempotent, `atexit` drain registered in `build_observers` (`2008203`). | Idle-notifier thread-count test. |
| — | low / docs | `docs/FAIL_MODES.md` | Pre-existing: preamble said 61 rows, table had 65; nothing pinned it. | True count (79 after +14) and `test_the_stated_row_count_matches_the_table` (`2008203`). | — |

Also tightened while reading: the ACP handshake test asserted C12 delimiting with
`"data" in text.lower()`, which matched the boundary sentence itself — now asserts the real
`<input>`/`</input>` tags (`feceda7`).

## Withdrawn — kept visible so the false positive is on record

| # | Where | Claim | Why withdrawn |
|---|---|---|---|
| R3 | `providers/acp/…/protocol.py` `call()` | On a queue timeout, `returncode is not None` raises "exited during …" even if the agent's final response line is still in the reader's pipe. | Probed 20× with the consumer stalled until after the process exit: the queue was never empty. The reader is a tight loop that queues every parsed line before `_EOF`, and the pipe flush precedes exit; the `_EOF` branch delivers the response. The code change was reverted; the scenario stays as a **guarantee test** (`answer_then_exit`) so a future reader reorder is caught. |

## Declined with evidence

| Candidate | Where | Evidence |
|---|---|---|
| `RunStartBody.principal` lets any asserted-mode client claim a `human` started the run | `api/main.py` | Same as every other body principal in asserted mode, which the startup WARNING and `TRUST_BOUNDARY.md` §1 already document as unverified; bearer mode 422s it via `principal_for` like the decision routes. Consistent, not new. |
| Webhook 415 for a JSON parse failure "should be 400" | `api/triggers.py` | Content-Type was `application/json` but the body is not JSON: unsupported media in substance; the detail says which. Style, not defect. |
| `_limit_body` 413 fires before the per-trigger cap | `api/main.py` | Correct layering: the global cap is the outer bound; the trigger cap can only be tighter. Documented in `docs/triggers.md`. |
| Health cache hides a freshly broken agent for 60 s | `acp/executor.py` | Deliberate: `GET /executors` is polled per request and a kiro-cli spawn is ~5 s. The TTL is a constant and the cache stores failures too, so a broken agent is reported within 60 s. |
| `SELF_AUTHENTICATED_PREFIXES` widens the middleware exemption by prefix | `api/auth.py` | Exactly one entry; the only route under it verifies HMAC and fails closed; `test_open_paths_are_exactly_the_probes_and_metrics` still pins `OPEN_PATHS`; emptying the tuple fails two tests. |
| Slack notifier drops the oldest on overflow — should drop newest | `notify/slack.py` | The newest is the one nobody has seen; the oldest is most likely already visible in the inbox. Stated in the module docstring. |

## Security sweep (exposure rule)

Touching API routes put the whole listener in scope. Fixed in files the PRs edit: README and
RELEASING `docker run -p 8000:8000` → `-p 127.0.0.1:8000:8000`. **Not fixed, flagged:**
`docker-compose.yml` publishes Postgres `5432:5432` (password `agentos`), Jaeger, Prometheus
and Grafana on all interfaces — one-line `127.0.0.1:` prefixes; a separate tiny PR. The live
listeners used for verification showed `TCP 127.0.0.1:8011 (LISTEN)`.

## Closing gate (slack branch tip `2008203`)

pytest 704 passed / 4 skipped (chaos excluded locally, as always); `ruff check .` clean at
CI's version; `lint-imports` 3 kept, 0 broken. Provider suite 42 passed / 1 skipped (live
kiro-cli health behind `AGENTOS_ACP_LIVE`). Live checks this session: kiro-cli 2.24.1 ACP
handshake and one compute turn; a signed webhook → 202 / redelivery same run / unsigned 401
on a loopback uvicorn.

## Not done

- Mid-step suspension for ACP agents (`session/load`); a sandbox for the agent process.
- A Slack *app* adapter with signed interactive requests that could carry a decision honestly.
- The compose port publishes above.
