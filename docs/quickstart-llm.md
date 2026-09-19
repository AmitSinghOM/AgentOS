# Quickstart: a real model in five minutes, no API key

This runs a two-step LLM workflow — a poet writes a haiku, a critic scores it as JSON —
on a model that lives on your laptop. Nothing leaves your machine and nothing costs money.
Every command below is also run by `tests/test_quickstart_llm.py` (against a recorded
cassette) so this page cannot silently rot.

## 1. A model server (≈1 minute)

Any OpenAI-compatible server works; [Ollama](https://ollama.com/download) is the
zero-config default.

```bash
brew install ollama            # or: curl -fsSL https://ollama.com/install.sh | sh
ollama serve &                 # listens on http://127.0.0.1:11434
ollama pull qwen2.5:0.5b       # ~400 MB; answers in about a second on a laptop
```

## 2. AgentOS plus the provider (≈1 minute)

```bash
git clone https://github.com/AmitSinghOM/AgentOS && cd AgentOS
python -m venv .venv && source .venv/bin/activate
pip install -e . -e providers/openai-compat
```

The provider is its own distribution (`agentos-provider-openai-compat`). The core never
imports it; the API and worker find it through the `agentos.executors` entry point.

## 3. Check what can run

```bash
uvicorn agentos.api.main:app --port 8000 &
python -m agentos.worker &
curl -s localhost:8000/executors | python -m json.tool
```

You should see `echo` and `openai-compat`, and under `openai-compat` a `health` block with
`"reachable": true` and `"aliases_available": {"chat.fast": true, ...}`. If `reachable` is
false, the `hint` says what to start. This is the first thing to look at whenever a step
fails.

## 4. Define the agents and the workflow

```bash
curl -X POST localhost:8000/agents -H 'Content-Type: application/json' -d @examples/poet_agent.json
curl -X POST localhost:8000/agents -H 'Content-Type: application/json' -d @examples/critic_agent.json
curl -X POST localhost:8000/workflows -H 'Content-Type: application/json' -d @examples/haiku_workflow.json
```

`examples/poet_agent.json` is the whole model of an agent:

```json
{"name": "poet", "type": "llm", "executor": "openai-compat",
 "config": {"model": "chat.fast",
            "system": "You are a terse poet. Answer with the poem only.",
            "prompt": "Write a haiku about {run.topic}."}}
```

- `model` is a **capability alias**. `chat.fast` points at `qwen2.5:0.5b` by default; set
  `AGENTOS_OPENAI_ALIASES='{"chat.fast": "gpt-4o-mini"}'` and the same agent runs on a
  different model with no redefinition — and if the alias changes *while a run is in
  flight*, the run's log records an `executor.substituted` event before the next step.
- `prompt` is a template. `{run.topic}` reads the run's inputs; `{write.text}` (in the
  critic) reads the upstream step `write`'s output field `text`.

## 5. Run it

```bash
curl -s -X POST localhost:8000/workflows/haiku/runs -H 'Content-Type: application/json' \
     -d '{"inputs": {"topic": "event logs"}}'
```

That returns `202` and a run id; the worker picks it up. Watch it live — one Server-Sent
Event per log record, from whichever process appended it:

```bash
curl -N localhost:8000/runs/{run_id}/stream
```

```
id: 1
event: run.started
data: {"seq":1,"event_type":"run.started",...}

id: 2
event: step.started
data: {"seq":2,"step_id":"poet",...}
...
id: 9
event: run.completed
```

The stream closes after the terminal event. Dropped? Reconnect with the last `id` you saw
(`-H 'Last-Event-ID: 5'`, or `?after=5`) and you get only what you missed. Long waits (a
run suspended for approval) get a `: keep-alive` comment every 15 s and the connection is
closed after an hour regardless — reconnect the same way. It is event-level, not
token-level. Or fetch the finished run in one call:

```bash
curl -s localhost:8000/runs/{run_id} | python -m json.tool
```

In the response you will find, per step, the `output` (`text`, and `json` for the critic),
the `cost` with metered `input_tokens` / `output_tokens` and a `pricing_snapshot_hash`, and
the `provenance` — which executor, which concrete model, which alias, the prompt hash.
`total_cost` is `0`: locally served models are priced at zero in the bundled table.

Prefer everything in one call while exploring? `?sync=true` runs the workflow in the API
process and returns the finished run:

```bash
curl -s -X POST 'localhost:8000/workflows/haiku/runs?sync=true' -H 'Content-Type: application/json' \
     -d '{"inputs": {"topic": "event logs"}}' | python -m json.tool
```

## 6. Same agents, a different wire format (optional)

There is a second provider, `anthropic`, which speaks the Anthropic Messages API — and
Ollama serves that format too. Change **one field** in the agent, nothing else:

```bash
pip install -e providers/anthropic       # then restart the API and worker
sed 's/"openai-compat"/"anthropic"/' examples/poet_agent.json > /tmp/poet.json
```

Register that agent (bump `version` if `poet` already exists), run the workflow again, and
the step's `provenance.executor` says `anthropic`. Point `AGENTOS_ANTHROPIC_BASE_URL` at
`https://api.anthropic.com` with `ANTHROPIC_API_KEY` and aliases like
`{"chat.fast": "claude-3-5-haiku-latest"}` for Anthropic's models. Tested end to end in
`tests/test_quickstart_llm.py::test_same_agents_run_on_the_anthropic_wire_format`.

## 6b. Bring the agent loop you already use (optional)

A third executor, `openai-agents`, runs an **OpenAI Agents SDK** agent as one AgentOS
step — the SDK owns the loop (model, tools, turns), AgentOS owns the gate, the cost meter
and the log. Same one-field change:

```bash
pip install -e providers/openai-agents    # then restart the API and worker
sed 's/"openai-compat"/"openai-agents"/' examples/poet_agent.json > /tmp/poet.json
```

Give it tools by name — the tools are the operator's Python, registered on the worker,
never code in the agent JSON — and the model is offered only those whose effect class the
agent *declared*; a step declaring `spend` or `write_external` is suspended for approval
before the SDK ever runs. `tool_calls`, `tools_withheld`, `turns` and the SDK's summed
token usage land on `step.completed`. Details in `providers/openai-agents/README.md`.

A fourth executor, `pydantic-ai`, does the same with a **PydanticAI** agent — same tool
registry, same config keys, same output shape, so the only edit is the executor name:

```bash
pip install -e providers/pydantic-ai    # then restart the API and worker
sed 's/"openai-compat"/"pydantic-ai"/' examples/poet_agent.json > /tmp/poet.json
```

Two inner harnesses sharing one seam is the point: the tools an operator registers once
(`agentos.tools` entry point) are offered by either, under the same declared-effects gate.
Details in `providers/pydantic-ai/README.md`.

## 7. A tool step before the model (optional)

The built-in `tool` executor calls an HTTP API or runs a program as a step, under the same
gate, cost meter and trust boundary as a model step. `examples/repo_facts_agent.json` fetches
this repository's metadata from GitHub (no token needed) and `repo_poet` writes a haiku
about `{facts.json.description}`:

```bash
for f in repo_facts_agent repo_poet_agent; do
  curl -X POST localhost:8000/agents -H 'Content-Type: application/json' -d @examples/$f.json; done
curl -X POST localhost:8000/workflows -H 'Content-Type: application/json' -d @examples/research_workflow.json
curl -s -X POST localhost:8000/workflows/research/runs
```

Three things to notice in the definition. The URL is fixed — inputs can only fill
`config.query` / `config.json` *values*, so a step upstream cannot redirect the request.
The agent declares `read`; a `POST` tool declares `write_external` and is **suspended for
approval before the request is sent**, exactly like a model step that declares it. And the
response body becomes the next step's input as data — the poet sees it inside `<input>`
tags like any other upstream output. `{"kind": "subprocess", "argv": [...]}` runs a program
instead; inputs arrive on stdin as JSON, never in `argv`, and a timeout kills the whole
process group. Calling something on your own machine (`http://localhost:…`) needs
`"allow_private_networks": true` on the agent — link-local addresses (cloud metadata) are
never allowed. Details: `agentos/agents/tool.py`.

## 8. See it in Jaeger and Grafana (optional)

`docker compose up -d` brings up Postgres, Jaeger, Prometheus and Grafana. Run the API and
worker with `AGENTOS_OTEL_EXPORTER=otlp AGENTOS_PROMETHEUS=1`, re-run step 5 (or
`python scripts/demo_traffic.py` for a few minutes of varied runs: both providers, an
approval, a dead-letter), then open [Jaeger](http://localhost:16686) — one trace per run,
the run span parenting each step span with `gen_ai.request.model` and token counts, and
approvals as child spans — and [Grafana](http://localhost:3000) (admin/admin) for the
AgentOS dashboard. The worker serves its own metrics on `:8001` (`AGENTOS_WORKER_METRICS_PORT`);
Prometheus scrapes both processes.

![Jaeger: a run suspended for approval, then resumed](images/jaeger-run-payment.png)

![Grafana: the provisioned AgentOS dashboard](images/grafana-dashboard.png)

## When something goes wrong

Every failure lands in the run's log as a `step.failed` / `step.dead_lettered` event whose
message says what to do, and `GET /runs/{id}` shows it. The provider's messages are
written to be acted on:

| Symptom in `run.error` | Fix |
| --- | --- |
| `cannot reach http://127.0.0.1:11434/v1 … Is Ollama running?` | `ollama serve` |
| `has no model 'x' … ollama pull x` | pull it, or change the alias / `config.model` |
| `rejected the credentials … Set AGENTOS_OPENAI_API_KEY` | export the key (only remote servers need one) |
| `needs executor 'openai-compat' but only [echo] are registered` | `pip install -e providers/openai-compat` and restart API and worker |
| `prompt template references {x.y} … available top-level keys: […]` | fix the template or add `depends_on` |
| `rate-limited … retry policy applies` / `HTTP 5xx` | the node's `retry` handles it; raise `max_attempts` |

## Pointing at another server

| Server | `AGENTOS_OPENAI_BASE_URL` | key |
| --- | --- | --- |
| Ollama (default) | `http://127.0.0.1:11434/v1` | none |
| vLLM / LM Studio | `http://host:8000/v1` / `http://127.0.0.1:1234/v1` | none |
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `AGENTOS_OPENAI_API_KEY` |

Then set the aliases to that server's model ids, e.g.
`AGENTOS_OPENAI_ALIASES='{"chat.fast": "gpt-4o-mini", "chat.default": "gpt-4.1"}'`. Paid
models are priced from `providers/openai-compat/agentos_provider_openai_compat/pricing.json`
(override with `AGENTOS_OPENAI_PRICING`); the table's hash is on every step and its bytes
are retrievable at `GET /blobs/{hash}`, so a charge stays explainable after prices change.

All variables: `providers/openai-compat/agentos_provider_openai_compat/config.py`.
