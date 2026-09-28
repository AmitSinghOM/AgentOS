"""A small Agent Client Protocol client over a subprocess's stdio.

ACP (agentclientprotocol.com, protocol v1) is JSON-RPC 2.0, one JSON object per line, on
the agent's stdin/stdout. The client (this code) calls `initialize`, `session/new` and
`session/prompt`; while a prompt turn runs, the agent sends `session/update` notifications
and may call back with requests such as `session/request_permission`, which the client must
answer before the turn can continue. Only one client-initiated request is ever in flight, so
the loop is single-consumer: a reader thread parses lines into a queue; `call()` drains the
queue, dispatching notifications and agent requests to the handlers as they arrive, until
the response to its own request shows up or the deadline passes.

Nothing here knows about AgentOS; the executor supplies the handlers. Standard library only.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}
KILL_GRACE_SECONDS = 2.0
_EOF = object()

NotificationHandler = Callable[[str, dict], None]           # (method, params)
RequestHandler = Callable[[str, dict], dict]                # (method, params) -> result


class AcpTransportError(RuntimeError):
    """The agent process could not be started, exited, or broke the framing."""


class AcpTimeout(RuntimeError):
    """The agent did not answer within the deadline; it has been cancelled and killed."""


class AcpRpcError(RuntimeError):
    def __init__(self, method: str, error: dict) -> None:
        self.method, self.code = method, error.get("code")
        self.error = error
        super().__init__(f"{method} failed: {error.get('code')} {error.get('message')}")


class AcpClient:
    def __init__(self, command: list[str], cwd: Path, *, env: dict[str, str] | None = None,
                 on_notification: NotificationHandler | None = None,
                 on_request: RequestHandler | None = None,
                 stderr_limit: int = 8192) -> None:
        self._command, self._cwd, self._env = list(command), cwd, env
        self._on_notification = on_notification or (lambda m, p: None)
        self._on_request = on_request or self._reject_request
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._next_id = 0
        self._stderr = bytearray()
        self._stderr_limit = stderr_limit
        self.agent_info: dict = {}
        self.agent_capabilities: dict = {}

    # -- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        import os
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        try:
            self._proc = subprocess.Popen(
                self._command, cwd=str(self._cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                start_new_session=True,       # kill the whole group on timeout, not just the shell
            )
        except FileNotFoundError as exc:
            raise AcpTransportError(f"agent program {self._command[0]!r} not found on PATH "
                                    f"({exc.strerror})") from exc
        except OSError as exc:
            raise AcpTransportError(f"could not start {self._command!r}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        """Terminate the agent if it is still alive; always leaves no process behind."""
        p = self._proc
        if p is None:
            return
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except OSError:
            pass
        if p.poll() is None:
            self._signal_group(p, "SIGTERM")
            try:
                p.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._signal_group(p, "SIGKILL")
                p.wait(timeout=KILL_GRACE_SECONDS)
        for stream in (p.stdout, p.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    @staticmethod
    def _signal_group(p: subprocess.Popen, name: str) -> None:
        import os
        import signal
        sig = getattr(signal, name)
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
            "clientCapabilities": CLIENT_CAPABILITIES,
            "clientInfo": {"name": "agentos-provider-acp", "version": _dist_version()},
        }, deadline)
        got = result.get("protocolVersion")
        if got != PROTOCOL_VERSION:
            raise AcpTransportError(f"agent answered initialize with protocolVersion {got!r}, "
                                    f"expected {PROTOCOL_VERSION}; upgrade the agent or this "
                                    f"provider so they agree")
        self.agent_info = result.get("agentInfo") or {}
        self.agent_capabilities = result.get("agentCapabilities") or {}
        return result

    def new_session(self, deadline: float) -> str:
        result = self.call("session/new", {"cwd": str(self._cwd), "mcpServers": []}, deadline)
        sid = result.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AcpTransportError(f"session/new returned no sessionId: {result!r}")
        return sid

    def prompt(self, session_id: str, text: str, deadline: float) -> str:
        result = self.call("session/prompt", {
            "sessionId": session_id, "prompt": [{"type": "text", "text": text}],
        }, deadline)
        stop = result.get("stopReason")
        if not isinstance(stop, str):
            raise AcpTransportError(f"session/prompt returned no stopReason: {result!r}")
        return stop

    def cancel(self, session_id: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": "session/cancel",
                        "params": {"sessionId": session_id}})
        except (OSError, ValueError):
            pass                                  # the agent may already be gone

    # -- JSON-RPC ------------------------------------------------------------------------
    def call(self, method: str, params: dict, deadline: float) -> dict:
        """Send one request and pump the inbound stream until its response arrives."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpTimeout(method)
            try:
                item = self._lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.returncode is not None:
                    raise AcpTransportError(self._exit_message(method)) from None
                continue
            if item is _EOF:
                # Drain any parse errors queued before EOF, then report the exit.
                rc = self._wait_exit()
                raise AcpTransportError(self._exit_message(method, rc))
            if isinstance(item, Exception):
                raise item
            msg: dict = item
            if "method" in msg:
                if "id" in msg and msg["id"] is not None:
                    self._answer_agent_request(msg)
                else:
                    self._on_notification(msg["method"], msg.get("params") or {})
                continue
            if msg.get("id") != rid:
                continue                               # not ours (stale); ignore
            if "error" in msg:
                raise AcpRpcError(method, msg["error"] or {})
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def _answer_agent_request(self, msg: dict) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
        try:
            result = self._on_request(method, params)
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        except _RejectRequest as exc:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": str(exc)}})

    @staticmethod
    def _reject_request(method: str, params: dict) -> dict:
        raise _RejectRequest(f"client does not implement {method}")

    def _send(self, msg: dict) -> None:
        p = self._proc
        if p is None or p.stdin is None:
            raise AcpTransportError("agent not started")
        p.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        p.stdin.flush()

    # -- readers -------------------------------------------------------------------------
    def _read_stdout(self) -> None:
        p = self._proc
        assert p is not None and p.stdout is not None
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self._lines.put(AcpTransportError(
                    f"agent wrote a non-JSON line to stdout: {line[:200]!r} — an ACP agent "
                    f"must keep stdout for protocol frames and log to stderr"))
                continue
            if not isinstance(parsed, dict):
                self._lines.put(AcpTransportError(f"agent wrote a non-object frame: {line[:200]!r}"))
                continue
            self._lines.put(parsed)
        self._lines.put(_EOF)

    def _read_stderr(self) -> None:
        p = self._proc
        assert p is not None and p.stderr is not None
        for line in p.stderr:
            if len(self._stderr) < self._stderr_limit:
                self._stderr.extend(line.encode("utf-8", "replace"))

    def _wait_exit(self) -> int | None:
        try:
            return self._proc.wait(timeout=KILL_GRACE_SECONDS) if self._proc else None
        except subprocess.TimeoutExpired:
            return None

    def _exit_message(self, method: str, rc: int | None = None) -> str:
        rc = self.returncode if rc is None else rc
        tail = self.stderr_tail()
        suffix = f"; stderr: {tail[-500:]}" if tail else ""
        return f"agent exited with {rc} during {method}{suffix}"


class _RejectRequest(Exception):
    pass


def _dist_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("agentos-provider-acp")
    except PackageNotFoundError:
        return "0.0.0+dev"
