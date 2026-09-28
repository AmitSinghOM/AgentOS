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

from dagentos.api.ui import UI_PREFIX, is_ui_asset_path

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
    from dagentos.api import main
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


def test_every_ui_response_carries_the_security_headers(monkeypatch, bundle, tmp_path):
    """A1 (package review v0.11.0): the page holds a bearer token in sessionStorage and renders
    log-derived strings. CSP bounds any future injection (no inline script, no exfil via
    connect-src), frame-ancestors 'none' stops framing, nosniff stops MIME confusion. The
    bundle has no inline script/style, so 'self' is sufficient. All three response kinds — the
    app shell, a root file, an asset — must carry them; API routes must NOT (they are JSON)."""
    from dagentos.api.ui import UI_HEADERS
    main = _app(monkeypatch, mode="asserted", ui_dir=bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    assert "default-src 'self'" in UI_HEADERS["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in UI_HEADERS["Content-Security-Policy"]
    for path in ("/ui/", "/ui/runs/abc", "/ui/favicon.svg", "/ui/assets/app.js"):
        r = c.get(path)
        assert r.status_code == 200, path
        for k, v in UI_HEADERS.items():
            assert r.headers.get(k) == v, (path, k, r.headers.get(k))
    r = c.get("/health")
    assert "Content-Security-Policy" not in r.headers


# ---- precompressed assets (v0.17): negotiated at request time, compressed at build time ----

def _gz(data: bytes) -> bytes:
    import gzip
    return gzip.compress(data, mtime=0)


@pytest.fixture
def compressed_bundle(bundle):
    js = (bundle / "assets" / "app.js").read_bytes()
    (bundle / "assets" / "app.js.gz").write_bytes(_gz(js))
    # A fake brotli sibling: the server never decodes it, it only needs to serve the bytes
    # with the right headers, so any distinguishable payload proves which file was picked.
    (bundle / "assets" / "app.js.br").write_bytes(b"BR-PAYLOAD")
    (bundle / "assets" / "plain.css").write_text("body{}")           # no siblings at all
    return bundle


def _get(c, path, accept):
    # httpx adds its own `Accept-Encoding: gzip, deflate, br` when none is given, so "no
    # preference" is spelled as an explicit empty header here. It also decodes gzip bodies
    # transparently, which the gzip assertions below rely on.
    return c.get(path, headers={"Accept-Encoding": accept if accept is not None else ""})


def test_asset_negotiation_prefers_br_then_gzip_then_identity(monkeypatch, tmp_path, compressed_bundle):
    main = _app(monkeypatch, mode="asserted", ui_dir=compressed_bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    r = _get(c, "/ui/assets/app.js", "gzip, deflate, br, zstd")
    assert r.headers.get("content-encoding") == "br" and r.content == b"BR-PAYLOAD"
    r = _get(c, "/ui/assets/app.js", "gzip, deflate")
    assert r.headers.get("content-encoding") == "gzip"
    assert r.content == b"console.log('ui')"          # httpx decoded it: the bytes were valid gzip
    r = _get(c, "/ui/assets/app.js", "identity")
    assert "content-encoding" not in r.headers and r.content == b"console.log('ui')"
    r = _get(c, "/ui/assets/app.js", None)
    assert "content-encoding" not in r.headers


def test_compressed_variant_keeps_the_original_media_type_and_security_headers(
        monkeypatch, tmp_path, compressed_bundle):
    from dagentos.api.ui import UI_HEADERS
    main = _app(monkeypatch, mode="asserted", ui_dir=compressed_bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    r = _get(c, "/ui/assets/app.js", "br")
    assert r.headers["content-type"].startswith("text/javascript")   # not octet-stream for .br
    assert r.headers["vary"] == "Accept-Encoding"
    for k, v in UI_HEADERS.items():
        assert r.headers.get(k) == v, k
    # identity responses of a file that HAS variants must also say Vary, or a shared cache
    # could hand the plain bytes to a br client or vice versa
    assert _get(c, "/ui/assets/app.js", "identity").headers.get("vary") == "Accept-Encoding"


def test_asset_without_siblings_is_served_plain_regardless_of_accept(monkeypatch, tmp_path, compressed_bundle):
    main = _app(monkeypatch, mode="asserted", ui_dir=compressed_bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    r = _get(c, "/ui/assets/plain.css", "br, gzip")
    assert r.status_code == 200 and "content-encoding" not in r.headers and r.text == "body{}"


def test_sibling_files_are_not_addressable_by_their_own_name_with_wrong_type(
        monkeypatch, tmp_path, compressed_bundle):
    """Asking for app.js.br directly is allowed (it is a file in the bundle) but must come back
    as what it is - an opaque encoded blob - not as text/javascript without Content-Encoding,
    which a browser would try to execute."""
    main = _app(monkeypatch, mode="asserted", ui_dir=compressed_bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    r = _get(c, "/ui/assets/app.js.br", "br")
    assert r.status_code == 200
    assert not r.headers["content-type"].startswith("text/javascript")


def test_conditional_requests_still_work_on_compressed_variants(monkeypatch, tmp_path, compressed_bundle):
    main = _app(monkeypatch, mode="asserted", ui_dir=compressed_bundle, tmp_path=tmp_path)
    c = TestClient(main.app)
    first = _get(c, "/ui/assets/app.js", "br")
    etag = first.headers["etag"]
    again = c.get("/ui/assets/app.js", headers={"Accept-Encoding": "br", "If-None-Match": etag})
    assert again.status_code == 304
    # the gzip variant is a different file, so its ETag differs and the br ETag must not match it
    other = c.get("/ui/assets/app.js", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert other.status_code == 200


# ---- the release bundler refuses a build whose precompressed siblings are missing or wrong ----

def _fake_dist(tmp_path, *, br=True, gz=True, gz_ok=True, br_small=True):
    import gzip
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    js = ("console.log('x');\n" * 200).encode()      # compressible, > 1 KiB
    (d / "assets" / "index-abc.js").write_bytes(js)
    (d / "index.html").write_text('<script src="/ui/assets/index-abc.js"></script>')
    if br:
        (d / "assets" / "index-abc.js.br").write_bytes(b"x" * (10 if br_small else len(js) + 1))
    if gz:
        (d / "assets" / "index-abc.js.gz").write_bytes(
            gzip.compress(js) if gz_ok else gzip.compress(js + b"tampered"))
    return d


def test_bundler_accepts_a_complete_build(tmp_path):
    from scripts.bundle_ui import _validate
    assert _validate(_fake_dist(tmp_path)) == []


@pytest.mark.parametrize("kw,needle", [
    ({"br": False}, ".br missing"),
    ({"gz": False}, ".gz missing"),
    ({"gz_ok": False}, "does not decompress to"),
    ({"br_small": False}, "is not smaller than"),
])
def test_bundler_refuses_bad_siblings(tmp_path, kw, needle):
    from scripts.bundle_ui import _validate
    problems = _validate(_fake_dist(tmp_path, **kw))
    assert any(needle in p for p in problems), problems
