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
  deployment mistake, not a vulnerability. Use `bearer` mode.
- Model or tool output content: the core hashes executor output and never interprets it.
- Denial of service against a deployment with no ingress rate limiting; rate limiting belongs
  at the ingress for this shape of service (see `docs/FAIL_MODES.md`).
- The development `docker-compose.yml` (dev credentials, anonymous Grafana) — it is a local
  stack, not a deployment.

## Dependencies

Dependabot watches the core, each provider, the UI and the workflow actions weekly;
CI runs `pip-audit --strict` and `npm audit --audit-level=high` on every pull request, so a
known-vulnerable dependency fails the build rather than shipping.
