"""Packaging facts that must not drift, checked from the files themselves:
  * distribution AND import package are both `dagentos` (PyPI `agentos` is agentos.org's and
    `agenticos` another project's — both with the same import names; `dagentos` was free)
  * core and every provider share one version, and providers depend on `dagentos`
  * the API reports the same version
  * every action in publish.yml is pinned by a 40-hex commit SHA (it holds id-token: write)
  * the Dockerfile pins its base by digest and runs as a non-root user
  * the sdist selection excludes tests, providers, CI and the compose stack
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = tomllib.loads((ROOT / "pyproject.toml").read_text())
PROVIDERS = sorted((ROOT / "providers").glob("*/pyproject.toml"))


def test_distribution_name_and_import_package():
    assert CORE["project"]["name"] == "dagentos"
    assert CORE["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["dagentos"]
    assert CORE["project"]["readme"] == "README.md"
    assert CORE["project"]["scripts"] == {"agentos": "dagentos.cli:main"}
    assert "Repository" in CORE["project"]["urls"]


def test_one_version_everywhere():
    version = CORE["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    for p in PROVIDERS:
        prov = tomllib.loads(p.read_text())["project"]
        assert prov["version"] == version, p
        deps = [d for d in prov["dependencies"] if d.startswith("dagentos")]
        assert len(deps) == 1 and deps[0].startswith("dagentos[providerkit]"), (p, deps)
        assert prov["readme"] == "README.md", p
    api = (ROOT / "dagentos" / "api" / "main.py").read_text()
    assert f'FastAPI(title="AgentOS", version="{version}")' in api


def test_sdist_ships_only_the_package_and_what_a_rebuilder_needs():
    include = CORE["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/dagentos/**" in include and "/pyproject.toml" in include and "/README.md" in include
    for banned in ("tests", "providers", ".github", "docker-compose.yml", "deploy"):
        assert not any(banned in i for i in include), banned


def test_publish_workflow_is_sha_pinned_and_pypi_is_an_explicit_switch():
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)@(\S+)", wf)
    assert len(uses) >= 12
    for action, ref in uses:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action}@{ref} is not SHA-pinned"
    assert "id-token: write" in wf
    assert "vars.PYPI_PUBLISH == 'true'" in wf and "PyPI upload not enabled" in wf
    assert "scripts/release_smoke.py" in wf and "twine check --strict" in wf
    assert "attest-build-provenance" in wf and "subject-digest" in wf


def test_dockerfile_pins_base_by_digest_and_drops_root():
    df = (ROOT / "Dockerfile").read_text()
    froms = re.findall(r"^FROM (\S+)", df, re.MULTILINE)
    assert froms and all("@sha256:" in f for f in froms), froms
    assert "USER agentos" in df and "useradd" in df
    assert "./dist/dagentos-*.whl" in df          # installed by path, never from an index
    assert "--find-links" not in df and "--index-url" not in df
    ignore = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert ignore[:2] == ["*", "!dist/"]


def test_ci_runs_the_package_gate_on_every_pr():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "  package:" in ci and "scripts/release_smoke.py" in ci and "twine check --strict" in ci
