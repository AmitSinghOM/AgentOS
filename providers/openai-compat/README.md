# agentos-provider-openai-compat

AgentOS executor for any server that speaks the OpenAI chat-completions wire format:
**Ollama** (zero-config default), vLLM, LM Studio, OpenRouter, OpenAI. Plain `httpx`, no
vendor SDK — the wire format is the stable contract; a client library is not.

```bash
pip install -e . -e providers/openai-compat        # from the AgentOS checkout
```

Registers the executor `openai-compat` through the `agentos.executors` entry point; the API
and worker discover it at startup and `GET /executors` shows it with a health check
(server reachable? aliases resolvable to models it actually has?).

Five-minute walkthrough with a local model: [`docs/quickstart-llm.md`](../../docs/quickstart-llm.md),
or `python scripts/quickstart_llm.py` against a running API.

## Agent definition

```json
{"name": "poet", "type": "llm", "executor": "openai-compat",
 "config": {"model": "chat.fast",
            "system": "You are a terse poet.",
            "prompt": "Write a haiku about {run.topic}."}}
```

| `config` key | meaning | default |
| --- | --- | --- |
| `model` | capability alias (`chat.fast`) or concrete id (`gpt-4o-mini`) | `chat.default` |
| `system` | system prompt | none |
| `prompt` | user-message template; `{run.x}` = run inputs, `{step.field}` = upstream output | inputs as JSON |
| `temperature` / `seed` | deterministic by default so cassettes replay | `0` / `42` |
| `max_tokens` | | server default |
| `json_output` | request a JSON object; parsed into `output.json` | `false` |
| `tools` | registered tool names the model may call (see [Tools](#tools)) | none |
| `tool_choice` | sent as-is alongside the offered tools (`auto`, `none`, `required`, or a named function) | server default |
| `max_tool_rounds` | how many rounds of tool calls the model may ask for | `4` |

## Tools

The function-calling round trip on the plain OpenAI wire format, with the same registry and
the same rules as the two SDK harnesses (`openai-agents`, `pydantic-ai`):

```json
{"name": "counter", "type": "llm", "executor": "openai-compat",
 "declared_effects": ["compute"],
 "config": {"model": "chat.default",
            "system": "Count the words with the word_count tool, then reply with the number.",
            "prompt": "How many words are in {run.topic}?",
            "tools": ["word_count"], "max_tool_rounds": 2}}
```

- **Tools are the operator's.** `config.tools` names entries of the shared registry
  (`dagentos/providerkit/tools.py`): two built-ins, `word_count` and `utc_now` (class
  `compute`), plus whatever the `agentos.tools` entry point registers. An unknown name is a
  definition error listing what is registered. Each tool's JSON Schema is derived from its
  type-annotated signature and docstring.
- **Offered by declared effect class.** The model is sent, as OpenAI `tools`, only the
  named tools whose effect class the agent declares; the rest are `tools_withheld`.
- **The rejection rule.** When a response carries `tool_calls`, each call is checked before
  anything runs. A call to a tool that was not offered (withheld, or a name nobody
  registered) is **rejected**: the function is never called, and the refusal (which tool,
  which class, what is offered) goes back as that call's tool result so the model can still
  answer. Arguments must fit the signature (unknown keys refused); a misfit is rejected the
  same way and the model may retry. A rejected call reports no effect when the tool was not
  offered; an offered tool refused for its arguments still reports its class, as the SDK
  harnesses do.
- **Results are data.** An executed tool's return value goes back as a `role: tool`
  message wrapped in `<input name="tool.NAME">…</input>`, the delimiter every input uses (C12).
  With tools offered, the system prompt also ends with one sentence telling the model to
  pass the data between the tags, not the tags: measured live on `llama3.2:3b` it lifted
  clean tool calls from 4/10 to 8/10 topics (`scripts/probe_tool_boundary.py`); without it
  the model copied `<input …>` into the argument, the server could not parse the call, and
  the step ended with the call as plain text and no tool run.
- **Bounded.** A model still asking for tools after `max_tool_rounds` rounds fails the step
  with `ToolError` naming the cap; so does a tool that raises (naming the tool). The step's
  retry policy decides what happens next.

What is recorded when `tools` is set: `tool_calls` (one entry per call: `name`, `call_id`,
`arguments_sha256`, `output_sha256`, and `rejected: true` with the refusal's hash in place
of an output), `tools_offered`, `tools_withheld`, `turns` (model calls), `usage` summed
over every round, a `model_requests` meter, and one `Effect` per distinct offered tool
called. The prompt hash covers the offered schemas. Without `tools` the request and the
output are unchanged. Mid-step approval is out of scope, as on the harnesses: a step that
declares an approval-required class is suspended by the governor before this executor runs.

## Environment

| variable | default | notes |
| --- | --- | --- |
| `AGENTOS_OPENAI_BASE_URL` | `http://127.0.0.1:11434/v1` | include `/v1` |
| `AGENTOS_OPENAI_API_KEY` | unset (falls back to `OPENAI_API_KEY`) | Ollama needs none |
| `AGENTOS_OPENAI_ALIASES` | `{"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}` | JSON, merged over the defaults |
| `AGENTOS_OPENAI_PRICING` | bundled `pricing.json` | content-addressed; hash on every step |
| `AGENTOS_OPENAI_TIMEOUT` | `60` | seconds |
| `AGENTOS_OPENAI_CASSETTES` | `off` | `replay` / `record` (tests) |
| `AGENTOS_OPENAI_CASSETTE_DIR` / `AGENTOS_OPENAI_CASSETTE` | `./cassettes` / `default` | |

A bad value raises `ConfigError` naming the variable; the API/worker log it and skip this
plugin rather than refusing to start.

## What a step records

`output`: `text`, `model` (the id the server reports), `alias`, `finish_reason`, `usage`,
`priced`, and `json` when requested. `cost`: `input_tokens`, `output_tokens`, `requests`
meters, a Decimal `amount`, and `pricing_snapshot_hash` — the table's bytes are stored in
the BlobStore at startup (`GET /blobs/{hash}`). `provenance`: `openai-compat`, package
version, concrete `model_id`, `model_alias`, `prompt_hash`. Effects: `compute`, plus
one per tool called (see [Tools](#tools)).

## Aliases and substitution (§11 A3)

`resolve(req)` maps the alias to a concrete id without touching the network. If the
resolution for an agent changes *during* a run — alias re-pointed, model retired — the
engine appends `executor.substituted{from, to, reason}` before the next step. Completed
steps are never re-run.

## Errors say what to do

| exception | when | message tells you |
| --- | --- | --- |
| `ProviderUnreachable` | connection refused | `ollama serve` / check `AGENTOS_OPENAI_BASE_URL` |
| `ModelNotFound` | 404 | `ollama pull <id>` or fix the alias |
| `AuthenticationFailed` | 401/403 | set `AGENTOS_OPENAI_API_KEY` |
| `ProviderRateLimited` / `ProviderServerError` | 429 / 5xx / timeout | the node's retry policy applies |
| `TemplateError` | `{x.y}` not in inputs | the keys that *are* available |
| `BadResponse` | malformed body or non-JSON when `json_output` | what came back |
| `ToolError` | the model is still calling tools after `max_tool_rounds`, or a tool raised | the cap to raise, or the tool that failed |

The engine records each as `step.failed` → retries per the node's policy →
`step.dead_lettered` with the message as the cause.

## Testing (§11 A10)

`tests/` runs against **cassettes recorded from a real Ollama** (`qwen2.5:0.5b`, see each
file's `source`), never the network. `tests/openai_reference_server.py` fakes specific status
codes. Re-record: `python scripts/record_cassettes.py --live`. The function-calling
cassette (`tool_round_trip.json`) is recorded from the reference server's scripted
trajectory (`--tools`), not a live model, and the nightly job leaves it alone. The nightly
[`provider-live`](../../.github/workflows/provider-live.yml) workflow installs Ollama on a
runner, re-records, and re-runs the replay tests on the fresh recordings. If that job fails
for 30 days this README gets a `deprecated` banner; the core is unaffected.

## Status

Versioned with the core (`pyproject.toml` here matches the root; `docs/RELEASING.md`
"Versions"). First provider plugin. MIT.
