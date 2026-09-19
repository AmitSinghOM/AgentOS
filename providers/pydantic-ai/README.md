# agentos-provider-pydantic-ai

Run a [PydanticAI](https://ai.pydantic.dev) agent as **one governed AgentOS step**. The SDK
owns the agent loop — model, tools, turns. AgentOS owns the ledger and the authority: which
tools the model is even offered, whether the step may run at all, what it cost, and a
hash-chained record of what happened.

This is the second inner harness, after `agentos-provider-openai-agents`. They share one tool
registry, one set of config keys and one output shape (pinned by a test), so a workflow
switches harness by changing the `executor` field and nothing downstream moves.

```bash
pip install -e . -e providers/pydantic-ai       # from a checkout; restart the API and worker
```

Zero-config default: PydanticAI's OpenAI-compatible chat model against a local Ollama
(`http://127.0.0.1:11434/v1`), no API key. `GET /executors` shows it as `pydantic-ai`.

## An agent

```json
{
  "name": "editor", "type": "llm", "executor": "pydantic-ai",
  "declared_effects": ["compute"],
  "config": {
    "model": "chat.default",
    "instructions": "Count the words in the text with the word_count tool, then reply with the number.",
    "prompt": "Text: {write.text}",
    "tools": ["word_count", "utc_now"],
    "max_turns": 4
  }
}
```

`instructions` (or `system`), `prompt` with `{dotted.inputs}`, `model` (alias or id),
`tools` (registered names), `max_turns` (default 6; PydanticAI's `UsageLimits.request_limit`),
`temperature`, `json_output`, `output_schema` (below). Identical to the openai-agents
provider's keys on purpose.

## Where the seam is

1. **The tool registry** (`dagentos.providerkit.tools`). Tools are the operator's Python
   functions, each carrying an `EffectClass`. Agent JSON names them; it never contains
   code. Register yours with the `agentos.tools` entry point (a callable returning
   `Iterable[ToolSpec]`) and both inner harnesses offer them; two harmless built-ins
   (`utc_now`, `word_count`, class `compute`) ship for the quickstart. At dispatch each
   `ToolSpec.fn` is wrapped in a `pydantic_ai.Tool`, so PydanticAI derives the schema the
   model sees from the function's annotations and docstring.
2. **The core's declared-effects gate.** The model is offered only the tools whose class the
   AgentOS agent *declared*. An undeclared tool is not offered-and-refused; it is not there
   (`tools_withheld` in the output says which). A step declaring an approval-required class
   (`spend`, `write_external`, …) is suspended by the governor **before this executor runs**
   — no `step.started`, no model call. Every tool the model actually called is reported as
   an `Effect`, so the governor's post-step check is the backstop.

## What lands in the log

One `step.completed` per run: `text` (final output), `turns` (model requests),
`tool_calls` (name, call id, SHA-256 of arguments and of output — the trajectory without
inlining large outputs; a call whose arguments failed validation never ran and carries
`rejected: true` with the retry message hashed instead), `tools_offered` / `tools_withheld`,
`usage`, and a `Cost` metered
from the run's summed usage through the bundled pricing table (`model_requests` as an extra
meter). Provenance carries the model, alias and a prompt hash over instructions, input and
offered tool names.

## Not in this slice (ROADMAP ⏭)

- **Mid-step approval.** PydanticAI can return `DeferredToolRequests` when a tool is marked
  `requires_approval` or raises `ApprovalRequired` / `CallDeferred`. Honouring that would
  mean persisting the message history as a blob and a resume protocol. Until then, gate at
  step granularity (declare the class), do not mark registry tools `requires_approval`, and
  a run that comes back deferred **raises** rather than approving on the operator's behalf.
- Token-level streaming into `progress()`; MCP toolsets as registry entries.

## Typed output (`output_schema`)

Replace `json_output: true` with a JSON Schema object under `config.output_schema` and the
step's `json` is guaranteed to satisfy it, or the step fails naming the first violation
(`reply violates output_schema at $.score: 9 is greater than the maximum of 5`). The
schema reaches the model through `PromptedOutput(StructuredDict(schema))` — it works on any
chat backend, no tool calling needed — and PydanticAI retries the model on non-JSON within
`max_turns`. PydanticAI's `StructuredDict` validates only "is a JSON object", so the CONTRACT
is enforced by `dagentos.providerkit.schema`, the same code the openai-agents harness uses, and
its SHA-256 lands on `step.completed` as `schema_sha256`. Draft 2020-12; root must be `type:
object`; remote `$ref`s are refused at first use (no fetch); `format` is never enforced
(reproducible verdicts). The schema is part of `prompt_hash`.

PydanticAI's own instrumentation (Logfire / OpenTelemetry) is opt-in and is never enabled
here; the worker's observers remain the only telemetry path.

## Environment

| Variable | Default |
|---|---|
| `AGENTOS_PYDANTIC_AI_BASE_URL` | `http://127.0.0.1:11434/v1` |
| `AGENTOS_PYDANTIC_AI_API_KEY` | unset (falls back to `OPENAI_API_KEY`) |
| `AGENTOS_PYDANTIC_AI_ALIASES` | `{"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}` |
| `AGENTOS_PYDANTIC_AI_PRICING` | bundled `pricing.json` |
| `AGENTOS_PYDANTIC_AI_TIMEOUT` | `60` |

Invalid values fail at startup naming the variable. The HTTP client is built from these
values only: `OPENAI_BASE_URL` in the process environment is ignored (tested), unlike a bare
`OpenAIProvider()`.

## Tests

`providers/pydantic-ai/tests` drives every run through PydanticAI's own `FunctionModel` —
no network, no cassettes — asserts on the instructions and tool list the model actually saw,
proves the per-step client is closed on success and failure, maps every `ModelHTTPError`
status to the providerkit vocabulary, pins output-key parity with the openai-agents
executor, and goes through the core to prove the gate suspends a `spend`-declaring step
before the SDK is invoked. Live: the quickstart's poet and critic run on this executor
against a local Ollama (`scripts/quickstart_llm.py --executor pydantic-ai`), and a
`word_count` tool step was run live too. What that showed: `qwen2.5:0.5b` does emit tool
calls here, but on the delimited prompt it invented an extra argument (a
`toolbench_rapidapi_key`, a training-data artefact); PydanticAI's schema validation rejected
the call without running the tool and the log records it as `rejected: true` with the retry
message hashed — the harness contained the model, and the trace says so. Use a tool-capable
model (`chat.default`) for tool steps that need to succeed.
