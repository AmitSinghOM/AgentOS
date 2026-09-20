"""Request-body size boundary (production review 2026-09-20, A3).

FastAPI/uvicorn impose no body limit. Without one, any caller (asserted mode) or any agent-kind
token (bearer mode) can POST a multi-hundred-MB workflow definition and hold a request worker
while it is parsed. The limit is enforced on `Content-Length` before the body is read, and a
bodyful request that declares no length is refused so chunked encoding cannot bypass it.
"""
from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient


def _client(monkeypatch, limit: str | None = None):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", "asserted")
    for k in ("AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS", "AGENTOS_UI_DIR", "AGENTOS_MAX_BODY_BYTES"):
        monkeypatch.delenv(k, raising=False)
    if limit is not None:
        monkeypatch.setenv("AGENTOS_MAX_BODY_BYTES", limit)
    from dagentos.api import main
    importlib.reload(main)
    return TestClient(main.app), main


def test_default_limit_is_one_mebibyte(monkeypatch):
    _, main = _client(monkeypatch)
    assert main.max_body_bytes == 1024 * 1024


def test_oversized_declared_body_is_refused_before_parsing(monkeypatch):
    c, _ = _client(monkeypatch, limit="1024")
    big = {"name": "w", "nodes": [{"id": f"n{i}", "agent": "calc"} for i in range(200)]}
    raw = json.dumps(big).encode()
    assert len(raw) > 1024
    r = c.post("/workflows", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert "AGENTOS_MAX_BODY_BYTES" in r.json()["detail"]
    assert c.get("/workflows/w").status_code == 404, "nothing was stored"


def test_body_within_limit_is_unaffected(monkeypatch):
    c, _ = _client(monkeypatch, limit="1024")
    assert c.post("/agents", json={"name": "calc", "type": "echo"}).status_code == 201


def test_bodyful_request_without_content_length_is_refused(monkeypatch):
    c, _ = _client(monkeypatch, limit="1024")

    def chunks():
        yield b'{"name": "calc", '
        yield b'"type": "echo"}'

    r = c.post("/agents", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 411
    assert c.get("/agents/calc").status_code == 404


def test_bodyless_post_with_a_stray_content_type_reaches_the_route(monkeypatch):
    """Self-review of A3: `curl -X POST .../cancel -H 'Content-Type: application/json'` sends
    neither Content-Length nor chunked framing. That is no body, not an undeclared one."""
    c, _ = _client(monkeypatch, limit="1024")
    r = c.post("/runs/nope/cancel", headers={"Content-Type": "application/json"})
    assert r.status_code == 404, r.text          # the route answered, not the middleware


def test_get_and_ui_paths_carry_no_body_and_are_untouched(monkeypatch):
    c, _ = _client(monkeypatch, limit="1")
    assert c.get("/health").status_code == 200
    assert c.get("/runs").status_code == 200


def test_bad_limit_fails_startup_naming_the_variable(monkeypatch):
    import pytest
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_MAX_BODY_BYTES", "lots")
    from dagentos.api import main
    with pytest.raises(RuntimeError, match="AGENTOS_MAX_BODY_BYTES"):
        importlib.reload(main)
    monkeypatch.delenv("AGENTOS_MAX_BODY_BYTES")
    importlib.reload(main)          # leave the module importable for the next test
