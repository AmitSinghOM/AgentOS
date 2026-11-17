# ADR 0006 — Core depends on nothing

Status: accepted (decided 2026-09-13 at Phase 0 close; written down 2026-11-16 from
`docs/DEVELOPMENT_STRUCTURE.md` §1 and `ROADMAP.md` Phase 1 "Longevity structure").

## Context

`core/engine.py` once imported the store singleton and the echo agent directly
(`DEVELOPMENT_STRUCTURE.md` §0, finding E1). FastAPI, SQL drivers, Pydantic and every model
SDK are expected to have breaking majors within the project's horizon; a domain that imports
them is rewritten with them.

## Decision

`dagentos.core` holds the domain and the ports (`core/ports.py`, `core/coordination.py`) and
imports only the standard library and pydantic. It must not import `dagentos.api`,
`dagentos.store`, `dagentos.agents`, `dagentos.observability`, `dagentos.plugins`,
`dagentos.providerkit`, or any framework or vendor SDK. Adapters are injected at the
composition roots (API, worker, CLI).

## Consequences

- Every dependency change is a contained adapter rewrite, never a core rewrite.
- The rule is mechanical, not documentary: two `import-linter` contracts in `pyproject.toml`
  (`[tool.importlinter]`) run as `lint-imports` in CI and fail a PR that violates them.
- Pydantic inside the core is a tolerated exception, recorded in §4 of
  `DEVELOPMENT_STRUCTURE.md`.
