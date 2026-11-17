# Contributing to AgentOS

Thanks for looking. This page is the shortest path from a clone to a green pull request. It
mirrors `.github/workflows/ci.yml` exactly: if a step here and a CI step ever disagree, CI is
right and this page has a bug — please fix the page in the same PR.

## Set up as CI does

Python **3.11 or newer** (`requires-python = ">=3.11"` in `pyproject.toml`; CI runs the floor,
3.11, and the newest interpreter we develop on, 3.14). Node **22 or newer** only if you touch
`ui/` (`"engines": {"node": ">=22"}` in `ui/package.json`).

```bash
git clone https://github.com/AmitSinghOM/AgentOS && cd AgentOS
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,observability,providerkit]" -e providers/openai-compat -e providers/anthropic -e providers/openai-agents -e providers/pydantic-ai -e providers/acp
```

That is the `test` job's install line: the core with every extra plus all five provider
distributions as editable installs. The providers are separate PyPI distributions that live in
this repository; their tests only run when they are installed (below).

## Run the gate locally

```bash
ruff check .                              # E, F, B, BLE, UP, I, RUF100 (pyproject [tool.ruff.lint])
lint-imports                              # layering contract: the core imports no adapter or SDK
mypy --no-error-summary | sort > /tmp/mypy.txt; python scripts/typecheck_gate.py mypy-baseline.txt /tmp/mypy.txt
pytest -q --ignore=tests/chaos            # the suite CI runs on 3.11 and 3.14
pytest -v tests/chaos --ignore=tests/chaos/network   # the fault points (the `chaos` job)
```

On lint and types:

- `ruff` selects `E, F, B, BLE, UP, I, RUF100`; `E501` (line length) and `UP042` (`StrEnum`)
  are ignored with the reasons written next to them in `pyproject.toml`. `I` is import order —
  `ruff check --fix --select I` sorts a new module for you. `BLE` means every deliberate
  `except Exception` boundary carries `# noqa: BLE001  # <why this boundary swallows>`, and
  `RUF100` fails a `noqa` that no longer suppresses anything.
- `mypy` is in the `dev` extra and runs non-strict over `dagentos` only (`[tool.mypy]`). The CI
  `typecheck` job is not count-based: `scripts/typecheck_gate.py` fails only on an error line
  that is **not** in the committed `mypy-baseline.txt` (line numbers ignored), so fixing an old
  error is fine and adding a new one is not. Refresh the baseline with
  `mypy --no-error-summary | sort > mypy-baseline.txt` only in a PR that fixes types, never to
  silence a new error. CI pins `mypy==2.4.1`; use the same version locally or a newer mypy's extra
  checks will look like regressions.
- Every package ships `py.typed` (`dagentos/`, each `providers/*/agentos_provider_*/`), which is
  the promise behind the `Typing :: Typed` classifier — keep public signatures annotated.

Three things that bite on a laptop:

- **A stray `pytest-asyncio`.** The repo does not depend on it, but a globally installed copy
  can crash collection under recent pytest (`'Package' object has no attribute 'obj'`). Run
  `pytest -p no:asyncio …` or use the clean venv above.
- **Provider tests and `testpaths`.** `pyproject.toml` lists every `providers/*/tests` directory
  in `[tool.pytest.ini_options] testpaths`, so a root `pytest` collects them all. Each provider
  test module therefore starts with `pytest.importorskip("agentos_provider_<name>")` and must
  keep doing so — a new provider test that imports its package at module top without it aborts
  the whole run for anyone who did not install that provider. CI installs all five, so the
  skip never hides a failure there.
- **The Postgres leg.** `tests/contract` has a Postgres variant that is skipped unless
  `AGENTOS_TEST_PG_DSN` is set (CI sets it against a `postgres:16` service and runs
  `pytest -q tests/contract -k postgres`). `docker compose up -d postgres` and
  `AGENTOS_TEST_PG_DSN=postgresql://agentos:agentos@localhost:5432/agentos` reproduces it.
  `tests/chaos/network` additionally needs Toxiproxy (`AGENTOS_TEST_TOXIPROXY_*`); leave that
  one to the `chaos-network` job unless you are changing lease fencing.

Touching `ui/`? The `ui` job is `cd ui && npm ci && npm test && npm run build`, then the mount
tests against the real bundle: `AGENTOS_UI_DIR="$PWD/ui/dist" pytest -q tests/test_ui_mount.py`.

## How a change is expected to look

- **Red first.** Write the test that fails on `main`, then the code that makes it pass, and
  keep both in the PR. For a bug fix the test is the bug's reproduction. A reviewer will ask
  for the discrimination proof: with your fix stashed the new test fails, with it restored it
  passes — paste the two one-line pytest summaries into the PR description.
- **A boundary changed? Add or edit a `docs/FAIL_MODES.md` row.** Every place the system can
  refuse, stop or carry on under a fault has a row there with its direction (fail-closed,
  fail-open, reported, warn, not detectable in-log) and the test that pins it.
  `tests/test_fail_modes.py` checks that every cited test exists and that the preamble's row
  count matches the table, so bump the count when you add a row.
- **Docs are checked against code.** A sentence in `README.md`, `docs/*.md` or a provider README
  that names a flag, a status code, a file or a default is a claim; several tests read those
  pages (`tests/test_quickstart.py`, `tests/test_tutorial.py`, `tests/test_repo_hygiene.py`,
  `tests/test_c13_mit_and_compose.py`, …). When code and prose disagree, prefer deleting the
  stale sentence over guessing a new one.
- **Smallest complete change**, inclusive language (allowlist / denylist, primary / replica), no
  secret values in code, tests, fixtures or logs — hashes and prefixes only, as the auth code
  does.
- The layering rules (`dagentos.core` imports nothing else in the package and no framework or
  vendor SDK; adapters do not import each other) are enforced by `lint-imports` from the
  `[tool.importlinter]` contracts in `pyproject.toml`. `docs/DEVELOPMENT_STRUCTURE.md` explains
  why.

## How a pull request is gated

`ci.yml` runs on every pull request that touches a path in its filter — `dagentos/**`,
`providers/**`, `ui/**`, `tests/**`, `examples/**`, `docker-compose.yml`, `pyproject.toml`,
`Dockerfile`, `scripts/release_smoke.py` or `ci.yml` itself — and its jobs are the required
checks:

| Job | What it proves |
| --- | --- |
| `package` | every distribution builds, `twine check --strict` passes, the wheels install into an empty venv and `scripts/release_smoke.py` runs — on every PR, not only on a tag |
| `ui` | type-check, Vitest, production build, then `tests/test_ui_mount.py` against the real bundle |
| `test (3.11)`, `test (3.14)` | `ruff check .`, `lint-imports`, `pytest -q --ignore=tests/chaos`, the Postgres contract leg |
| `audit` | `pip-audit --strict` over the resolved environment and `npm audit --audit-level=high` |
| `chaos` | the fault points under `tests/chaos`, including the real-process `kill -9` demo |
| `chaos-network` | the lease-expiry race through Toxiproxy ends with the stale worker fenced |
| `compose` | `docker compose up` brings Postgres, Jaeger, Prometheus and Grafana to healthy |

A **docs-only PR shows no checks** — that is the paths filter, not a failure. Run the
doc-pinning tests yourself before opening it and say so in the description:
`pytest -q tests/test_repo_hygiene.py tests/test_quickstart.py tests/test_tutorial.py tests/test_fail_modes.py tests/test_c13_mit_and_compose.py`.

Dependabot (`.github/dependabot.yml`) opens the dependency bumps for the core, every provider,
`ui/` and the workflow actions; a maintainer reviews and merges them.

## Releasing

Maintainers only. The procedure — version bump in three files, a `dry_run` rehearsal of
`publish.yml`, the tag, what publishes where and how a stranger verifies the provenance — is
[`docs/RELEASING.md`](docs/RELEASING.md).

## Security

Do not open an issue for a vulnerability. Report it privately as [`SECURITY.md`](SECURITY.md)
describes; it also says what is in scope and what is documented as out of scope by design.
