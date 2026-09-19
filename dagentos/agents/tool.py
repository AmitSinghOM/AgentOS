"""The `tool` executor: HTTP calls and subprocesses as workflow steps, governed exactly like
model steps — same gate, same cost meter, same provenance, same trust boundary.

Two kinds, chosen by `config.kind`:

    http        {"kind": "http", "method": "GET", "url": "https://api.github.com/repos/o/r",
                 "query": {"q": "{run.topic}"}, "json": {"text": "{write.text}"},
                 "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}, "timeout": 30}
    subprocess  {"kind": "subprocess", "argv": ["python", "scripts/summarise.py"],
                 "cwd": ".", "env": {"API_KEY": "${API_KEY}"}, "timeout": 60}

The boundaries (docs/TRUST_BOUNDARY.md §4), fixed by construction rather than by option:

- **Host, path and program are the operator's.** `url` and `argv` come from the agent
  definition verbatim and are never templated — step inputs cannot redirect a request or
  change a command (injection). Inputs reach a tool only as *values*: templated into
  `query`/`json` string values (URL-encoded / JSON-encoded by httpx) and, for a subprocess,
  as one JSON document on stdin. Redirects are not followed. The SSRF vector that remains —
  the operator's own hostname resolving to a metadata / link-local / private address — is
  refused by an egress check (`_check_egress`); private networks need an explicit opt-in.
  That check is defence in depth, not a substitute for network-layer egress policy.
- **Secrets are the operator's too.** `${NAME}` in `headers` or `env` is substituted from
  the executor's process environment at call time, never from inputs, and header values
  are redacted from the recorded output.
- **Effects are honest.** GET/HEAD report `read`; any other method reports
  `write_external` with the URL as `external_ref`; a subprocess reports `execute_code`.
  The agent must DECLARE those, so the existing three-tier gate (allowed / approval /
  refused) governs a tool step with no new mechanism.
- **Results are data.** A response body or stdout becomes the next step's inputs and is
  wrapped as `<input>` by every provider like any other output. Bodies are capped at
  `max_bytes` (default 1 MiB) so a hostile endpoint cannot flood the blob store.

Failures raise `ToolError`; the engine records `step.failed` and the node's retry policy
decides. There is no "do not retry" signal in the port yet — a 4xx retries like a 5xx.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any

import httpx

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
from dagentos.providerkit.prompt import render_prompt

VERSION = "0.7.0"
DEFAULT_MAX_BYTES = 1 << 20
_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class ToolError(RuntimeError):
    """The message says what failed and what to do; the engine records it as step.failed."""


class ToolExecutor:
    name = "tool"
    version = VERSION

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 env: dict[str, str] | None = None) -> None:
        self._transport = transport
        self._env = env                  # None → os.environ at call time (tests inject)

    def describe(self) -> dict:
        return {"kinds": ["http", "subprocess"], "max_bytes_default": DEFAULT_MAX_BYTES,
                "effects": {"http GET/HEAD": "read", "http other": "write_external",
                            "subprocess": "execute_code"}}

    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        cfg = req.agent.config
        kind = cfg.get("kind")
        if kind == "http":
            output, effects = self._http(cfg, req.inputs, progress)
        elif kind == "subprocess":
            output, effects = self._subprocess(cfg, req.inputs, progress)
        else:
            raise ToolError(f"agent {req.agent.name!r}: config.kind must be 'http' or "
                            f"'subprocess', got {kind!r}")
        return StepResult(
            output=output, effects=effects,
            cost=Cost(units=[Meter(name="requests", quantity=1)], amount="0"),
            provenance=Provenance(executor=self.name, executor_version=self.version),
        )

    # ------------------------------------------------------------------ http

    def _http(self, cfg: dict, inputs: dict, progress: ProgressFn):
        url = cfg.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ToolError("config.url must be an absolute http(s) URL fixed in the agent "
                            "definition (inputs may only fill config.query / config.json)")
        method = str(cfg.get("method", "GET")).upper()
        query = _render_values(cfg.get("query") or {}, inputs)
        body = _render_values(cfg.get("json"), inputs) if "json" in cfg else None
        headers = {k: self._substitute(v) for k, v in (cfg.get("headers") or {}).items()}
        timeout = float(cfg.get("timeout", 30))
        max_bytes = int(cfg.get("max_bytes", DEFAULT_MAX_BYTES))
        if self._transport is None:          # real network: egress guard (see _check_egress)
            _check_egress(url, allow_private=bool(cfg.get("allow_private_networks", False)))

        progress(0.0, f"{method} {url}")
        try:
            with httpx.Client(timeout=timeout, transport=self._transport,
                              follow_redirects=False) as c:
                r = c.request(method, url, params=query or None, json=body, headers=headers)
        except httpx.ConnectError as exc:
            raise ToolError(f"cannot reach {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise ToolError(f"{method} {url} did not answer within {timeout}s "
                            f"(raise config.timeout; the step's retry policy applies)") from exc
        if len(r.content) > max_bytes:
            raise ToolError(f"{method} {url} returned {len(r.content)} bytes, over "
                            f"config.max_bytes={max_bytes}")
        if r.status_code >= 400:
            raise ToolError(f"{method} {url} → HTTP {r.status_code}: {r.text[:200]!r} "
                            f"(the step's retry policy applies)")
        progress(1.0, f"HTTP {r.status_code}, {len(r.content)} bytes")

        output: dict[str, Any] = {
            "status": r.status_code, "url": str(r.url),
            "headers": {k: v for k, v in r.headers.items()
                        if k.lower() in ("content-type", "content-length", "location", "etag")},
        }
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                output["json"] = r.json()
            except ValueError:
                output["text"] = r.text
        else:
            output["text"] = r.text
        if method in ("GET", "HEAD"):
            effects = [Effect(effect_class=EffectClass.read, description=f"{method} {url}")]
        else:
            effects = [Effect(effect_class=EffectClass.write_external,
                              description=f"{method} {url}",
                              external_ref=r.headers.get("location") or str(r.url))]
        return output, effects

    # ------------------------------------------------------------------ subprocess

    def _subprocess(self, cfg: dict, inputs: dict, progress: ProgressFn):
        argv = cfg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ToolError("config.argv must be a non-empty list of strings fixed in the agent "
                            "definition (inputs are delivered on stdin as JSON, never in argv)")
        timeout = float(cfg.get("timeout", 60))
        cwd = cfg.get("cwd")
        if cwd is not None and not Path(cwd).is_dir():
            raise ToolError(f"config.cwd {cwd!r} is not a directory")
        env = {k: self._substitute(v) for k, v in (cfg.get("env") or {}).items()}
        env.setdefault("PATH", (self._env or os.environ).get("PATH", "/usr/bin:/bin"))
        max_bytes = int(cfg.get("max_bytes", DEFAULT_MAX_BYTES))

        progress(0.0, f"exec {argv[0]}")
        stdin = json.dumps(inputs, ensure_ascii=False).encode()
        try:
            # Own session so a timeout kills the whole process GROUP: a program that forks
            # must not leave grandchildren running past the step's wall-clock budget.
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, env=env, start_new_session=True)
        except FileNotFoundError as exc:
            raise ToolError(f"program {argv[0]!r} not found on the worker "
                            f"(PATH={env['PATH']!r})") from exc
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ToolError(f"{argv[0]} did not exit within {timeout}s; its process group was "
                            f"killed (raise config.timeout; the step's retry policy applies)"
                            ) from exc
        stderr = err.decode("utf-8", "replace")[-2000:]
        if proc.returncode != 0:
            raise ToolError(f"{argv[0]} exited {proc.returncode}: {stderr.strip()[:300]!r} "
                            f"(the step's retry policy applies)")
        if len(out) > max_bytes:
            raise ToolError(f"{argv[0]} wrote {len(out)} bytes to stdout, over "
                            f"config.max_bytes={max_bytes}")
        progress(1.0, f"exit 0, {len(out)} bytes")
        text = out.decode("utf-8", "replace")
        output: dict[str, Any] = {"exit_code": 0, "stderr": stderr}
        try:
            output["json"] = json.loads(text)
        except ValueError:
            output["text"] = text
        return output, [Effect(effect_class=EffectClass.execute_code, description=argv[0])]

    # ------------------------------------------------------------------ helpers

    def _substitute(self, value: Any) -> str:
        """`${NAME}` → the executor's environment. Unset names are an error naming them:
        a silently empty Authorization header is worse than a failed step."""
        env = self._env if self._env is not None else os.environ

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in env:
                raise ToolError(f"config references ${{{name}}} but it is not set in the "
                                f"worker's environment")
            return env[name]
        return _ENV_REF.sub(repl, str(value))


def _check_egress(url: str, *, allow_private: bool) -> None:
    """Defence in depth for the one SSRF vector an operator-fixed URL leaves open: the
    operator's hostname resolving — by mistake, or by a hostile/rebinding DNS answer — to a
    cloud metadata service, a link-local or loopback address, or a private network.
    Link-local (169.254.0.0/16, fe80::/10 — where every cloud metadata endpoint lives) is
    always refused. Loopback and RFC 1918 need `config.allow_private_networks: true`, so a
    laptop calling its own services opts in once, per agent version. Residual: the check
    resolves the name once and httpx resolves it again to connect (a TOCTOU window);
    pinning egress at the network layer is the complete answer and this does not replace it."""
    host = httpx.URL(url).host
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ToolError(f"refusing {url}: {host!r} resolves to {ip}, a link-local/reserved "
                            f"address (cloud metadata lives there); this is never allowed")
        if (ip.is_loopback or ip.is_private) and not allow_private:
            raise ToolError(f"refusing {url}: {host!r} resolves to {ip}, a private/loopback "
                            f"address; set config.allow_private_networks: true on this agent "
                            f"if that is intended")


def _render_values(obj: Any, inputs: dict) -> Any:
    """Template STRING VALUES of a query/json config from inputs; keys and structure are
    the operator's. Values are inserted as values (httpx encodes them), never spliced."""
    if isinstance(obj, str):
        return render_prompt(obj, inputs, delimit=False)
    if isinstance(obj, dict):
        return {k: _render_values(v, inputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_values(v, inputs) for v in obj]
    return obj
