# The operator UI

Phase 9. A React app served by the API itself at `/ui`. Today it is one screen — the approvals
inbox — and the rules below are the ones every later screen (run graph, timeline, cost panel)
inherits.

## Run it

```bash
cd ui && npm ci && npm run build          # → ui/dist
uvicorn agentos.api.main:app              # mounts /ui when ui/dist/index.html exists
open http://localhost:8000/ui/
```

`AGENTOS_UI_DIR=<dir>` points the API at a bundle built elsewhere. Without a bundle `GET /ui` is
a 404 whose message says how to build — never a blank page.

Development: `cd ui && npm run dev` serves the app on `:5173` and proxies API paths to
`127.0.0.1:8000`, so the browser still sees one origin.

## What the inbox does

- Asks for a bearer token (when the API runs `AGENTOS_AUTH=bearer`), keeps it in the tab's
  `sessionStorage`, sends it as `Authorization: Bearer` on every request.
- Calls `GET /me` and shows **Acting as `<id>` (`<kind>`, `token:sha256:…`) — verified by the
  API** before any decision button is enabled. That line is what `approval.granted` will record.
- Polls `GET /approvals` every 3 s; each pending item shows the workflow, run, step, declared
  effect classes (or, for a cost approval, the rolling cost and the ceiling a grant would set),
  requested-at and expiry.
- Approve / Reject with a reason. The body is the API's own: `{"reason"}` in bearer mode; the
  API derives the principal and rejects a body principal (422). 401 / 403 / 409 / 422 text is
  shown verbatim.
- In `AGENTOS_AUTH=asserted` the banner says **Unverified**, explains that nothing checks the
  principal, and requires one to be typed before decisions are enabled; it is then sent in the
  body because the API requires it in that mode.

## What it deliberately does not do

- No client-side authorization. Whether an agent may approve `spend` is the engine's call.
- No token anywhere but `sessionStorage`: not `localStorage` (survives the tab), not a cookie
  (CSRF surface, a second auth path), not a query string (SSE would need one — so the inbox
  polls instead of streaming).
- No cross-origin calls. Same origin is why there is no CORS configuration to get wrong.

## The contract the API keeps for the UI

`agentos/api/ui.py` and `tests/test_ui_mount.py`:

- Only **GET/HEAD of static files under `/ui`** are exempt from authentication. A test enumerates
  every route under the prefix and fails if anything but the two UI handlers and the assets
  mount is there; mutations under `/ui` are never exempt.
- Traversal (`/ui/../…`) cannot escape the bundle directory.
- `/ui/<client route>` returns `index.html` (single-page routing); `/ui/assets/*` are plain
  files; `index.html` is `Cache-Control: no-cache` so a new build is picked up on reload.

## Tests

- `ui/src/App.test.tsx` — Vitest + Testing Library, `fetch` stubbed (three requests; MSW would
  be a dependency for nothing). One test per rule above, named for the rule.
- `tests/test_ui_mount.py` — the mount contract, run in CI against the real built bundle.

## Not yet

The bundle is not in the wheel or the image; that lands with the Phase 9 close-out when the
publish workflow gains a Node step. Run graph, event timeline and cost panel are their own
slices and will pick the UI kit.
