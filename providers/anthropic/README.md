# agentos-provider-anthropic

AgentOS executor for the **Anthropic Messages API** wire format — Anthropic's own API, and
Ollama's `/v1/messages`, which serves the same shape locally. Plain `httpx`, no SDK.

This is the second provider, and its job is to prove the plugin seam: a different
request/response shape, a different error envelope (`{"type":"error","error":{…}}`,
including `529 overloaded_error`), a different auth header (`x-api-key` +
`anthropic-version`) — and zero changes to the core or to the OpenAI provider. An agent
moves between the two by changing one field, `executor`.

```bash
pip install -e . -e providers/anthropic          # from the AgentOS checkout
```

## Agent definition

Identical vocabulary to `openai-compat`:

```json
{"name": "poet", "type": "llm", "executor": "anthropic",
 "config": {"model": "chat.fast", "system": "You are a terse poet.",
            "prompt": "Write a haiku about {run.topic}."}}
```

| `config` key | meaning | default |
| --- | --- | --- |
| `model` | alias (`chat.fast`) or concrete id (`claude-3-5-haiku-latest`) | `chat.default` |
| `system` | sent as the top-level `system` field | none |
| `prompt` | user-message template; `{run.x}`, `{step.field}` | inputs as JSON |
| `temperature` | | `0` |
| `max_tokens` | the Messages API requires it | `1024` |
| `json_output` | the API has no `response_format`; an instruction is appended to `system` and fences are stripped before parsing | `false` |

## Environment

| variable | default | notes |
| --- | --- | --- |
| `AGENTOS_ANTHROPIC_BASE_URL` | `http://127.0.0.1:11434` | **no `/v1`** — the path is `/v1/messages`; Anthropic: `https://api.anthropic.com` |
| `AGENTOS_ANTHROPIC_API_KEY` | unset (falls back to `ANTHROPIC_API_KEY`) | Ollama needs none |
| `AGENTOS_ANTHROPIC_VERSION` | `2023-06-01` | `anthropic-version` header |
| `AGENTOS_ANTHROPIC_ALIASES` | `{"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}` | for Anthropic: e.g. `{"chat.fast": "claude-3-5-haiku-latest"}` |
| `AGENTOS_ANTHROPIC_PRICING` / `_TIMEOUT` / `_CASSETTES` / `_CASSETTE_DIR` / `_CASSETTE` | as in the OpenAI provider | |

## What a step records

Same as the OpenAI provider (`output.text/model/alias/finish_reason/usage/priced[/json]`,
metered `cost`, `provenance`), plus a `cached_input_tokens` meter when the server reports
cache reads/writes. Cached tokens are counted inside `input_tokens` at the full input rate
— conservative for Anthropic's 10 % cache-read price — and recorded separately so the
discount is recoverable from the log.

## Testing

Cassettes in `tests/cassettes/` were recorded from a real Ollama (`qwen2.5:0.5b`) via
`/v1/messages`; the shared scenarios live in `agentos.providerkit.conformance`.
`tests/anthropic_reference_server.py` fakes 401 / 429 / 529. Re-record:
`python scripts/record_cassettes.py --live`. The nightly `provider-live` workflow re-records
both providers on a runner and replays.

## Status

`0.1.0`. MIT.
