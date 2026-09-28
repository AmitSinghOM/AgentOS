# agentos-provider-acp

AgentOS executor that runs **one [Agent Client Protocol](https://agentclientprotocol.com)
agent turn as one governed step**. `kiro-cli acp` by default; any ACP agent (Gemini CLI,
Claude Code through its ACP adapter, a Zed-compatible agent, your own) by setting one
variable. Standard library only: ACP is newline-delimited JSON-RPC 2.0 over the agent's
stdio.

This is the fifth provider and the third inner harness. The first two (`openai-agents`,
`pydantic-ai`) wrap an agent *SDK* inside the worker process; this one wraps an agent
*program* — a whole coding agent with its own tools, login and model — and makes it a step
in a declared DAG. It is how AgentOS runs the agent a personal-workspace product like Kiro
Crew runs, under AgentOS's ledger and authority instead of a chat window.

```bash
pip install -e . -e providers/acp          # from the AgentOS checkout
kiro-cli login                             # the agent needs its own credentials; the worker inherits them
```

## What the step records

One `step.completed` per turn, in the hash-chained log:

| field | from |
|---|---|
| `output.text` | every `agent_message_chunk`, concatenated |
| `output.stop_reason` | the `session/prompt` result (`end_turn`; anything else is a `BadResponse`) |
| `output.tool_calls[]` | every `tool_call` / `tool_call_update`: `call_id`, `title`, `kind`, `status`, `effect_class`, `raw_input_sha256`, `raw_output_sha256`, and `permission` when the agent asked |
| `output.usage` | the last `usage_update` (context tokens used / window), when the agent sends one |
| `output.plan` | the agent's last `plan`, when it sends one |
| `cost` | meters `tool_calls` and `context_tokens_used`; `amount` from the agent's `usage_update.cost` when present, else `0` |
| `effects` | `compute`, plus one per effect class of tool the agent actually **ran** |
| `provenance.model_id` | the agent's advertised `agentInfo.name` (e.g. `Kiro CLI Agent`) |

Payloads are never inlined — arguments and outputs are recorded by SHA-256, the same
discipline as the other harnesses.

## The governance seam

An ACP agent asks its client for permission before sensitive tools
(`session/request_permission`). In an editor the client is a human clicking *Allow*. Here the
client is AgentOS, and **the answer comes from the step's declaration**:

| ACP tool `kind` | reported as | permission if declared | if not declared |
|---|---|---|---|
| `read`, `search`, `fetch` | `read` | allow once | reject |
| `think`, `switch_mode` | `compute` | allow once | reject |
| `edit`, `delete`, `move`, `execute`, `other`, unknown | `execute_code` | allow once | reject |

- `allow_always` is **never** selected: a grant belongs to one governed step, not to the
  agent's memory. If the agent offers only `allow_always`, the request is rejected.
- A tool the agent runs **without asking** (its own trust settings) is reported by class
  anyway. Under a step declared `[compute, read]`, an unasked `edit` produces
  `step.dead_lettered: step reported undeclared effect 'execute_code'` — the governor
  refuses it after the fact rather than the log pretending it did not happen.
- A rejected request never ran, so it is recorded as `permission: rejected` but is **not**
  an effect; the step stays within its declaration.
- The client advertises no `fs` or `terminal` capability. AgentOS lends the agent nothing of
  its own; the agent uses its own tools in `cwd`.

So the human who approved the step (for `spend` / `write_external` under the default
budget) or the operator who wrote the declaration already made every permission decision
the agent will ask for. Do **not** pass `--trust-all-tools` to kiro-cli: that bypasses the
seam on the agent's side.

## Agent definition

```json
{"name": "fixer", "type": "llm", "executor": "acp",
 "config": {"instructions": "You are working in a Python checkout. Make the smallest change that fixes the failing test.",
            "prompt": "Failing test: {run.test}. Fix it and run it."},
 "declared_effects": ["compute", "read", "execute_code"]}
```

`instructions`/`system` lead the prompt (ACP has no separate system slot on the wire; the
agent's own configuration owns its persona), then the `DATA_BOUNDARY`, then `prompt` rendered
with the inputs as delimited data (C12). `model` is ignored — the agent chooses its model.

## Configuration

| variable | default | meaning |
|---|---|---|
| `AGENTOS_ACP_COMMAND` | `["kiro-cli", "acp"]` | JSON list: the agent program and arguments |
| `AGENTOS_ACP_CWD` | worker's cwd | absolute path the session runs in (the agent's tools read and write here) |
| `AGENTOS_ACP_TIMEOUT` | `600` | seconds one turn may take before `session/cancel` + kill |

`ConfigError` names the variable. Nothing else in the environment is read; the agent process
inherits the worker's environment (that is how kiro-cli finds its login).

## Quickstart (live)

```bash
uvicorn dagentos.api.main:app &            # API
python -m dagentos.worker &                # worker; kiro-cli must be logged in on this host
python scripts/quickstart_llm.py --executor acp --topic "event logs"
curl localhost:8000/executors | jq '.[] | select(.name=="acp") | .health'
```

`health` spawns the agent, runs `initialize`, and reports `agentInfo` and capabilities
(cached for 60 s, since a spawn costs seconds). Verified on 2026-09-28 against
kiro-cli 2.24.1: protocol v1, one compute-only turn in 13.6 s, `end_turn`, no process left.

## Limits (honest)

- **One process per step.** kiro-cli takes ~5 s to start; a DAG of many tiny ACP steps pays
  it each time. Give ACP steps real work.
- **No mid-step suspension.** If the agent needs an approval AgentOS would gate (`spend`),
  declare the class on the agent so the *step* is gated before dispatch. The executor cannot
  pause the agent halfway and resume it later (that needs `session/load`, ROADMAP ⏭).
- **Cost is what the agent reports.** kiro-cli does not send `usage_update` today, so
  `cost.amount` is `0` and `context_tokens_used` is `null`; the step is still metered by
  `tool_calls`. When the agent reports cost, it is carried as-is.
- **Sandboxing is the agent's.** AgentOS gates *which classes* may run; it does not confine
  the agent process. Run the worker as a user whose `cwd` is a checkout you are prepared to
  let the agent modify.

Tests: `providers/acp/tests/test_acp.py` drives a real fake ACP agent
(`tests/fake_acp_agent.py`) over real stdio — no mocks of the transport. Set
`AGENTOS_ACP_LIVE=1` with kiro-cli on `PATH` to add a live health check.
