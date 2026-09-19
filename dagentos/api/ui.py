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
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

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


class _HardenedStaticFiles(StaticFiles):
    """StaticFiles whose every file response carries UI_HEADERS (the mount bypasses the SPA
    handler, so the headers must be added here too)."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
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
