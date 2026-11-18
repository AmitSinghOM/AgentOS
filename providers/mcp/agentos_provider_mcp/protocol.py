"""A small Model Context Protocol client over a subprocess's stdio.

MCP (modelcontextprotocol.io, spec 2025-11-25) is JSON-RPC 2.0, one JSON object per line,
on the server's stdin/stdout; stderr is the server's log. The client (this code) calls
`initialize`, sends the `notifications/initialized` notification, then `tools/list` and
`tools/call`. The server may send notifications (`notifications/progress`,
`notifications/message`) while a request is in flight; it may also send requests of its own
(`sampling/createMessage`, `elicitation/create`, `roots/list`) — this client advertises none
of those capabilities and answers every server request with `-32601`.

Only one client request is ever in flight, so the loop is single-consumer: a reader thread
splits stdout into frames and queues them; `call()` drains the queue, dispatching
notifications and server requests as they arrive, until the response to its own request
shows up or the deadline passes. The reader enforces a per-frame byte cap while reading —
a frame that passes `max_bytes` stops the reader and surfaces as `McpOverCap`, so a hostile
server cannot buffer gigabytes into the worker.

Nothing here knows about AgentOS; the executor supplies the handlers. Standard library only.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

#: The protocol version this client speaks; what `initialize` asks for.
PROTOCOL_VERSION = "2025-11-25"
#: Versions whose `initialize` / `tools/list` / `tools/call` shapes this client handles. A
#: server that answers with another version it supports (spec: lifecycle → Version
#: Negotiation) is accepted when it is one of these; anything else is a transport error.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26",
                                                "2024-11-05")
#: No `roots`, `sampling` or `elicitation`: AgentOS lends the server nothing of its own.
CLIENT_CAPABILITIES: dict[str, Any] = {}
KILL_GRACE_SECONDS = 2.0
_READ_CHUNK = 65536
_EOF = object()

NotificationHandler = Callable[[str, dict], None]           # (method, params)


class McpTransportError(RuntimeError):
    """The server process could not be started, exited, or broke the framing."""


class McpTimeout(RuntimeError):
    """The server did not answer within the deadline; the caller cancels and kills it."""


class McpOverCap(RuntimeError):
    """The server wrote a frame past `max_bytes`; `size` is how much was read. The reader
    has stopped; the caller kills the process group."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"frame of at least {size} bytes, over max_bytes={cap}")
        self.size, self.cap = size, cap


class McpRpcError(RuntimeError):
    def __init__(self, method: str, error: dict) -> None:
        self.method, self.code = method, error.get("code")
        self.error = error
        super().__init__(f"{method} failed: {error.get('code')} {error.get('message')}")


class McpClient:
    def __init__(self, command: list[str], cwd: str | None, env: dict[str, str], *,
                 max_bytes: int, on_notification: NotificationHandler | None = None,
                 client_name: str = "agentos-provider-mcp", client_version: str = "0",
                 stderr_limit: int = 8192) -> None:
        self._command, self._cwd, self._env = list(command), cwd, dict(env)
        self._max_bytes = max_bytes
        self._on_notification = on_notification or (lambda m, p: None)
        self._client_info = {"name": client_name, "version": client_version}
        self._proc: subprocess.Popen | None = None
        self._frames: queue.Queue = queue.Queue()
        self._next_id = 0
        self._stderr = bytearray()
        self._stderr_limit = stderr_limit
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.protocol_version: str | None = None

    # -- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the server with EXACTLY `env` as its environment (the executor builds it
        from the agent definition plus PATH — the worker's environment is not inherited)."""
        try:
            self._proc = subprocess.Popen(
                self._command, cwd=self._cwd, env=self._env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                start_new_session=True,       # kill the whole group on timeout, not just a shell
            )
        except FileNotFoundError as exc:
            raise McpTransportError(f"server program {self._command[0]!r} not found on PATH "
                                    f"({exc.strerror})") from exc
        except OSError as exc:
            raise McpTransportError(f"could not start {self._command!r}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self, *, force: bool = False) -> None:
        """Spec (lifecycle → Shutdown, stdio): close the server's stdin, wait, SIGTERM, then
        SIGKILL — each on the whole process group. `force=True` (timeout, over-cap, engine
        cancel) skips the grace and kills the group at once. Always leaves no process behind."""
        p = self._proc
        if p is None:
            return
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except OSError:
            pass
        if p.poll() is None:
            if force:
                self._signal_group(p, signal.SIGKILL)
                self._wait(p)
            else:
                if self._wait(p) is None:
                    self._signal_group(p, signal.SIGTERM)
                    if self._wait(p) is None:
                        self._signal_group(p, signal.SIGKILL)
                        self._wait(p)
        for stream in (p.stdout, p.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    @staticmethod
    def _wait(p: subprocess.Popen) -> int | None:
        try:
            return p.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return None

    @staticmethod
    def _signal_group(p: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.send_signal(sig)
            except ProcessLookupError:
                pass

    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc else None

    def stderr_tail(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace").strip()

    # -- protocol ------------------------------------------------------------------------
    def initialize(self, deadline: float) -> dict:
        result = self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": CLIENT_CAPABILITIES,
            "clientInfo": self._client_info,
        }, deadline)
        got = result.get("protocolVersion")
        if got not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpTransportError(
                f"server answered initialize with protocolVersion {got!r}, not one this "
                f"provider speaks ({', '.join(SUPPORTED_PROTOCOL_VERSIONS)}); upgrade the "
                f"server or this provider so they agree")
        self.protocol_version = got
        self.server_info = result.get("serverInfo") or {}
        self.server_capabilities = result.get("capabilities") or {}
        self.notify("notifications/initialized", None)
        return result

    def list_tools(self, deadline: float, *, max_pages: int = 50) -> list[dict]:
        """Every page of `tools/list`, followed through `nextCursor`."""
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self.call("tools/list", params, deadline)
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpTransportError(f"tools/list returned no `tools` list: {str(result)[:200]}")
            tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise McpTransportError(f"tools/list did not end after {max_pages} pages")

    def call_tool(self, name: str, arguments: dict, deadline: float) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments}, deadline)

    def cancel(self, request_id: int, reason: str) -> None:
        """Spec (utilities → cancellation): tell the server to stop the in-flight request.
        Best effort — the server may already be gone."""
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except (OSError, ValueError, McpTransportError):
            pass

    # -- JSON-RPC ------------------------------------------------------------------------
    @property
    def last_request_id(self) -> int:
        return self._next_id

    def notify(self, method: str, params: dict | None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def call(self, method: str, params: dict, deadline: float) -> dict:
        """Send one request and pump the inbound stream until its response arrives."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeout(method)
            try:
                item = self._frames.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.returncode is not None:
                    raise McpTransportError(self._exit_message(method)) from None
                continue
            if item is _EOF:
                rc = self._wait(self._proc) if self._proc else None
                raise McpTransportError(self._exit_message(method, rc))
            if isinstance(item, Exception):
                raise item
            msg: dict = item
            if "method" in msg:
                if "id" in msg and msg["id"] is not None:
                    # A server request (sampling, elicitation, roots): no capability was
                    # advertised, so refuse it; the server must carry on without.
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                        "code": -32601, "message": f"client does not implement {msg['method']} "
                                                   f"(capability not advertised)"}})
                else:
                    self._on_notification(msg["method"], msg.get("params") or {})
                continue
            if msg.get("id") != rid:
                continue                               # not ours (stale); ignore
            if "error" in msg:
                raise McpRpcError(method, msg["error"] or {})
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def _send(self, msg: dict) -> None:
        p = self._proc
        if p is None or p.stdin is None:
            raise McpTransportError("server not started")
        try:
            p.stdin.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            p.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:           # server gone / stdin closed
            raise McpTransportError(self._exit_message(msg.get("method", "write"))) from exc

    # -- readers -------------------------------------------------------------------------
    def _read_stdout(self) -> None:
        """Byte-level reader: frames are split on `\\n`; a partial frame that passes
        `max_bytes` ends the reader (the caller kills the process group)."""
        p = self._proc
        assert p is not None and p.stdout is not None
        fd = p.stdout.fileno()
        buf = bytearray()
        while True:
            try:
                data = os.read(fd, _READ_CHUNK)
            except (OSError, ValueError):
                break
            if not data:
                break
            buf += data
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                self._queue_line(line)
            if len(buf) > self._max_bytes:
                self._frames.put(McpOverCap(len(buf), self._max_bytes))
                return                                  # stop reading; the caller kills it
        self._frames.put(_EOF)

    def _queue_line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        if len(line) > self._max_bytes:
            self._frames.put(McpOverCap(len(line), self._max_bytes))
            return
        try:
            parsed = json.loads(line.decode("utf-8"))
        except ValueError:
            self._frames.put(McpTransportError(
                f"server wrote a non-JSON line to stdout: {line[:200]!r} — an MCP server must "
                f"keep stdout for protocol frames and log to stderr"))
            return
        if not isinstance(parsed, dict):
            self._frames.put(McpTransportError(f"server wrote a non-object frame: {line[:200]!r}"))
            return
        self._frames.put(parsed)

    def _read_stderr(self) -> None:
        p = self._proc
        assert p is not None and p.stderr is not None
        fd = p.stderr.fileno()
        while True:
            try:
                data = os.read(fd, _READ_CHUNK)
            except (OSError, ValueError):
                break
            if not data:
                break
            if len(self._stderr) < self._stderr_limit:
                self._stderr.extend(data[:self._stderr_limit - len(self._stderr)])

    def _exit_message(self, method: str, rc: int | None = None) -> str:
        rc = self.returncode if rc is None else rc
        tail = self.stderr_tail()
        suffix = f"; stderr: {tail[-500:]}" if tail else ""
        return f"server exited with {rc} during {method}{suffix}"
