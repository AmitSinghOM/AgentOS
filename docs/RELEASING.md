# Releasing AgentOS

How a version leaves this repository, what publishes where, and how a stranger verifies it.
Phase 8 #10. The pipeline is `.github/workflows/publish.yml`; every action in it is pinned by
commit SHA because it holds `id-token: write`.

## Names

| Thing | Name | Why |
| --- | --- | --- |
| PyPI distribution (core) | `agentos-durable` | `agentos` on PyPI belongs to agentos.org — a different project whose **import package is also `agentos`** |
| Import package | `agentos` | unchanged; `pip install agentos` and `pip install agentos-durable` in one environment clobber each other. Renaming the import package is an open decision, not a small one (every module, test and provider) |
| PyPI distributions (providers) | `agentos-provider-openai-compat`, `-anthropic`, `-openai-agents`, `-pydantic-ai` | each depends on `agentos-durable[providerkit]` |
| Image | `ghcr.io/amitsinghom/agentos:{version,latest}` | GitHub namespace; multi-arch (amd64, arm64) |
| Console script | `agentos` | `verify` / `doctor` / `policy explain` |

Documented install path: `pip install "agentos-durable[providerkit]" agentos-provider-openai-compat`.

## Versions

One version for the core and every provider (`publish.yml` refuses a tag whose version does
not match `pyproject.toml`, and refuses a provider whose version differs from the core). Bump
in: `pyproject.toml`, `providers/*/pyproject.toml`, `agentos/api/main.py` (`FastAPI(version=)`).
Tag `vX.Y.Z` or `vX.Y.Z-<word>` (the word is dropped for the comparison: `v0.9.0-pilot` → `0.9.0`).

## Cut a release

1. Merge the close-out PR (version bump, `docs/releases/vX.Y.Z-<word>.md`, golden log, ROADMAP).
2. **Rehearse**: Actions → publish → Run workflow → `dry_run: true`. This builds and twine-checks
   every distribution, installs the wheels into an empty venv and runs `scripts/release_smoke.py`,
   attests every artifact, builds the image from those wheels and runs `agentos --help` inside it.
   Nothing is uploaded. Expect the first rehearsal of any pipeline change to find something.
3. Tag the merge commit: `git tag -a vX.Y.Z-<word> <sha> -m "..."`, `git push origin vX.Y.Z-<word>`.
4. The tag runs `publish.yml` for real: artifacts + provenance, image pushed and attested,
   GitHub release created with every artifact and `SHA256SUMS`, PyPI upload **if enabled** (below).

## PyPI: one-time setup (Amit only — needs the PyPI account)

Trusted Publishing means no API token ever lives in this repository. For **each** of the five
distributions, on PyPI: *Your projects → Publishing → Add a new pending publisher*:

| Field | Value |
| --- | --- |
| PyPI project name | `agentos-durable` (then each `agentos-provider-*`) |
| Owner | `AmitSinghOM` |
| Repository | `AgentOS` |
| Workflow name | `publish.yml` |
| Environment | `pypi` |

Then create the `pypi` environment in the repository settings (optionally with a required
reviewer), and set the repository variable **`PYPI_PUBLISH=true`**. Until that variable is set
the `pypi` jobs do not run and the workflow summary says "PyPI upload not enabled" — an explicit
switch, not a silent skip. To publish a tag that ran before the switch: re-run the workflow on
that tag.

A version that failed to upload cannot be re-uploaded to PyPI once any file for it landed; fix
forward with a new patch version.

## Verify what you installed

```bash
# provenance of a wheel (Sigstore-signed SLSA attestation produced by the release run)
gh attestation verify agentos_durable-0.9.0-py3-none-any.whl --owner AmitSinghOM

# provenance of the image
gh attestation verify oci://ghcr.io/amitsinghom/agentos:0.9.0 --owner AmitSinghOM

# the release's SHA256SUMS matches what pip downloaded
pip download --no-deps agentos-durable==0.9.0 -d /tmp/dl && (cd /tmp/dl && sha256sum *)
```

## What the image is

`python:3.12-slim` pinned by digest; a venv with the core (`[providerkit,observability]`) and all
four providers installed **from the wheels the same run built** (by path, never from an index);
non-root user `agentos` (uid 10001); `/var/lib/agentos` volume for the default SQLite store;
`CMD` is the API; `python -m agentos.worker` is the worker; the `agentos` CLI is on `PATH`.

```bash
docker run --rm ghcr.io/amitsinghom/agentos:0.9.0 agentos --help
docker run --rm -p 8000:8000 -e AGENTOS_STORE=postgres -e AGENTOS_PG_DSN=... ghcr.io/amitsinghom/agentos:0.9.0
docker run --rm -e AGENTOS_STORE=postgres -e AGENTOS_PG_DSN=... ghcr.io/amitsinghom/agentos:0.9.0 python -m agentos.worker
```

## Local dry run

```bash
pip install build==1.2.2.post1 twine==7.0.0 packaging==26.3
rm -rf dist && python -m build --outdir dist . && for p in providers/*/; do python -m build --outdir dist "$p"; done
python -m twine check --strict dist/*
python -m venv /tmp/smoke && /tmp/smoke/bin/pip install "$(ls dist/agentos_durable-*.whl)[providerkit]" dist/agentos_provider_*.whl
/tmp/smoke/bin/python scripts/release_smoke.py 0.9.0
docker build -t agentos:local .            # needs the wheels in dist/
```
