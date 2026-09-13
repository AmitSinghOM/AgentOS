"""Layer 2 chaos fixtures: Toxiproxy between a worker and Postgres.

Locally: spawns `toxiproxy-server` (brew install toxiproxy) if the API is not already up.
CI: a toxiproxy service container. Both skipped cleanly when Postgres or Toxiproxy is
unavailable — these tests are about the wire, so they need the real wire.

Env:
  AGENTOS_TEST_PG_DSN               direct DSN, e.g. postgresql://127.0.0.1:5432/agentos_test
  AGENTOS_TEST_TOXIPROXY_API        default http://127.0.0.1:8474
  AGENTOS_TEST_TOXIPROXY_UPSTREAM   what the proxy dials, default 127.0.0.1:5432
  AGENTOS_TEST_TOXIPROXY_LISTEN     what the proxy listens on, default 0.0.0.0:15432
  AGENTOS_TEST_TOXIPROXY_CLIENT     what the worker dials, default 127.0.0.1:15432
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

import pytest

PG_DSN = os.environ.get("AGENTOS_TEST_PG_DSN")
API = os.environ.get("AGENTOS_TEST_TOXIPROXY_API", "http://127.0.0.1:8474")
UPSTREAM = os.environ.get("AGENTOS_TEST_TOXIPROXY_UPSTREAM", "127.0.0.1:5432")
LISTEN = os.environ.get("AGENTOS_TEST_TOXIPROXY_LISTEN", "0.0.0.0:15432")
CLIENT = os.environ.get("AGENTOS_TEST_TOXIPROXY_CLIENT", "127.0.0.1:15432")


class Toxiproxy:
    """The handful of Toxiproxy HTTP calls these tests need. stdlib only."""

    def __init__(self, api: str) -> None:
        self.api = api.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.api}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def alive(self) -> bool:
        try:
            self._req("GET", "/version")
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return False

    def reset(self) -> None:
        self._req("POST", "/reset")

    def proxy(self, name: str, listen: str, upstream: str) -> None:
        try:
            self._req("DELETE", f"/proxies/{name}")
        except urllib.error.HTTPError:
            pass
        self._req("POST", "/proxies", {"name": name, "listen": listen,
                                       "upstream": upstream, "enabled": True})

    def latency(self, proxy: str, ms: int, *, name: str = "lat",
                stream: str = "downstream") -> None:
        self._req("POST", f"/proxies/{proxy}/toxics", {
            "name": name, "type": "latency", "stream": stream, "toxicity": 1.0,
            "attributes": {"latency": ms, "jitter": 0},
        })

    def remove_toxic(self, proxy: str, name: str) -> None:
        try:
            self._req("DELETE", f"/proxies/{proxy}/toxics/{name}")
        except urllib.error.HTTPError:
            pass


@pytest.fixture(scope="session")
def toxiproxy():
    if not PG_DSN:
        pytest.skip("AGENTOS_TEST_PG_DSN not set — network chaos needs real Postgres")
    tp = Toxiproxy(API)
    proc = None
    if not tp.alive():
        binary = shutil.which("toxiproxy-server")
        if not binary:
            pytest.skip("no Toxiproxy API and no toxiproxy-server binary")
        host, _, port = urlsplit(API).netloc.partition(":")
        proc = subprocess.Popen([binary, "-host", host or "127.0.0.1", "-port", port or "8474"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if tp.alive():
                break
            time.sleep(0.1)
        else:
            proc.kill()
            pytest.skip("toxiproxy-server did not come up")
    tp.reset()
    try:
        yield tp
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)


def proxied_dsn(dsn: str, client: str = CLIENT) -> str:
    """Rewrite the DSN's host:port to point at the Toxiproxy listener."""
    parts = urlsplit(dsn)
    userinfo, _, _ = parts.netloc.rpartition("@")
    netloc = f"{userinfo}@{client}" if userinfo else client
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
