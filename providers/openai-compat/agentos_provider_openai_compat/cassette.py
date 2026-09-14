"""HTTP record/replay for provider tests (docs/DEVELOPMENT_STRUCTURE.md §11 A10).

A cassette is a JSON file of request→response interactions keyed by a hash of the
request (method, path, canonical body; auth headers excluded and never written). In
`replay` mode the transport answers from the cassette and never touches the network — a
plugin stays testable after the model it was recorded against is retired. In `record`
mode it forwards to the real server and appends what came back. A nightly opt-in job
re-records against a live Ollama; CI replays.

Deliberately small and dependency-free rather than vcrpy: the format is documented here,
readable by hand, and will still be readable when this package is archived.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

FORMAT = "agentos.cassette/1"
_REDACTED_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie"}


class CassetteMiss(RuntimeError):
    """Replay had no interaction for this request. The message says how to fix it."""


def request_key(method: str, url: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode()); h.update(b"\n")
    h.update(httpx.URL(url).raw_path); h.update(b"\n")
    try:  # canonicalise JSON so key order in the client never matters
        h.update(json.dumps(json.loads(body or b"{}"), sort_keys=True,
                            separators=(",", ":")).encode())
    except ValueError:
        h.update(body)
    return h.hexdigest()


class Cassette:
    def __init__(self, path: Path, *, source: str = "unknown"):
        self.path = Path(path)
        self.source = source
        self.interactions: dict[str, dict] = {}
        self.meta: dict = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("format") != FORMAT:
                raise ValueError(f"{self.path}: unknown cassette format {data.get('format')!r}")
            self.meta = {k: v for k, v in data.items() if k != "interactions"}
            self.interactions = {i["key"]: i for i in data["interactions"]}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": FORMAT,
            "source": self.source,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "interactions": [self.interactions[k] for k in sorted(self.interactions)],
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


class CassetteTransport(httpx.BaseTransport):
    """`mode="replay"` answers from the cassette; `mode="record"` forwards to `inner`
    and records. Auth headers are stripped from what is written."""

    def __init__(self, cassette: Cassette, mode: str, inner: httpx.BaseTransport | None = None):
        if mode not in ("replay", "record"):
            raise ValueError(f"mode must be replay | record, got {mode!r}")
        if mode == "record" and inner is None:
            inner = httpx.HTTPTransport()
        self.cassette, self.mode, self.inner = cassette, mode, inner
        self.hits = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        key = request_key(request.method, str(request.url), body)
        if self.mode == "replay":
            hit = self.cassette.interactions.get(key)
            if hit is None:
                raise CassetteMiss(
                    f"no recorded response in {self.cassette.path} for "
                    f"{request.method} {request.url.raw_path.decode()} (key {key[:12]}…). "
                    f"Re-record with AGENTOS_OPENAI_CASSETTES=record against a live server, "
                    f"or check that the request body is deterministic (temperature, seed).")
            self.hits += 1
            r = hit["response"]
            return httpx.Response(r["status"], headers=r.get("headers", {}),
                                  content=r["body"].encode(), request=request)
        assert self.inner is not None
        response = self.inner.handle_request(request)
        response.read()
        self.cassette.interactions[key] = {
            "key": key,
            "request": {
                "method": request.method,
                "path": request.url.raw_path.decode(),
                "headers": {k: v for k, v in request.headers.items()
                            if k.lower() not in _REDACTED_HEADERS},
                "body": body.decode("utf-8", "replace"),
            },
            "response": {
                "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items()
                            if k.lower() in ("content-type",)},
                "body": response.text,
            },
        }
        self.cassette.save()
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, request=request)
