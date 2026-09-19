# agentos-provider-openai-agents

Run an [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) agent as **one
governed AgentOS step**. The SDK owns the agent loop — model, tools, turns. AgentOS owns
the ledger and the authority: which tools the model is even offered, whether the step may
run at all, what it cost, and a hash-chained record of what happened.

```bash
pip install -e . -e providers/openai-agents      # from a checkout; restart the API and worker
```

Zero-config default: the SDK's chat-completions model against a local Ollama
(`http://127.0.0.1:11434/v1`), no API key. `GET /executors` shows it as `openai-agents`.

## An agent

```json
{
  "name": "editor", "type": "llm", "executor": "openai-agents",
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
`tools` (registered names), `max_turns` (default 6), `temperature`, `json_output`.

## Where the seam is

Temporal's Agent Harness describes "a seam between the model deciding to use a capability
and that capability actually executing". Here it is two things you already have:

1. **The tool registry** (`agentos.providerkit.tools`, shared with the PydanticAI harness).
   Tools are the operator's Python functions, each carrying an `EffectClass`. Agent JSON
   names them; it never contains code. Register yours with the `agentos.tools` entry point
   (a callable returning `Iterable[ToolSpec]`) and every inner harness offers them; this
   provider also still loads the original `agentos.openai_agents_tools` group for one
   release. Two harmless built-ins (`utc_now`, `word_count`, class `compute`) ship for the
   quickstart.
2. **The core's declared-effects gate.** At dispatch the model is offered only the tools
   whose class the AgentOS agent *declared*. An undeclared tool is not offered-and-refused;
   it is not there (`tools_withheld` in the output says which). A step declaring an
   approval-required class (`spend`, `write_external`, …) is suspended by the governor
   **before this executor runs** — no `step.started`, no model call. Every tool the model
   actually called is reported as an `Effect`, so the governor's post-step check is the
   backstop.

## What lands in the log

One `step.completed` per SDK run: `text` (final output), `turns` (model calls),
`tool_calls` (name, call id, SHA-256 of arguments and of output — the trajectory without
inlining large outputs), `tools_offered` / `tools_withheld`, `usage`, and a `Cost` metered
from the SDK's summed usage through the bundled pricing table (`model_requests` as an
extra meter). Provenance carries the model, alias and a prompt hash over instructions,
input and offered tool names.

## Not in this slice (ROADMAP ⏭)

- **Mid-step approval.** The SDK can pause on a `needs_approval` tool (`interruptions`).
  Honouring that would mean persisting the SDK `RunState` as a blob and a resume
  protocol. Until then, gate at step granularity (declare the class) and do not set
  `needs_approval` on registry tools; a run that comes back paused **raises** rather than
  auto-approving.
- Token-level streaming of SDK events into `progress()`.
- MCP servers as registry entries.

## Environment

| Variable | Default |
|---|---|
| `AGENTOS_OPENAI_AGENTS_BASE_URL` | `http://127.0.0.1:11434/v1` |
| `AGENTOS_OPENAI_AGENTS_API_KEY` | unset (falls back to `OPENAI_API_KEY`) |
| `AGENTOS_OPENAI_AGENTS_ALIASES` | `{"chat.fast": "qwen2.5:0.5b", "chat.default": "llama3.2:3b"}` |
| `AGENTOS_OPENAI_AGENTS_PRICING` | bundled `pricing.json` |
| `AGENTOS_OPENAI_AGENTS_TIMEOUT` | `60` |

Invalid values fail at startup naming the variable.

## Tests

`providers/openai-agents/tests` drives every run through the SDK's own `ScriptedModel` —
no network, no cassettes — and one test goes through the core to prove the gate suspends
a `spend`-declaring step before the SDK is invoked. Live: the quickstart's poet and critic
were run on this executor against a local Ollama; note that a 0.5B model does not reliably
emit tool calls — use a tool-capable model (`chat.default`) for tool steps.
