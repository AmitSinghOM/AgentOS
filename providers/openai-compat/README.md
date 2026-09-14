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
version, concrete `model_id`, `model_alias`, `prompt_hash`. Effects: `compute` only.

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

The engine records each as `step.failed` → retries per the node's policy →
`step.dead_lettered` with the message as the cause.

## Testing (§11 A10)

`tests/` runs against **cassettes recorded from a real Ollama** (`qwen2.5:0.5b`, see each
file's `source`), never the network. `tests/reference_server.py` fakes specific status
codes. Re-record: `python scripts/record_cassettes.py --live`. The nightly
[`provider-live`](../../.github/workflows/provider-live.yml) workflow installs Ollama on a
runner, re-records, and re-runs the replay tests on the fresh recordings. If that job fails
for 30 days this README gets a `deprecated` banner; the core is unaffected.

## Status

`0.1.0` — first provider plugin. MIT.
