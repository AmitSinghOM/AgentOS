"""The operator UI (Phase 9), served from the API's own origin under `/ui`.

Why same-origin: no CORS surface, and the bearer token the browser holds is never a
cross-origin credential. Why a separate module: the API must know exactly what it exposes
without a token — static FILES under `/ui`, nothing else. `is_ui_asset_path` is the one rule
the auth middleware consults, and `tests/test_ui_mount.py` proves no API route lives under it.

The bundle is `ui/dist` (Vite build; see docs/UI.md). `AGENTOS_UI_DIR` overrides the location.
When the directory is absent the mount is skipped and `GET /ui` answers 404 with the build
command, so a missing bundle is a clear message, not a blank page.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import stat
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse, StaticFiles

logger = logging.getLogger("agentos.api.ui")

UI_PREFIX = "/ui"

#: Sent on every response under /ui (shell, root files, assets) and on nothing else. The bundle
#: has no inline script or style (Vite emits one module script and one stylesheet; React sets
#: styles through the CSSOM, which CSP does not govern), so 'self' is sufficient. The page holds
#: a bearer token in sessionStorage and renders log-derived strings — React escapes them, and
#: this is the control that bounds a future injection: no foreign script, no exfil via
#: connect-src, no framing of the approve button. `tests/test_ui_mount.py` asserts all three
#: response kinds carry these and API routes do not.
UI_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


#: Encodings we will serve from a precompressed sibling, best first. The build writes
#: `<asset>.br` and `<asset>.gz` next to every text asset (ui/scripts/precompress.mjs, run by
#: `npm run build`); the server never compresses at request time, so a hot path costs one stat.
#: zstd is deliberately absent: Python's stdlib cannot produce it before 3.14 and the extra
#: saving over brotli on JS/CSS is within noise.
PRECOMPRESSED: tuple[tuple[str, str], ...] = (("br", ".br"), ("gzip", ".gz"))


def _accepted_encodings(scope) -> set[str]:
    """Tokens of `Accept-Encoding` with a non-zero q-value, lowercased. Absent header = identity."""
    raw = b",".join(v for k, v in scope.get("headers", ()) if k == b"accept-encoding").decode(
        "latin-1")
    out: set[str] = set()
    for part in raw.split(","):
        token, _, params = part.strip().partition(";")
        if not token:
            continue
        q = 1.0
        for p in params.split(";"):
            k, _, v = p.strip().partition("=")
            if k.strip().lower() == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        if q > 0:
            out.add(token.strip().lower())
    return out


class _HardenedStaticFiles(StaticFiles):
    """StaticFiles whose every file response carries UI_HEADERS (the mount bypasses the SPA
    handler, so the headers must be added here too), and which serves a precompressed sibling
    (`app.js.br`, `app.js.gz`) when the client accepts that encoding.

    Why at the mount and not a middleware: a compressing middleware buffers the body and would
    have to be excluded from `/runs/{id}/stream` (SSE) by hand; here nothing is compressed at
    request time, the negotiation is a stat, and the API is untouched.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        if scope["method"] in ("GET", "HEAD"):
            accepted = _accepted_encodings(scope)
            has_variant = False
            for encoding, ext in PRECOMPRESSED:
                try:
                    full, st = await anyio.to_thread.run_sync(self.lookup_path, path + ext)
                except (OSError, ValueError):
                    continue
                if not (st and stat.S_ISREG(st.st_mode)):
                    continue
                has_variant = True
                if encoding in accepted:
                    response = self._variant_response(full, st, scope, original=path,
                                                      encoding=encoding)
                    response.headers.update(UI_HEADERS)
                    return response
            if has_variant:
                response = await super().get_response(path, scope)
                response.headers["Vary"] = "Accept-Encoding"
                return response
        return await super().get_response(path, scope)

    def _variant_response(self, full, st, scope, *, original: str, encoding: str) -> Response:
        media_type, _ = mimetypes.guess_type(original)
        response = FileResponse(full, stat_result=st, media_type=media_type or "text/plain",
                                headers={"Content-Encoding": encoding, "Vary": "Accept-Encoding"})
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response

    def file_response(self, full_path, stat_result, scope, status_code: int = 200):  # type: ignore[override]
        # `mimetypes` treats .br/.gz as an *encoding* suffix and reports the inner type, so a
        # direct GET of `app.js.br` would be typed text/javascript with no Content-Encoding and
        # a browser would try to execute the compressed bytes. Siblings fetched by their own
        # name are opaque blobs.
        if str(full_path).endswith(tuple(ext for _, ext in PRECOMPRESSED)):
            response = FileResponse(full_path, status_code=status_code, stat_result=stat_result,
                                    media_type="application/octet-stream")
            if self.is_not_modified(response.headers, Headers(scope=scope)):
                response = NotModifiedResponse(response.headers)
        else:
            response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.update(UI_HEADERS)
        return response


#: Development bundle (a checkout with `ui/` built) and the packaged bundle (copied into the
#: wheel as `dagentos/_ui` by scripts/bundle_ui.py during the release build). The env var wins,
#: then the checkout, then the package — so a developer's fresh build is never shadowed by
#: whatever version was installed.
DEFAULT_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
PACKAGED_DIST = Path(__file__).resolve().parent.parent / "_ui"


def is_ui_asset_path(method: str, path: str) -> bool:
    """The auth exemption: GET/HEAD of `/ui` or anything below it. Mutations are never exempt."""
    if method not in ("GET", "HEAD"):
        return False
    return path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def ui_dir() -> Path | None:
    raw = os.environ.get("AGENTOS_UI_DIR")
    candidates = [Path(raw)] if raw else [DEFAULT_DIST, PACKAGED_DIST]
    for d in candidates:
        if (d / "index.html").is_file():
            return d
    return None


def mount_ui(app: FastAPI) -> Path | None:
    """Serve the built bundle. Assets under `/ui/assets/*` are plain static files; every other
    path under `/ui` returns `index.html` (single-page app routing). Returns the directory
    served, or None when there is no bundle."""
    d = ui_dir()
    if d is None:
        @app.get(UI_PREFIX, include_in_schema=False)
        @app.get(UI_PREFIX + "/{rest:path}", include_in_schema=False)
        def _no_bundle(rest: str = "") -> None:
            raise HTTPException(status_code=404, detail=(
                "the operator UI is not built. Run `npm ci && npm run build` in ui/ (or set "
                "AGENTOS_UI_DIR to a built bundle) and restart the API."))
        logger.info("operator UI not mounted: no bundle at %s or %s", DEFAULT_DIST, PACKAGED_DIST)
        return None

    index = d / "index.html"
    assets = d / "assets"
    if assets.is_dir():
        app.mount(UI_PREFIX + "/assets", _HardenedStaticFiles(directory=str(assets)), name="ui-assets")

    @app.get(UI_PREFIX, include_in_schema=False)
    @app.get(UI_PREFIX + "/{rest:path}", include_in_schema=False)
    def _spa(request: Request, rest: str = "") -> FileResponse:
        # Files at the bundle root (favicon, manifest) are served by name; anything else is
        # the app shell. Traversal cannot escape `d`: the resolved path must stay inside it.
        candidate = (d / rest).resolve() if rest else index
        if rest and candidate.is_file() and d.resolve() in candidate.parents:
            return FileResponse(candidate, headers=UI_HEADERS)
        return FileResponse(index, headers={"Cache-Control": "no-cache", **UI_HEADERS})

    logger.info("operator UI mounted at %s from %s", UI_PREFIX, d)
    return d
