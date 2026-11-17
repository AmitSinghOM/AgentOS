# Security policy

AgentOS is a control plane: it records what ran, who approved it and what it cost, and it
holds the human-authority controls (approval, cancel, budget) that no executor can bypass.
A defect that lets an executor act outside its declared effects, lets a caller act as another
principal, or lets the log be altered without `agentos verify` noticing is a security issue.

## Reporting a vulnerability

Please report privately through GitHub's **Security Advisory** form for this repository
(*Security → Report a vulnerability*), not in a public issue or pull request. Include the
version (`pip show dagentos` or the image tag), the store adapter, the `AGENTOS_AUTH` mode,
and a reproduction if you have one.

You will get an acknowledgement within 7 days and a fix or a written assessment within
90 days. Coordinated disclosure after the fix ships is welcome; credit is given unless you
ask otherwise.

## Supported versions

The latest minor release receives fixes. Earlier minors are not patched; upgrade. Release
artifacts (wheels, sdists, the `ghcr.io/amitsinghom/agentos` image) carry GitHub build
provenance — verify with `gh attestation verify <file> -R AmitSinghOM/AgentOS`.

## Scope

In scope:

- authentication and authorisation (`dagentos.api.auth`, the operator policy ceiling, the
  approval gate);
- log integrity (hash chain, `integrity.sealed`, `agentos verify`);
- the built-in `tool` executor's egress and subprocess guards;
- request handling at the API boundary (body limits, validation, the SSE stream);
- the operator UI served at `/ui` (CSP, token handling);
- the published artifacts and their provenance.

Out of scope, by design and documented:

- `AGENTOS_AUTH=asserted` (the default for the quickstart) records principals **unverified**;
  the API logs a warning at startup and `GET /me` says so. Exposing an `asserted` API is a
  deployment mistake, not a vulnerability. Use `bearer` mode. To limit the blast radius of
  that mistake, asserted mode refuses (403) a `tool` agent that is `kind: subprocess`, reads
  `${NAME}` from the environment in `headers`/`env`, or sets `allow_private_networks`, unless
  `AGENTOS_ASSERTED_ALLOW_UNSAFE_TOOLS=1` is set deliberately.
- The open paths. In `bearer` mode every request needs a token except `/health`, `/ready` and
  `/metrics`, `GET`/`HEAD` of the UI's static files under `/ui` (no data lives there), and
  `/triggers/webhooks/*`, which verify their own HMAC credential and fail closed. `/metrics`
  is open so a Prometheus scraper needs no token, and that is a deliberate disclosure: its
  labels carry workflow names, step ids, agent names, meter names, currencies and summed
  cost per workflow — no inputs, outputs or principals, but aggregates with names are run
  data to an outsider. An operator who considers those names sensitive should front
  `/metrics` with a scrape token at the ingress or bind the API on loopback and scrape it
  from the same host.
- Model or tool output content: the core hashes executor output and never interprets it.
- Code a step runs with `execute_code` runs **with the worker's privileges** today; there is no
  sandbox. A `tool` subprocess gets an environment built from its own config only (`env` plus
  `PATH`), but it runs as the worker's uid: it can read any file the worker can (the SQLite
  store, the `AGENTOS_SIGNING_KEYS` keyring), reach any network the worker can, and connect to
  the store's DSN. The ACP agent process (`agentos-provider-acp`) also inherits the worker's
  whole environment, `AGENTOS_PG_DSN` and provider keys included, because it needs its own
  login. The approval gate decides *whether* such a step runs, not what it can reach once it
  does, and no sensitive-path check on its cwd or args exists yet. Until then: run the worker
  as a dedicated uid on a dedicated host or container that holds nothing else; keep
  `execute_code` out of the operator policy's `effect_ceiling` unless a workflow needs it
  (`examples/operator_policy.json` already leaves it out, so such a step is refused); and fix
  each tool's `argv` and `cwd` in the agent definition, where step inputs cannot change them.
  The OS sandbox is ROADMAP control-plane row 8, the sensitive-path check row 9.
- Denial of service against a deployment with no ingress rate limiting; rate limiting belongs
  at the ingress for this shape of service (see `docs/FAIL_MODES.md`).
- The development `docker-compose.yml` — it is a local stack, not a deployment. Every port is
  published on `127.0.0.1` only, the Postgres password comes from `.env` (the file carries no
  credential), and Grafana is anonymous **Viewer** over read-only provisioned dashboards
  (`tests/test_c13_mit_and_compose.py` locks all three). Do not publish it wider.

## Dependencies

Dependabot watches the core, each provider, the UI and the workflow actions weekly;
CI runs `pip-audit --strict` and `npm audit --audit-level=high --omit=dev` (the three runtime
dependencies that ship in `ui/dist`; the full dev tree is audited too and reported as a
warning) on every pull request, so a
known-vulnerable dependency fails the build rather than shipping.
