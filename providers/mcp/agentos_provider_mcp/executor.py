"""One MCP tool call on a stdio server as one governed AgentOS step.

A pilot's `write_external` / `spend` tools often already run behind MCP servers (audit E-10).
Without this executor each had to be hand-wrapped as an HTTP / subprocess `tool` agent or a
Python function. Here the agent definition names the server, maps each tool it may call to
an effect class, and fixes the one call the step makes; the step is gated before the server
is spawned, the call is recorded by hash, and the result enters the run as data.

Agent `config`:

    server        {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                   "env": {"API_KEY": "${API_KEY}"}, "cwd": "/abs/dir"}
                  `command` is the operator's argv, verbatim — never templated. The server's
                  environment is `env` (with `${NAME}` substituted from the worker's
                  environment and redacted from everything the step records) plus PATH, and
                  nothing else of the worker's.
    tools         {"read_file": {"effect": "read"}, "write_file": {"effect": "write_external"}}
                  The OPERATOR's effect mapping. The spec says a server's tool annotations are
                  untrusted; they are never consulted. A tool not in this map cannot be called.
    call          {"tool": "read_file", "arguments": {"path": "${inputs.run.path}"}}
                  Exactly ONE call per step. `tool` is a literal name; string values in
                  `arguments` may reference step inputs as `${inputs.<dotted.path>}` (a value
                  that is only a reference keeps its JSON type). `${NAME}` is refused here —
                  arguments come from the definition and the inputs, never the environment,
                  never model text.
    tools_sha256  optional: pins the SHA-256 of the server's tool list (filtered to the mapped
                  names, canonical JSON). A mismatch fails the step before anything is called;
                  the operator re-pins deliberately. The observed hash is always recorded.
    timeout       seconds for the whole step (default AGENTOS_MCP_TIMEOUT, 30)
    max_bytes     largest frame read from the server (default AGENTOS_MCP_MAX_BYTES, 1 MiB)

Governance, by construction:

- **Rejected before send.** `call.tool` must be in `tools`, and its mapped class must be in
  `req.declared_effects`; otherwise the step fails with `ProviderError` naming the tool and
  the server is never spawned. The failure dead-letters through the normal path (`step.failed`
  → retry policy → `step.dead_lettered` with the cause). A rejected call never ran, so it is
  not recorded as a successful step with a `rejected` flag — a step whose one call did not
  happen has no honest output.
- **One effect**, of the mapped class, `description = "mcp tool <name> on <command[0]>"`.
  The governor's `_settle` still checks it against the declaration.
- **Results are data.** The text items of `tools/call` are concatenated and wrapped as
  `<input name="mcp.<tool>">…</input>` (`wrap_input`, C12); `structuredContent` is recorded
  as JSON; image/audio/resource items are counted by type, never inlined. `isError: true`
  fails the step with the server's text (first 200 chars, redacted).
- **Bounded.** A frame over `max_bytes` or no answer by the deadline kills the server's
  process group. `progress()` is called on every inbound frame (lease renewal / cancel
  token); when it raises, the server is killed.
- **stdio only.** `server.command` runs on this host; nothing leaves it unless the server
  itself talks to the network. Streamable-HTTP (remote servers) is out of scope here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from dagentos.core.models import (
    Cost,
    Effect,
    EffectClass,
    Meter,
    Provenance,
    StepRequest,
    StepResult,
)
from dagentos.core.ports import ProgressFn
from dagentos.providerkit.errors import BadResponse, ProviderError, ProviderUnreachable
from dagentos.providerkit.prompt import wrap_input

from .config import McpConfig, from_env
from .protocol import (
    CLIENT_CAPABILITIES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpClient,
    McpOverCap,
    McpRpcError,
    McpTimeout,
    McpTransportError,
)

NAME = "mcp"
try:
    VERSION = _dist_version("agentos-provider-mcp")
except PackageNotFoundError:  # running from a checkout without install
    VERSION = "0.0.0+dev"

_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_INPUT_REF = re.compile(r"\$\{inputs\.([A-Za-z0-9_.\-]+)\}")
_ANY_REF = re.compile(r"\$\{[^}]*\}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EFFECTS = ", ".join(e.value for e in EffectClass)


def tools_sha256(tools: list[dict]) -> str:
    """SHA-256 over the canonical JSON of a tool list: sorted by name, keys sorted, compact.
    Order-independent so pagination cannot change the pin."""
    ordered = sorted(tools, key=lambda t: str(t.get("name")))
    return _sha(_canonical(ordered))


def tool_sha256(tool: dict) -> str:
    return _sha(_canonical(tool))


class _Redactor:
    """The concrete values `${NAME}` substitution put into this step's `server.env`, so they
    can be scrubbed from everything the step records or reports (the `tool` executor's
    discipline, #137): error text, the recorded output, progress notes. Each occurrence
    becomes `[REDACTED:NAME]`; `names` lists what was removed. Values under `MIN_LEN` are
    not tracked — redacting a short common string would mangle unrelated output."""

    MIN_LEN = 8

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self.names: set[str] = set()

    def track(self, name: str, value: str) -> None:
        if len(value) >= self.MIN_LEN:
            self._values[name] = value

    def __call__(self, text: str) -> str:
        for name, value in sorted(self._values.items(), key=lambda kv: -len(kv[1])):
            if value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
                self.names.add(name)
        return text

    def progress(self, progress: ProgressFn) -> ProgressFn:
        return lambda fraction, note: progress(fraction, self(note))


class _Call:
    """The validated agent config plus the one call it makes, resolved before any spawn."""

    def __init__(self, cfg: dict, req: StepRequest, defaults: McpConfig) -> None:
        server = _require(cfg, "server", dict, "an object {command, env?, cwd?}")
        command = server.get("command")
        if not isinstance(command, list) or not command \
                or not all(isinstance(a, str) and a for a in command):
            raise ProviderError("config.server.command must be a non-empty list of strings fixed in "
                                "the agent definition (inputs reach the server only as tool arguments)")
        self.command: list[str] = command
        cwd = server.get("cwd")
        if cwd is not None and not Path(str(cwd)).is_dir():
            raise ProviderError(f"config.server.cwd {cwd!r} is not a directory")
        self.cwd: str | None = str(cwd) if cwd is not None else None
        env = server.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
            raise ProviderError("config.server.env must be an object of string values")
        self.env_template: dict[str, Any] = env

        tools = _require(cfg, "tools", dict, "an object mapping tool name → {\"effect\": ...}")
        if not tools:
            raise ProviderError("config.tools must map at least one tool to an effect class; "
                                "a tool not in this map can never be called")
        self.mapping: dict[str, EffectClass] = {}
        for name, entry in tools.items():
            if not isinstance(entry, dict) or "effect" not in entry:
                raise ProviderError(f"config.tools[{name!r}] must be an object "
                                    f"{{\"effect\": <class>}}, got {entry!r}")
            try:
                self.mapping[str(name)] = EffectClass(entry["effect"])
            except ValueError:
                raise ProviderError(f"config.tools[{name!r}].effect is {entry['effect']!r}, not one "
                                    f"of the effect classes: {_EFFECTS}") from None

        call = _require(cfg, "call", dict, "an object {tool, arguments?}")
        tool = call.get("tool")
        if not isinstance(tool, str) or not tool or "${" in tool:
            raise ProviderError("config.call.tool must be a literal tool name from config.tools "
                                "(inputs cannot choose the tool)")
        self.tool: str = tool
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProviderError(f"config.call.arguments must be a JSON object, got "
                                f"{type(arguments).__name__}")
        self.arguments_template: dict = arguments

        pin = cfg.get("tools_sha256")
        if pin is not None and not (isinstance(pin, str) and _HEX64.fullmatch(pin)):
            raise ProviderError(f"config.tools_sha256 must be a 64-hex SHA-256 (copy "
                                f"output.tools_sha256 from a run you trust), got {pin!r}")
        self.pin: str | None = pin

        timeout = cfg.get("timeout", defaults.timeout_seconds)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 0.0
        if not (self.timeout > 0):
            raise ProviderError(f"config.timeout must be a positive number of seconds, got {timeout!r}")
        max_bytes = cfg.get("max_bytes", defaults.max_bytes)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ProviderError(f"config.max_bytes must be a positive integer, got {max_bytes!r}")
        self.max_bytes: int = max_bytes

        # The governance seam: mapped, and the mapped class declared — BEFORE anything runs.
        effect = self.mapping.get(self.tool)
        if effect is None:
            raise ProviderError(
                f"mcp call rejected before send: tool {self.tool!r} is not in config.tools "
                f"(mapped: [{', '.join(sorted(self.mapping))}]); nothing was sent to the server. "
                f"Map it with an effect class, or fix config.call.tool")
        if effect not in req.declared_effects:
            raise ProviderError(
                f"mcp call rejected before send: tool {self.tool!r} is mapped to effect "
                f"{effect.value!r}, which this step has not declared (declared: "
                f"[{', '.join(sorted(e.value for e in req.declared_effects))}]); nothing was "
                f"sent to the server. Declare it on the agent, or map the tool differently")
        self.effect: EffectClass = effect
        self.arguments: dict = _render_arguments(self.arguments_template, req.inputs)


class McpExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: McpConfig | None = None, *, env: dict[str, str] | None = None) -> None:
        self.config = config or from_env()
        self._env = env                  # None → os.environ at call time (tests inject)

    # -- optional hooks ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "wire_format": "mcp", "transport": "stdio",
            "protocol_version": PROTOCOL_VERSION,
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "client_capabilities": CLIENT_CAPABILITIES,
            "timeout_seconds_default": self.config.timeout_seconds,
            "max_bytes_default": self.config.max_bytes,
            "effect_vocabulary": [e.value for e in EffectClass],
        }

    def health(self) -> dict:
        """Spawns nothing: the server lives in each agent definition (`config.server`), so
        there is no executor-wide process to probe, and `GET /executors` must stay cheap. A
        missing program surfaces at dispatch as `ProviderUnreachable` naming it."""
        return {"transport": "stdio", "spawns_on_health": False,
                "protocol_version": PROTOCOL_VERSION,
                "servers": "per agent definition (config.server.command); checked at dispatch"}

    # -- the port -----------------------------------------------------------------------
    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        call = _Call(req.agent.config, req, self.config)
        redact = _Redactor()
        env = self._server_env(call.env_template, redact)
        progress = redact.progress(progress)
        prog = call.command[0]
        params = {"name": call.tool, "arguments": call.arguments}
        prompt_hash = _sha(_canonical({"command": call.command, "call": params}))
        arguments_sha256 = _sha(_canonical(call.arguments))

        def on_frame(method: str, _params: dict) -> None:
            progress(0.7, f"mcp {method}")          # renews the lease; polls the cancel token

        client = McpClient(call.command, call.cwd, env, max_bytes=call.max_bytes,
                           on_notification=on_frame, client_name=f"agentos-provider-{NAME}",
                           client_version=VERSION)
        deadline = time.monotonic() + call.timeout
        progress(0.0, f"mcp spawn {prog}")
        force_kill = False
        try:
            try:
                client.start()
                client.initialize(deadline)
                progress(0.2, f"mcp initialize ok: {client.server_info.get('name')} "
                              f"protocol {client.protocol_version}")
                progress(0.3, "mcp tools/list")
                offered = client.list_tools(deadline)
                mapped = self._pinned_tools(call, offered)
                observed = tools_sha256(mapped)
                progress(0.5, f"mcp tools/list: {len(offered)} tool(s), mapped sha {observed[:12]}")
                if call.pin is not None and observed != call.pin:
                    per_tool = ", ".join(f"{t['name']}={tool_sha256(t)[:16]}" for t in mapped)
                    raise ProviderError(
                        f"config.tools_sha256 pins {call.pin[:16]}… but the server's tool list for "
                        f"the mapped tools hashes to {observed[:16]}…; per tool now: {per_tool}. "
                        f"Compare with output.tool_schemas recorded by the run you pinned from — the "
                        f"tool whose hash differs is the one whose schema changed — then re-pin "
                        f"deliberately. Nothing was called")
                progress(0.6, f"mcp tools/call {call.tool}")
                result = client.call_tool(call.tool, call.arguments, deadline)
            except McpTimeout as exc:
                force_kill = True
                client.cancel(client.last_request_id, "timeout")
                raise ProviderError(
                    f"mcp step timed out after {call.timeout:g}s during {exc}; the server's "
                    f"process group was killed. Raise config.timeout (or AGENTOS_MCP_TIMEOUT) or "
                    f"give the tool a smaller task") from exc
            except McpOverCap as exc:
                force_kill = True
                raise ProviderError(
                    f"mcp server {prog} wrote a frame of at least {exc.size} bytes, over "
                    f"max_bytes={exc.cap}; reading stopped there and its process group was "
                    f"killed. Raise config.max_bytes if the result is legitimately that large") from exc
            except McpTransportError as exc:
                raise ProviderUnreachable(f"{redact(str(exc))}. Check config.server.command "
                                          f"({prog!r}) runs and speaks MCP on stdio") from exc
            except McpRpcError as exc:
                raise BadResponse(f"mcp {redact(str(exc))}") from exc
            except BaseException:
                force_kill = True                    # engine cancel from progress(), or anything else
                raise
        finally:
            client.close(force=force_kill)

        text_items: list[str] = []
        types: dict[str, int] = {}
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "?")
            types[kind] = types.get(kind, 0) + 1
            if kind == "text" and isinstance(item.get("text"), str):
                text_items.append(item["text"])
        text = redact("\n".join(text_items))
        if result.get("isError"):
            raise ProviderError(f"mcp tool {call.tool!r} answered isError: true: {text[:200]!r} "
                                f"(the step's retry policy applies)")
        content_bytes = len(text.encode("utf-8"))
        progress(1.0, f"mcp tools/call {call.tool} ok: {content_bytes} bytes, {types}")

        output: dict[str, Any] = {
            "tool": call.tool, "call_id": str(client.last_request_id),
            "arguments_sha256": arguments_sha256, "output_sha256": _sha(text),
            "tools_sha256": observed, "tool_schemas": {t["name"]: tool_sha256(t) for t in mapped},
            "is_error": False, "content_bytes": content_bytes, "content_types": types,
            "text": wrap_input(f"mcp.{call.tool}", text),       # C12: a tool result is data
            "server": client.server_info, "protocol_version": client.protocol_version,
        }
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            output["structured"] = json.loads(redact(_canonical(structured)))
        if redact.names:
            output["redactions"] = sorted(redact.names)
        return StepResult(
            output=output,
            effects=[Effect(effect_class=call.effect, description=f"mcp tool {call.tool} on {prog}")],
            cost=Cost(units=[Meter(name="tool_calls", quantity=1),
                             Meter(name="content_bytes", quantity=float(content_bytes))], amount="0"),
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------
    def _server_env(self, template: dict[str, Any], redact: _Redactor) -> dict[str, str]:
        """The server's whole environment: the definition's `env` with `${NAME}` filled from
        the worker's environment (unset → error naming it), plus PATH. Nothing else of the
        worker's reaches the server."""
        worker = self._env if self._env is not None else os.environ

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in worker:
                raise ProviderError(f"config.server.env references ${{{name}}} but it is not set "
                                    f"in the worker's environment")
            redact.track(name, worker[name])
            return worker[name]
        env = {k: _ENV_REF.sub(repl, str(v)) for k, v in template.items()}
        env.setdefault("PATH", worker.get("PATH", "/usr/bin:/bin"))
        return env

    @staticmethod
    def _pinned_tools(call: _Call, offered: list[dict]) -> list[dict]:
        by_name = {str(t.get("name")): t for t in offered}
        missing = sorted(n for n in call.mapping if n not in by_name)
        if missing:
            raise ProviderError(
                f"mapped tool(s) {', '.join(repr(n) for n in missing)} not offered by the server "
                f"(offered: [{', '.join(sorted(by_name))}]); fix config.tools or the server. "
                f"Nothing was called")
        return [by_name[n] for n in sorted(call.mapping)]


def _render_arguments(obj: Any, inputs: dict) -> Any:
    """String VALUES may reference step inputs as `${inputs.a.b}`; keys and structure are
    the operator's. A string that is exactly one reference becomes the referenced value with
    its type kept; references inside a longer string are substituted as text. `${NAME}`
    (an environment reference) and any other `${…}` are refused."""
    if isinstance(obj, str):
        env = _ENV_REF.search(obj)
        if env:
            raise ProviderError(f"config.call.arguments contains {env.group(0)}: arguments may not "
                                f"read the environment; they come from the definition and "
                                f"${{inputs.*}} only")
        whole = _INPUT_REF.fullmatch(obj)
        if whole:
            return _lookup(whole.group(1), inputs)
        out = _INPUT_REF.sub(lambda m: _as_text(_lookup(m.group(1), inputs)), obj)
        other = _ANY_REF.search(out)
        if other:
            raise ProviderError(f"config.call.arguments contains {other.group(0)}, which is not a "
                                f"${{inputs.<path>}} reference")
        return out
    if isinstance(obj, dict):
        return {k: _render_arguments(v, inputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_arguments(v, inputs) for v in obj]
    return obj


def _lookup(path: str, inputs: dict) -> Any:
    node: Any = inputs
    seen: list[str] = []
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
            seen.append(part)
        else:
            where = f"at {'.'.join(seen)!r}" if seen else "at the top level"
            have = sorted(node) if isinstance(node, dict) else "a non-object value"
            raise ProviderError(f"config.call.arguments references ${{inputs.{path}}} but the step "
                                f"inputs have no such field; available {where}: {have}")
    return node


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _require(cfg: dict, key: str, kind: type, what: str) -> Any:
    value = cfg.get(key)
    if not isinstance(value, kind):
        raise ProviderError(f"config.{key} must be {what}; got {value!r}")
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
