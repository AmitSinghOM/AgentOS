# ADR 0008 — Providers are plugins

Status: accepted (decided 2026-09-13, Phase 1 carry-forward; shipped 2026-09-14, Phase 4;
written down 2026-11-16 from `docs/DEVELOPMENT_STRUCTURE.md` §2.2 and `ROADMAP.md` Phase 4).

## Context

Phase 1 originally planned a "real LLM agent (Bedrock or OpenAI)" inside the core
(`DEVELOPMENT_STRUCTURE.md` §0, finding E3). Vendors retire models in 6–18 months and change
SDKs faster than that; a core that imports one dies with each model generation.

## Decision

- The core ships two executors only, `echo` and `tool` (`dagentos/agents/`). It never
  imports a vendor SDK or a provider package (ADR 0006's second contract lists them).
- Each model bridge is its own distribution, `agentos-provider-*` (`providers/`), versioned
  with the core, depending on `dagentos[providerkit]`, discovered through the entry-point
  group `agentos.executors` (`dagentos/plugins.py`, composition-root code only).
- The seam is the `Executor` port (`dagentos/core/ports.py`): the core sees `StepRequest` in
  and `StepResult` out and inspects payloads only by hash. Capability aliases, cassettes,
  pricing and prompt delimiting live in `dagentos.providerkit`, not the core.
- A plugin that fails to load is skipped with a warning naming it; a run that needs a
  missing executor fails at dispatch with the install hint. One broken provider never takes
  the API down.

## Consequences

- Two wire formats (OpenAI-compatible, Anthropic Messages), two SDK harnesses and an ACP
  agent were added with zero core changes between them (ROADMAP Phases 4, 7, 10).
- A provider can be archived when a vendor disappears without a core release.
- The cost is a second distribution to release per provider; `docs/RELEASING.md` keeps them
  on one version number.
