"""Production pass 2, A4: readiness is a store round-trip; liveness is not.

`/health` answers 200 as long as the process is up (liveness). `/ready` performs one real
store query and answers 503 while the store cannot be reached, so an orchestrator stops
routing traffic to an API whose database is down instead of letting every request 500.
The 503 body names the exception CLASS only — the message (which may carry a DSN or a
host name) goes to the log.
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from dagentos.store.memory import MemoryStore


def _app(monkeypatch, **env):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.delenv("AGENTOS_AUTH_TOKENS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from dagentos.api import main
    importlib.reload(main)
    return main


class _Down(MemoryStore):
    def read_events(self, run_id, after_seq=0, limit=None):
        raise ConnectionError("connection to server at \"db.internal\" (10.0.0.7) failed")


def test_ready_is_200_when_the_store_answers(monkeypatch):
    main = _app(monkeypatch)
    c = TestClient(main.app)
    r = c.get("/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}


def test_ready_is_503_with_the_exception_class_only_when_the_store_is_down(monkeypatch, caplog):
    main = _app(monkeypatch)
    monkeypatch.setattr(main, "store", _Down())
    c = TestClient(main.app)
    with caplog.at_level("WARNING", logger="agentos.api"):
        r = c.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "store": "ConnectionError"}
    assert "db.internal" not in r.text and "10.0.0.7" not in r.text   # never in the body
    assert "db.internal" in caplog.text                                # but in the log
    assert c.get("/health").status_code == 200                         # liveness unaffected


def test_ready_needs_no_token_in_bearer_mode(monkeypatch, tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text('{"principals": [{"sha256": "%s", "kind": "human", "id": "probe-owner"}]}'
                      % ("0" * 64))
    main = _app(monkeypatch, AGENTOS_AUTH="bearer", AGENTOS_AUTH_TOKENS=str(tokens))
    c = TestClient(main.app)
    assert c.get("/ready").status_code == 200
    assert c.get("/runs").status_code == 401   # the wall still stands for everything else
