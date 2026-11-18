# agentos-provider-mcp

AgentOS executor that runs **one [Model Context Protocol](https://modelcontextprotocol.io)
tool call on a stdio server as one governed step**. The agent definition names the server,
maps each tool it may call to an effect class, and fixes the one call; AgentOS gates the step
before the server is spawned, records the call by hash, and hands the result on as data.
Standard library only: MCP is newline-delimited JSON-RPC 2.0 over the server's stdio
([spec 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)).

This is the sixth provider. A pilot's `write_external` / `spend` tools already run behind MCP
servers (audit E-10); until now each had to be hand-wrapped as an HTTP or subprocess `tool`
agent. Now the server is the tool, and the ledger still says what ran, with which arguments,
under whose declaration.

```bash
pip install -e . -e providers/mcp          # from the AgentOS checkout
```

## Agent definition

```json
{"name": "fs_reader", "type": "tool", "executor": "mcp",
 "config": {"server": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/srv/data"],
                       "env": {"HOME": "${HOME}"}},
            "tools": {"read_text_file": {"effect": "read"},
                      "write_file": {"effect": "write_external"}},
            "call": {"tool": "read_text_file", "arguments": {"path": "${inputs.run.path}"}},
            "timeout": 60, "max_bytes": 1048576},
 "declared_effects": ["read"]}
```

| key | meaning |
|---|---|
| `server.command` | the operator's argv, verbatim — never templated; inputs reach the server only as tool arguments |
| `server.env` | the server's **whole** environment, plus `PATH`. `${NAME}` is filled from the worker's environment and redacted from everything the step records. Nothing else of the worker's is inherited (the `tool` executor's rule, not the ACP provider's) |
| `server.cwd` | optional working directory (must exist) |
| `tools` | **the operator's effect mapping.** A tool not in this map can never be called. The spec says a server's tool annotations are untrusted; they are never read |
| `call` | exactly **one** call per step. `tool` is a literal name; string values in `arguments` may reference step inputs as `${inputs.<dotted.path>}` (`${inputs.run.path}` is the run input `path`; `${inputs.fetch.text}` the upstream step `fetch`'s `text`). A value that is only a reference keeps its JSON type. `${NAME}` is refused in arguments |
| `tools_sha256` | optional pin of the server's tool list (see below) |
| `timeout` / `max_bytes` | per-agent overrides of `AGENTOS_MCP_TIMEOUT` (30 s) / `AGENTOS_MCP_MAX_BYTES` (1 MiB) |

## The governance seam

| the step asks for | what happens |
|---|---|
| a tool **not in `tools`** | rejected before the server is spawned: `ProviderError` naming the tool and the mapped names → `step.failed` → retry policy → `step.dead_lettered` with the cause |
| a mapped tool whose class is **not declared** on the agent | rejected the same way, before spawn, naming the class and the declared set |
| a mapped, declared tool | the step reports **one effect** of the mapped class (`mcp tool <name> on <command[0]>`), which the governor's settle check still verifies |
| a `write_external` / `spend` tool under the default budget | the run **suspends before dispatch** (the normal gate); the server is not spawned until a human approves |

A rejected call never ran, so there is no "successful step with `rejected: true`": a step
whose one call did not happen has no honest output, and it fails closed.

## Schema pinning

At every step the executor runs `tools/list`, keeps the tools named in `tools`, and records
`output.tools_sha256` = SHA-256 of their canonical JSON (sorted by name, keys sorted; order
of pages does not matter) plus `output.tool_schemas` = the same per tool. Set
`config.tools_sha256` to the recorded value and the step fails closed, **before `tools/call`**,
when the server's tool list changes or a mapped tool disappears. The error lists every
mapped tool's current hash; diff against the pinned run's `tool_schemas` to see which one
changed, then re-pin deliberately. The pin covers the whole tool object (name, description,
`inputSchema`, `outputSchema`, annotations) — a reworded description changes it too.

## What the step records

| field | from |
|---|---|
| `output.tool`, `output.call_id` | the call made; `call_id` is the JSON-RPC id on the wire |
| `output.arguments_sha256` | SHA-256 of the rendered arguments (canonical JSON) |
| `output.text` | the `text` content items, concatenated, wrapped as `<input name="mcp.<tool>">…</input>` (C12 — a tool result is data; a `</input>` inside it is escaped) |
| `output.output_sha256`, `output.content_bytes`, `output.content_types` | over the raw text; `content_types` counts items by type — image/audio/resource items are **never inlined** |
| `output.structured` | `structuredContent`, when the server returns one |
| `output.tools_sha256`, `output.tool_schemas` | the pin material (above) |
| `output.server`, `output.protocol_version` | `serverInfo` and the negotiated version |
| `output.redactions` | the `${NAME}`s whose values were scrubbed, when any |
| `effects` | one, of the mapped class |
| `cost` | meters `tool_calls` (1) and `content_bytes`; `amount` `0` |
| `provenance.prompt_hash` | SHA-256 of `{command, call}` |

`isError: true` from the server fails the step with the first 200 characters of its text
(redacted). A JSON-RPC error on `tools/call` is a `BadResponse`; a server that exits or is not
on `PATH` is `ProviderUnreachable`, naming the program.

## Bounded

- **Byte cap while reading.** A frame past `max_bytes` — even one the server never finishes —
  stops the reader and SIGKILLs the server's process group.
- **Deadline for the whole step** (spawn + `initialize` + `tools/list` + `tools/call`). On
  timeout `notifications/cancelled` is sent, then the group is killed.
- **Normal shutdown per spec:** stdin closed, wait, SIGTERM, SIGKILL. No process is left
  behind on any path, including an engine cancel raised from `progress()`.
- `progress()` is called on every inbound frame (lease renewal / cancel token).

## Configuration

| variable | default | meaning |
|---|---|---|
| `AGENTOS_MCP_TIMEOUT` | `30` | seconds one step may take before the server is killed; `config.timeout` overrides |
| `AGENTOS_MCP_MAX_BYTES` | `1048576` | largest frame read from the server; `config.max_bytes` overrides |

`ConfigError` names the variable. `health()` spawns nothing — the server lives in each agent
definition, so there is no executor-wide process to probe; a missing program surfaces at
dispatch as `ProviderUnreachable`.

## In the API

In `AGENTOS_AUTH=asserted` mode (the default, nobody verified the caller) `POST /agents`
refuses an `mcp` agent with 403: `config.server.command` is argv the API would spawn under
`?sync=true`, the `kind: subprocess` hole through another executor. Set
`AGENTOS_AUTH=bearer`, or `AGENTOS_ASSERTED_ALLOW_UNSAFE_TOOLS=1` to allow it anyway.

## Limits (honest)

- **stdio only.** `server.command` runs on this host, so "nothing leaves the host" holds
  unless the server itself talks to the network. Streamable-HTTP (remote) servers are out of
  scope for this provider today (ROADMAP ⏭); when they come they go through the `tool`
  executor's egress guard.
- **One call per step; no model in the loop.** A model choosing among MCP tools is a later
  item; here the operator fixes the call and inputs fill only the argument values.
- **The aggregate pin cannot name the changed tool by itself**; the error lists the current
  per-tool hashes and the pinned run's `tool_schemas` has the old ones.
- **Sandboxing is the server's.** AgentOS gates *which class* the call is; it does not confine
  the server process.

Tests: `providers/mcp/tests/test_mcp.py` drives a real fake MCP server
(`tests/fake_mcp_server.py`) over real stdio — no mocks of the transport. Set
`AGENTOS_MCP_LIVE=1` with `npx` on `PATH` to add one live `read_text_file` against
`@modelcontextprotocol/server-filesystem`.
