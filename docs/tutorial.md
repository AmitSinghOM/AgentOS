# Tutorial: your first hour with AgentOS

One run, end to end, in about ten minutes of typing: define a workflow whose one step
declares it will **spend**, start it, watch the run suspend *before* that step, decide as a
named human, then read the log that proves what happened and rewind it to the moment it
stopped. Everything below runs locally with no model and no API key (the agents are `echo`
agents). The same walk is executed as a test on every commit:
`pytest tests/test_tutorial.py`.

If you would rather start with a real model, [`quickstart-llm.md`](quickstart-llm.md) does
that; come back here for the approval gate, which that page does not cover.

## 0. What you will see

```
      a ──► b ──┐
      │         ├──► d
      └──► pay ─┘        pay declares "spend" → the run suspends here until a human decides
```

The "diamond" workflow. `a` runs, then `b` and `pay` may run in parallel, then `d` needs both.
The agent behind `pay` declares the `spend` effect class, which every workflow budget gates by
default, so the engine asks for an approval *before* `pay` is dispatched. `b` is not gated and
still runs. `d` waits.

## 1. Run the API and the worker

```bash
pip install -e ".[dev]"                       # or: pip install dagentos
uvicorn dagentos.api.main:app                 # terminal 1 — API on :8000, SQLite file ./agentos.db
python -m dagentos.worker                     # terminal 2 — the worker that advances runs
```

The API prints three warnings at startup. They are right: `AGENTOS_AUTH=asserted` means the
principal you send is recorded exactly as typed and nothing checks it; no operator policy; no
signing keys. That is the correct setting for this tutorial on your own machine and the wrong
one for anything anyone else can reach — [`TRUST_BOUNDARY.md`](TRUST_BOUNDARY.md) says what to
set before that.

## 2. Define two agents and the workflow

Three files in `examples/` are the whole definition. `payer` is the one that matters:

```json
{"name": "payer", "type": "echo", "declared_effects": ["compute", "spend"], "config": {"message": "paid"}}
```

An agent **declares** its effect classes up front; the engine gates on the declaration, and an
executor that later reports an effect outside its declaration is dead-lettered. Register them:

```bash
curl -X POST localhost:8000/agents    -H 'Content-Type: application/json' -d @examples/calc_agent.json
curl -X POST localhost:8000/agents    -H 'Content-Type: application/json' -d @examples/payer_agent.json
curl -X POST localhost:8000/workflows -H 'Content-Type: application/json' -d @examples/diamond_workflow.json
```

Each answers `201` with the stored record (the `Content-Type` header is required; `curl -d`
sends form-encoding without it and the API answers `422`).

## 3. Start a run and watch it suspend

```bash
curl -X POST localhost:8000/workflows/diamond/runs -H 'Idempotency-Key: tutorial-1'
```

`202` and a run id. Keep it: `RUN=<the id>`. Repeating that exact command returns the same
run, not a second one; the key is how a retrying client starts a run once.

Now poll the folded state until the worker has done what it can:

```bash
curl localhost:8000/runs/$RUN
```

Within a second or two `status` is `"suspended"`, `steps` holds `a` and `b` (both completed),
and `approvals` holds one entry with `"status": "pending"` and `"step_id": "pay"`. `pay` never
started, so `d` has not either. Open [http://localhost:8000/ui/runs/$RUN](http://localhost:8000/ui/)
to see the same fold as a graph: `pay` amber ("awaiting approval"), `d` grey ("pending").

## 4. The inbox names the gate

```bash
curl localhost:8000/approvals
```

One item: the run, the workflow, `"step_id": "pay"`, `"effect_classes": ["spend"]`, when it
was requested, and its `approval_id`. Keep that too: `AID=<the approval id>`. In the UI this
is the Approvals tab; the card says the same things and, under "what this approves", shows
what `pay` will receive from `a`.

## 5. Decide, as someone

First, what the engine refuses. `spend` is human-only unless the workflow's budget says
otherwise, and this one does not:

```bash
curl -X POST localhost:8000/runs/$RUN/approvals/$AID/approve \
     -H 'Content-Type: application/json' \
     -d '{"principal": {"kind": "agent", "id": "bot"}, "reason": "auto"}'
```

`403`, with the reason in `detail`: the approval covers `spend` and requires a human
principal. Nothing was recorded. Now as a human:

```bash
curl -X POST localhost:8000/runs/$RUN/approvals/$AID/approve \
     -H 'Content-Type: application/json' \
     -d '{"principal": {"kind": "human", "id": "you"}, "reason": "within budget"}'
```

`202`. The worker dispatches `pay`, then `d`; poll `GET /runs/$RUN` again and `status` is
`"completed"` with four steps. The approval entry now reads `"status": "granted"`,
`"decided_by": {"kind": "human", "id": "you"}`, `"decision_reason": "within budget"`, and a
`decided_at`. In the UI, the run page's Approvals panel shows the same as a record row.

In `asserted` mode that `"you"` is whatever you typed; the UI's identity bar says
**Unverified** for exactly this reason. With `AGENTOS_AUTH=bearer` the token decides who you
are and a body principal is rejected.

## 6. Read the log that proves it

The fold you have been polling is a convenience. The record is the event log:

```bash
curl 'localhost:8000/runs/$RUN/events?after=0'
```

Seventeen events, in this order (seq numbers will match):

| seq | event | note |
| --- | --- | --- |
| 1 | `run.started` | |
| 2–4 | `step.started/progress/completed` `a` | |
| 5 | `approval.requested` `pay` | the gate, raised before dispatch |
| 6–8 | `step.started/progress/completed` `b` | the sibling still runs |
| 9 | `run.suspended` | nothing left that can run without a decision |
| 10 | `approval.granted` `pay` | carries `principal` and `reason` |
| 11–13 | `step.started/progress/completed` `pay` | exactly once, after the grant |
| 14–16 | `step.started/progress/completed` `d` | |
| 17 | `run.completed` | |

Two things to notice. The gate is at seq 5 but the run does not suspend until seq 9: a gated
step never blocks its siblings, only itself and what depends on it. And `pay` starts *after*
the grant; the engine does not run a gated step and ask forgiveness.

Every event carries a hash chained to the previous one:

```bash
curl localhost:8000/runs/$RUN/integrity
```

`"ok": true`, `"hashed": 17`, and `"seals": {"state": "unsigned"}`. Unsigned because this
tutorial set no `AGENTOS_SIGNING_KEYS`; with a keyring the idle and terminal events are HMAC
sealed and the state reads `verified`. The CLI does the same for every run: `agentos verify`.

## 7. Rewind to the moment it stopped

The server folds any prefix of the log on request:

```bash
curl 'localhost:8000/runs/$RUN?at=9'
```

`"status": "suspended"`, the approval `"pending"`, two steps done. That is the run as it was
when it stopped, computed by the same fold over events 1–9, not a cached snapshot. Try `at=5`
(still `running`; `b` has not finished) and `at=10` (`running` again; the grant landed, `pay`
has not started). In the UI, drag the Timeline scrubber: the graph, cost table and Approvals
panel all follow the seek, and the decision buttons disappear because a historical fold is
not something you can decide on.

## What you just used

| you did | the primitive | where it is tested |
| --- | --- | --- |
| declared `spend` on an agent | `EffectClass`, `declared_effects` | `tests/test_approvals.py` |
| the run suspended before the step | budget gate, `approval.requested` | `tests/test_approvals.py::test_gated_step_suspends_before_dispatch_and_siblings_still_run` |
| a non-human was refused | `HUMAN_ONLY_EFFECTS`, `allow_agent_approval` | `tests/test_approvals.py::test_spend_requires_human_unless_workflow_allows_agent_approval` |
| the decision named you | `Principal` on `approval.granted` | `tests/test_auth.py` |
| the same key gave the same run | `Idempotency-Key` on run start | `tests/test_api_durability.py` |
| the chain verified | `verify`, `verify_seals` | `tests/test_seal_and_cli.py` |
| you rewound the fold | `GET /runs/{id}?at=k` | `tests/test_time_travel.py` |
| this page | all of the above, as written | `tests/test_tutorial.py` |

## Next

- Reject instead of approve: the step is dead-lettered with your name and reason, the run
  fails, and `POST /runs/{id}/steps/pay/retry` re-asks for approval rather than re-running.
- A cost ceiling: give the workflow `"budget": {"max_run_cost": "0.25"}` and use agents that
  report cost; the run suspends when the ceiling is crossed and approving *raises* it.
- Operator policy: `AGENTOS_POLICY=examples/operator_policy.json` intersects every workflow's
  budget with a ceiling one person controls; `agentos policy explain` shows what it narrows.
- A real model: [`quickstart-llm.md`](quickstart-llm.md).
