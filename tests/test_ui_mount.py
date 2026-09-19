"""Phase 9 — the operator UI mount and `GET /me`.

Threat model for serving a UI from the API's origin:
  * only static FILES under /ui are reachable without a token; no API route may live there
  * traversal (`/ui/../pyproject.toml`) cannot escape the bundle directory
  * mutations under /ui are never exempt from auth
  * without a bundle, /ui is a 404 that says how to build — not a blank page, not a 500
  * /me tells the UI exactly what the log will record: the token's principal in bearer mode,
    null + mode in asserted mode (so the UI must label a typed principal "unverified")
"""
from __future__ import annotations

import importlib
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agentos.api.ui import UI_PREFIX, is_ui_asset_path

TOKEN = "ui-test-token"


@pytest.fixture
def bundle(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root>agentos ui</div>")
    (d / "assets" / "app.js").write_text("console.log('ui')")
    (d / "favicon.svg").write_text("<svg/>")
    return d


def _app(monkeypatch, *, mode: str, ui_dir=None, tmp_path=None):
    monkeypatch.setenv("AGENTOS_STORE", "memory")
    monkeypatch.setenv("AGENTOS_AUTH", mode)
    monkeypatch.delenv("AGENTOS_POLICY", raising=False)
    monkeypatch.delenv("AGENTOS_SIGNING_KEYS", raising=False)
    if mode == "bearer":
        tokens = tmp_path / "tokens.json"
        tokens.write_text(json.dumps({"principals": [
            {"sha256": sha256(TOKEN.encode()).hexdigest(), "kind": "human", "id": "amit"}]}))
        monkeypatch.setenv("AGENTOS_AUTH_TOKENS", str(tokens))
    if ui_dir is not None:
        monkeypatch.setenv("AGENTOS_UI_DIR", str(ui_dir))
    else:
        monkeypatch.setenv("AGENTOS_UI_DIR", str(tmp_path / "absent"))
    from agentos.api import main
    importlib.reload(main)
    return main


def test_exemption_rule_is_get_head_under_the_prefix_only():
    assert is_ui_asset_path("GET", "/ui") and is_ui_asset_path("GET", "/ui/assets/app.js")
    assert is_ui_asset_path("HEAD", "/ui/")
    assert not is_ui_asset_path("POST", "/ui") and not is_ui_asset_path("GET", "/uix")
    assert not is_ui_asset_path("GET", "/runs/ui") and not is_ui_asset_path("DELETE", "/ui/x")


def test_bundle_is_served_without_a_token_and_nothing_else_is(monkeypatch, tmp_path, bundle):
    main = _app(monkeypatch, mode="bearer", ui_dir=bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    assert c.get("/ui").status_code == 200 and "agentos ui" in c.get("/ui").text
    assert c.get("/ui/").status_code == 200
    assert c.get("/ui/some/client/route").text == c.get("/ui").text      # SPA fallback
    assert c.get("/ui/assets/app.js").text == "console.log('ui')"
    assert c.get("/ui/favicon.svg").text == "<svg/>"
    assert c.get("/ui").headers["cache-control"] == "no-cache"
    # data still needs a token
    assert c.get("/approvals").status_code == 401
    assert c.get("/me").status_code == 401
    assert c.post("/ui", json={}).status_code in (401, 405)


def test_traversal_cannot_escape_the_bundle(monkeypatch, tmp_path, bundle):
    (tmp_path / "secret.txt").write_text("not for you")
    main = _app(monkeypatch, mode="bearer", ui_dir=bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    for path in ("/ui/../secret.txt", "/ui/%2e%2e/secret.txt", "/ui/assets/../../secret.txt"):
        r = c.get(path)
        assert "not for you" not in r.text, path
        assert r.status_code in (200, 401, 404)    # the shell, the auth wall, or not found — never the file


def test_no_api_route_lives_under_the_ui_prefix(monkeypatch, tmp_path, bundle):
    """The exemption is only safe while this holds. Every route under /ui must be one of the
    two UI handlers or the assets mount."""
    main = _app(monkeypatch, mode="bearer", ui_dir=bundle, tmp_path=tmp_path)
    under = [r for r in main.app.routes if getattr(r, "path", "").startswith(UI_PREFIX)]
    names = sorted(getattr(r, "name", "") for r in under)
    assert names == ["_spa", "_spa", "ui-assets"], names
    for r in under:
        methods = getattr(r, "methods", None)
        assert methods is None or methods <= {"GET", "HEAD"}, (r.path, methods)


def test_without_a_bundle_ui_is_a_404_that_says_how_to_build(monkeypatch, tmp_path):
    main = _app(monkeypatch, mode="asserted", tmp_path=tmp_path)
    c = TestClient(main.app)
    r = c.get("/ui")
    assert r.status_code == 404 and "npm run build" in r.json()["detail"]
    assert c.get("/ui/anything").status_code == 404


def test_me_reports_the_recorded_principal_per_mode(monkeypatch, tmp_path):
    main = _app(monkeypatch, mode="bearer", tmp_path=tmp_path)
    c = TestClient(main.app)
    assert c.get("/me").status_code == 401
    body = c.get("/me", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body["mode"] == "bearer"
    assert body["principal"]["kind"] == "human" and body["principal"]["id"] == "amit"
    assert body["principal"]["attestation"].startswith("token:sha256:")
    main = _app(monkeypatch, mode="asserted", tmp_path=tmp_path)
    assert TestClient(main.app).get("/me").json() == {"mode": "asserted", "principal": None}
