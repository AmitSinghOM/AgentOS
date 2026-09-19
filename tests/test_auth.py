"""Phase 8 #1 — the credential decides who the caller is; the body may not.

Threat model, one test each (docs/TRUST_BOUNDARY.md §1a):
  * anonymous caller in bearer mode → 401 on every path except /health and /metrics
  * unknown token → 401, logged with the hash prefix, never the token
  * a body `principal` in bearer mode → 422 (rejected, not silently replaced)
  * the recorded Principal on approval.granted / run.* events is the TOKEN's, with an attestation
    naming the credential
  * an agent-kind token cannot approve a human-only step (403, no event appended) and
    cannot register agents or define workflows (403, nothing stored)
  * the token file holds hashes; a plaintext `token` key fails startup with the fix
  * bearer mode without a token file fails startup naming the variable
  * asserted mode (default) warns once at startup and keeps today's behaviour
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dagentos.api import auth as auth_mod
from dagentos.api.auth import (
    AuthConfig,
    AuthError,
    AuthMode,
    StaticTokenAuthenticator,
    token_digest,
)
from dagentos.core.models import PrincipalKind

HUMAN_TOKEN = "human-secret-for-tests-only"
AGENT_TOKEN = "agent-secret-for-tests-only"
HUMAN_BODY = {"principal": {"kind": "human", "id": "amit"}, "reason": "ok"}


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps({"principals": [
        {"sha256": _sha(HUMAN_TOKEN), "kind": "human", "id": "amit"},
        {"sha256": _sha(AGENT_TOKEN).upper(), "kind": "agent", "id": "planner-bot"},
    ]}))
    return p


def _app(monkeypatch, *, mode: str, tokens=None):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", mode)
    if tokens is not None:
        monkeypatch.setenv("AGENTOS_AUTH_TOKENS", str(tokens))
    else:
        monkeypatch.delenv("AGENTOS_AUTH_TOKENS", raising=False)
    from dagentos.api import main
    importlib.reload(main)
    return main


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _suspended_run(main, c: TestClient, headers: dict) -> tuple[str, str]:
    """Register a spend-declaring agent and start a run that suspends on approval."""
    from tests.test_approvals import RecordingExecutor

    main.engine._executors = {"echo": RecordingExecutor()}
    assert c.post("/agents", json={"name": "payer", "type": "echo",
                                   "declared_effects": ["compute", "spend"]},
                  headers=headers).status_code == 201
    assert c.post("/workflows", json={"name": "w", "nodes": [{"id": "pay", "agent": "payer"}]},
                  headers=headers).status_code == 201
    run = c.post("/workflows/w/runs", params={"sync": "true"}, headers=headers).json()
    assert run["status"] == "suspended"
    aid = next(iter(run["approvals"]))
    return run["id"], aid


# ------------------------------------------------------------------ the authenticator

def test_token_file_holds_hashes_and_resolves_case_insensitively(token_file):
    a = StaticTokenAuthenticator.from_file(token_file)
    human = a.authenticate(HUMAN_TOKEN)
    agent = a.authenticate(AGENT_TOKEN)              # stored upper-case → still matches
    assert human is not None and human.kind is PrincipalKind.human and human.id == "amit"
    assert agent is not None and agent.kind is PrincipalKind.agent
    assert human.attestation == f"token:sha256:{_sha(HUMAN_TOKEN)[:12]}"
    assert a.authenticate("not-a-token") is None
    assert a.authenticate("") is None
    assert token_digest(HUMAN_TOKEN) == _sha(HUMAN_TOKEN)


def test_a_plaintext_token_key_is_refused_with_the_fix(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"principals": [{"token": "x", "kind": "human", "id": "a"}]}))
    with pytest.raises(AuthError, match=r"principals\[0\] carries a plaintext 'token'.*sha256sum"):
        StaticTokenAuthenticator.from_file(p)


@pytest.mark.parametrize("payload, needle", [
    ({"principals": []}, "is empty"),
    ({"principals": [{"sha256": "abc", "kind": "human", "id": "a"}]}, "64 hex"),
    ({"principals": [{"sha256": "0" * 64, "kind": "robot", "id": "a"}]}, "principals.0.kind"),
    ({"principals": [{"sha256": "0" * 64, "kind": "human", "id": "a", "scopes": []}]},
     "principals.0.scopes"),
    ({"tokens": []}, "principals"),
    ([], "<root>"),
])
def test_malformed_token_files_fail_naming_the_entry(tmp_path, payload, needle):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(AuthError, match=needle):
        StaticTokenAuthenticator.from_file(p)


def test_missing_or_invalid_file_names_the_variable(tmp_path):
    with pytest.raises(AuthError, match="AGENTOS_AUTH_TOKENS=.*file not found"):
        StaticTokenAuthenticator.from_file(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(AuthError, match="AGENTOS_AUTH_TOKENS=.*not valid JSON"):
        StaticTokenAuthenticator.from_file(bad)


# ------------------------------------------------------------------ configuration

def test_default_mode_is_asserted_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("AGENTOS_AUTH", raising=False)
    with caplog.at_level(logging.WARNING, logger="agentos.api.auth"):
        cfg = AuthConfig.from_env()
    assert cfg.mode is AuthMode.asserted and not cfg.enforced and cfg.authenticator is None
    warnings = [r for r in caplog.records if "NOT verified" in r.getMessage()]
    assert len(warnings) == 1 and "AGENTOS_AUTH=bearer" in warnings[0].getMessage()


def test_bearer_without_a_token_file_fails_startup_naming_the_variable(monkeypatch):
    monkeypatch.setenv("AGENTOS_AUTH", "bearer")
    monkeypatch.delenv("AGENTOS_AUTH_TOKENS", raising=False)
    with pytest.raises(AuthError, match="AGENTOS_AUTH=bearer requires AGENTOS_AUTH_TOKENS"):
        AuthConfig.from_env()


def test_unknown_mode_fails_startup(monkeypatch):
    monkeypatch.setenv("AGENTOS_AUTH", "jwt")
    with pytest.raises(AuthError, match="asserted \\| bearer, got 'jwt'"):
        AuthConfig.from_env()


# ------------------------------------------------------------------ the boundary, end to end

def test_anonymous_caller_is_401_everywhere_except_probes(monkeypatch, token_file, caplog):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    assert c.get("/health").status_code == 200
    assert c.get("/metrics").status_code in (200, 404)    # 404 when Prometheus is off
    with caplog.at_level(logging.WARNING, logger="agentos.api.auth"):
        for method, path in [("get", "/agents"), ("get", "/approvals"), ("get", "/runs/x"),
                             ("get", "/runs/x/events"), ("get", "/runs/x/stream"),
                             ("get", "/blobs/" + "0" * 64), ("get", "/executors"),
                             ("post", "/agents"), ("post", "/workflows"),
                             ("post", "/workflows/w/runs"), ("post", "/runs/x/cancel"),
                             ("post", "/runs/x/approvals/y/approve")]:
            r = c.post(path, json={}) if method == "post" else c.get(path)
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.json() == {"detail": "missing bearer token"}
    auth_records = [r for r in caplog.records if r.name == "agentos.api.auth"]
    assert auth_records and all("missing bearer token" in r.getMessage() for r in auth_records)
    assert not any("Basic" in r.getMessage() for r in auth_records)

    # a token with the wrong scheme is anonymous too
    assert c.get("/agents", headers={"Authorization": f"Basic {HUMAN_TOKEN}"}).status_code == 401


def test_unknown_token_is_401_and_logged_by_hash_prefix_only(monkeypatch, token_file, caplog):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    with caplog.at_level(logging.WARNING, logger="agentos.api.auth"):
        r = c.get("/agents", headers=_bearer("wrong-token"))
    assert r.status_code == 401 and r.json()["detail"] == "unknown bearer token"
    line = caplog.records[-1].getMessage()
    assert f"token:sha256:{_sha('wrong-token')[:12]}" in line
    assert "wrong-token" not in line and "GET /agents" in line


def test_recorded_principal_comes_from_the_token_not_the_body(monkeypatch, token_file):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    rid, aid = _suspended_run(main, c, _bearer(HUMAN_TOKEN))

    # A body principal is rejected — not ignored — in bearer mode.
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve", json=HUMAN_BODY,
               headers=_bearer(HUMAN_TOKEN))
    assert r.status_code == 422 and "derived from the bearer token" in r.json()["detail"]
    assert c.get(f"/runs/{rid}/approvals", headers=_bearer(HUMAN_TOKEN)) \
        .json()["data"][0]["status"] == "pending"

    # A body claiming to be a different human is rejected the same way.
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve",
               json={"principal": {"kind": "human", "id": "someone-else"}},
               headers=_bearer(HUMAN_TOKEN))
    assert r.status_code == 422

    r = c.post(f"/runs/{rid}/approvals/{aid}/approve", params={"sync": "true"},
               json={"reason": "within budget"}, headers=_bearer(HUMAN_TOKEN))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    events = c.get(f"/runs/{rid}/events", headers=_bearer(HUMAN_TOKEN)).json()["data"]
    decided = [e for e in events if e["event_type"] == "approval.granted"]
    assert len(decided) == 1
    p = decided[0]["principal"]
    assert p == {"kind": "human", "id": "amit",
                 "attestation": f"token:sha256:{_sha(HUMAN_TOKEN)[:12]}"}
    assert decided[0]["reason"] == "within budget"


def test_control_events_carry_the_token_principal(monkeypatch, token_file):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    h = _bearer(HUMAN_TOKEN)
    rid, _ = _suspended_run(main, c, h)
    assert c.post(f"/runs/{rid}/cancel", json=HUMAN_BODY, headers=h).status_code == 422
    assert c.post(f"/runs/{rid}/cancel", json={"reason": "enough"}, headers=h).status_code == 202
    events = c.get(f"/runs/{rid}/events", headers=h).json()["data"]
    req = [e for e in events if e["event_type"] == "run.cancel_requested"]
    assert req and req[0]["principal"]["id"] == "amit" \
        and req[0]["principal"]["attestation"].startswith("token:sha256:")


def test_agent_token_cannot_approve_a_human_only_step(monkeypatch, token_file):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    rid, aid = _suspended_run(main, c, _bearer(HUMAN_TOKEN))
    before = len(c.get(f"/runs/{rid}/events", headers=_bearer(HUMAN_TOKEN)).json()["data"])
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve", json={"reason": "I approve myself"},
               headers=_bearer(AGENT_TOKEN))
    assert r.status_code == 403
    after = c.get(f"/runs/{rid}/events", headers=_bearer(HUMAN_TOKEN)).json()["data"]
    assert len(after) == before                       # nothing appended
    assert c.get(f"/runs/{rid}/approvals", headers=_bearer(AGENT_TOKEN)) \
        .json()["data"][0]["status"] == "pending"     # an agent may still READ


def test_agent_token_cannot_register_agents_or_define_workflows(monkeypatch, token_file, caplog):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    with caplog.at_level(logging.WARNING, logger="agentos.api.auth"):
        r1 = c.post("/agents", json={"name": "sneaky", "type": "echo",
                                     "declared_effects": ["compute"]}, headers=_bearer(AGENT_TOKEN))
        r2 = c.post("/workflows", json={"name": "w", "nodes": [{"id": "n", "agent": "sneaky"}]},
                    headers=_bearer(AGENT_TOKEN))
    assert r1.status_code == r2.status_code == 403
    assert "may not register agents or define workflows" in r1.json()["detail"]
    assert c.get("/agents", headers=_bearer(AGENT_TOKEN)).json()["data"] == []
    assert any("auth 403 on POST /agents" in r.getMessage() for r in caplog.records)
    # ...but an agent may start a run of an operator-defined workflow
    assert c.post("/agents", json={"name": "calc", "type": "echo"},
                  headers=_bearer(HUMAN_TOKEN)).status_code == 201
    assert c.post("/workflows", json={"name": "w", "nodes": [{"id": "n", "agent": "calc"}]},
                  headers=_bearer(HUMAN_TOKEN)).status_code == 201
    assert c.post("/workflows/w/runs", headers=_bearer(AGENT_TOKEN)).status_code == 202


def test_asserted_mode_still_requires_a_body_principal_on_decisions(monkeypatch):
    """A2 unchanged in asserted mode: approve/reject without a principal is 422; control
    endpoints accept an absent principal (recorded as None, as before)."""
    main = _app(monkeypatch, mode="asserted")
    c = TestClient(main.app)
    rid, aid = _suspended_run(main, c, {})
    assert c.post(f"/runs/{rid}/approvals/{aid}/approve", json={"reason": "x"}).status_code == 422
    assert c.post(f"/runs/{rid}/approvals/{aid}/reject", json={}).status_code == 422
    r = c.post(f"/runs/{rid}/approvals/{aid}/approve", json=HUMAN_BODY)
    assert r.status_code == 202
    assert c.post(f"/runs/{rid}/pause", json={"reason": "no principal"}).status_code == 202
    events = c.get(f"/runs/{rid}/events").json()["data"]
    decided = next(e for e in events if e["event_type"] == "approval.granted")
    assert decided["principal"] == {"kind": "human", "id": "amit", "attestation": None}
    paused = next(e for e in events if e["event_type"] == "run.pause_requested")
    assert paused["principal"] is None


def test_every_route_is_covered_by_the_middleware_not_a_dependency(monkeypatch, token_file):
    """Fail-closed by construction: a route added tomorrow is protected without remembering
    a `Depends`. Prove it by mounting a route after import and calling it anonymously."""
    main = _app(monkeypatch, mode="bearer", tokens=token_file)

    @main.app.get("/added-later")
    def added_later() -> dict:
        return {"ok": True}

    c = TestClient(main.app)
    assert c.get("/added-later").status_code == 401
    assert c.get("/added-later", headers=_bearer(AGENT_TOKEN)).json() == {"ok": True}


def test_open_paths_are_exactly_health_and_metrics():
    assert auth_mod.OPEN_PATHS == frozenset({"/health", "/metrics"})
    assert auth_mod.DEFINITION_PATHS == frozenset({"/agents", "/workflows"})


def test_reject_in_bearer_mode_records_the_token_principal(monkeypatch, token_file):
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    h = _bearer(HUMAN_TOKEN)
    rid, aid = _suspended_run(main, c, h)
    assert c.post(f"/runs/{rid}/approvals/{aid}/reject", json=HUMAN_BODY, headers=h).status_code == 422
    r = c.post(f"/runs/{rid}/approvals/{aid}/reject", json={"reason": "no"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    events = c.get(f"/runs/{rid}/events", headers=h).json()["data"]
    rejected = next(e for e in events if e["event_type"] == "approval.rejected")
    assert rejected["principal"]["id"] == "amit" \
        and rejected["principal"]["attestation"] == f"token:sha256:{_sha(HUMAN_TOKEN)[:12]}"


def test_openapi_declares_the_bearer_scheme_only_in_bearer_mode(monkeypatch, token_file):
    """The served contract must say the header is required, or a generated client sends
    nothing and `/docs` has no Authorize button. Probes stay `security: []`."""
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    assert c.get("/openapi.json").status_code == 401          # the document itself is protected
    doc = c.get("/openapi.json", headers=_bearer(AGENT_TOKEN)).json()
    assert doc["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http", "scheme": "bearer",
        "description": "Token from the operator's AGENTOS_AUTH_TOKENS file (dagentos/api/auth.py)."}
    assert doc["security"] == [{"bearerAuth": []}]
    assert doc["paths"]["/health"]["get"]["security"] == []
    assert "security" not in doc["paths"]["/agents"]["post"]   # inherits the global requirement

    main = _app(monkeypatch, mode="asserted")
    doc = TestClient(main.app).get("/openapi.json").json()
    assert "securitySchemes" not in doc.get("components", {}) and "security" not in doc


@pytest.mark.parametrize("path", ["/agents/", "//agents", "/%61gents", "/agents?x=1"])
def test_path_variants_do_not_bypass_the_definition_rule(monkeypatch, token_file, path):
    """DEFINITION_PATHS is an exact match on the decoded path; a variant must either be
    refused 403 or never reach the handler (redirect resolves to the exact path, or 404).
    Either way nothing is stored."""
    main = _app(monkeypatch, mode="bearer", tokens=token_file)
    c = TestClient(main.app)
    r = c.post(path, json={"name": "sneaky", "type": "echo"}, headers=_bearer(AGENT_TOKEN))
    assert r.status_code in (403, 404), (path, r.status_code)
    assert c.get("/agents", headers=_bearer(HUMAN_TOKEN)).json()["data"] == []
