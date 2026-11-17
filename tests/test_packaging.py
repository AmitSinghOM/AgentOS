"""Packaging facts that must not drift, checked from the files themselves:
  * distribution AND import package are both `dagentos` (PyPI `agentos` is agentos.org's and
    `agenticos` another project's — both with the same import names; `dagentos` was free)
  * core and every provider share one version, and providers depend on `dagentos`
  * the API reports the same version
  * every action in publish.yml is pinned by a 40-hex commit SHA (it holds id-token: write)
  * the Dockerfile pins its base by digest and runs as a non-root user
  * the sdist selection excludes tests, providers, CI and the compose stack
  * CI runs on `main` daily and the nightly provider job replays every provider suite
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

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


WORKFLOWS = sorted(p.name for p in (ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_workflow_pins_every_action_by_commit_sha(name):
    """Audit I-22: publish.yml was SHA-pinned because it holds `id-token: write`, but ci.yml and
    provider-live.yml still ran `actions/*@v7` — a moved tag on any of them runs foreign code
    with the repository's read token and the PR's secrets. Every `uses:` in every workflow is a
    40-hex commit followed by a `# vN.N.N` comment, the shape Dependabot understands and bumps."""
    wf = (ROOT / ".github" / "workflows" / name).read_text()
    lines = [ln for ln in wf.splitlines() if re.search(r"^\s*-?\s*uses:\s", ln)]
    assert lines, f"{name} has no `uses:` lines"
    for ln in lines:
        m = re.search(r"uses:\s*(\S+)@([0-9a-f]{40})\s+#\s*(v\d+\.\d+\.\d+|release/v\d+)\s*$", ln)
        assert m, f"{name}: not `owner/action@<40-hex> # vN.N.N`: {ln.strip()}"


def test_provider_live_verifies_the_ollama_installer_and_quotes_the_model_input():
    """Audit I-22: `curl https://ollama.com/install.sh | sh` ran whatever the CDN served; the
    step now downloads the script, checks its recorded sha256 and only then runs it. The
    operator-supplied `model` input reaches the shell through `env:` only, never substituted
    into a `run:` block (GitHub's script-injection shape)."""
    live = (ROOT / ".github" / "workflows" / "provider-live.yml").read_text()
    assert "install.sh | sh" not in live, "installer is still piped straight into sh"
    assert re.search(r"curl -fsSL -o install\.sh https://ollama\.com/install\.sh", live)
    assert re.search(r'OLLAMA_INSTALL_SHA256: "[0-9a-f]{64}"', live), "no recorded sha256"
    assert 'echo "$OLLAMA_INSTALL_SHA256  install.sh" | sha256sum -c -' in live
    assert "\n          sh install.sh\n" in live
    for ln in live.splitlines():
        if "github.event.inputs" in ln:
            assert re.fullmatch(r"\s+[A-Z_]+: \$\{\{ github\.event\.inputs\.model[^}]*\}\}", ln), \
                f"input used outside an env: entry: {ln.strip()}"
    assert 'ollama pull "$MODEL"' in live


def test_dependabot_watches_the_image_base():
    """Audit I-21: the Dockerfile pins python:3.12-slim by digest, so without the docker
    ecosystem the digest only ever moves by hand (last 2026-09-20)."""
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    blocks = re.split(r"\n(?=  - package-ecosystem:)", text)
    docker = [b for b in blocks if 'package-ecosystem: "docker"' in b]
    assert len(docker) == 1, "exactly one docker ecosystem entry expected"
    assert 'directory: "/"' in docker[0] and 'interval: "weekly"' in docker[0], docker[0]


CONSTRAINTS = ROOT / "deploy" / "constraints.txt"


def _logical_lines(text: str) -> list[str]:
    """Requirements-file lines with `\\` continuations joined, comments and blanks dropped."""
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        out.append((cur + line).strip())
        cur = ""
    return out


def test_image_installs_third_party_deps_from_hash_pinned_constraints():
    """Audit I-21/E-20: the image installed `[providerkit,observability]` plus five providers
    and let pip resolve every third-party dependency from the index at build time, so two builds
    of one tag could differ. Every third-party distribution now comes from deploy/constraints.txt
    (`==` + `--hash=sha256:` on every line, pip in `--require-hashes` mode) and our own wheels
    go in with `--no-deps`, so nothing outside that list can reach the image."""
    df = (ROOT / "Dockerfile").read_text()
    assert "deploy/constraints.txt" in df, "Dockerfile does not copy the constraints file"
    assert re.search(r"pip install [^\n]*--require-hashes [^\n]*-r \./constraints\.txt", df), df
    assert re.search(r"pip install [^\n]*--no-deps [^\n]*\\\n\s+\"\$\(ls \./dist/dagentos-\*\.whl\)", df)
    assert "--upgrade pip" not in df, "pip must come from the constraints file too, not `latest`"
    ignore = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()]
    assert "!deploy/constraints.txt" in ignore, "the build context never sees the constraints"

    assert CONSTRAINTS.is_file(), "deploy/constraints.txt missing: run scripts/refresh_constraints.py"
    pinned = _logical_lines(CONSTRAINTS.read_text())
    assert pinned, "constraints file is empty"
    names = set()
    for ln in pinned:
        assert "==" in ln, f"not an exact pin: {ln}"
        assert "--hash=sha256:" in ln, f"no sha256 hash: {ln}"
        names.add(re.match(r"([A-Za-z0-9_.\-]+)", ln).group(1).lower().replace("_", "-"))
    assert "pip" in names, "pip itself is upgraded in the image, so it must be pinned too"

    # every direct third-party requirement of what the image installs is on the list, so a new
    # dependency in any pyproject without a refresh fails here instead of at `docker build`
    wanted = list(CORE["project"]["dependencies"])
    wanted += CORE["project"]["optional-dependencies"]["providerkit"]
    wanted += CORE["project"]["optional-dependencies"]["observability"]
    for p in PROVIDERS:
        wanted += tomllib.loads(p.read_text())["project"]["dependencies"]
    for req in wanted:
        dist = re.match(r"([A-Za-z0-9_.\-]+)", req).group(1).lower().replace("_", "-")
        if dist == "dagentos":
            continue                     # our own wheel, installed by path
        assert dist in names, f"{req!r} is not pinned in deploy/constraints.txt (refresh it)"


def test_constraints_refresh_is_scripted_and_documented():
    script = ROOT / "scripts" / "refresh_constraints.py"
    assert script.is_file()
    body = script.read_text()
    assert "deploy/constraints.in" in body and "deploy/constraints.txt" in body
    assert "--generate-hashes" in body and "--universal" in body
    releasing = (ROOT / "docs" / "RELEASING.md").read_text()
    assert "## Reproducible image" in releasing
    assert "scripts/refresh_constraints.py" in releasing and "deploy/constraints.txt" in releasing


def test_pypi_matrix_lists_every_distribution_the_build_job_builds():
    """`build` builds core plus every `providers/*`; the `pypi` matrix must name each of
    them, or a provider is attested and attached to the release yet never uploaded (the
    v0.17.0 run shipped four providers and silently left `agentos_provider_acp` behind)."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    m = re.search(r"dist:\s*\[([^\]]*)\]", wf)
    assert m, "pypi matrix `dist:` list not found in publish.yml"
    listed = {d.strip() for d in m.group(1).replace("\n", " ").split(",") if d.strip()}
    expected = {"dagentos"} | {
        tomllib.loads(p.read_text())["project"]["name"].replace("-", "_") for p in PROVIDERS}
    assert listed == expected, (sorted(expected - listed), sorted(listed - expected))


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


def test_dockerfile_installs_every_provider_wheel():
    """The image is 'core plus every provider' (docs/RELEASING.md); publish.yml builds a
    wheel for each `providers/*`, so each must appear on the Dockerfile's install line or
    the image silently ships without it (v0.17.1 omitted `agentos_provider_acp`, audit I-42)."""
    df = (ROOT / "Dockerfile").read_text()
    for p in PROVIDERS:
        dist = tomllib.loads(p.read_text())["project"]["name"].replace("-", "_")
        assert f"./dist/{dist}-*.whl" in df, dist
    words = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
    releasing = " ".join((ROOT / "docs" / "RELEASING.md").read_text().split())
    assert f"all {words[len(PROVIDERS)]} providers" in releasing, len(PROVIDERS)


def test_ci_runs_the_package_gate_on_every_pr():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "  package:" in ci and "scripts/release_smoke.py" in ci and "twine check --strict" in ci


def test_main_is_checked_daily_and_the_nightly_replays_every_provider():
    """Audit I-11: floors-not-pins plus a gitignored uv.lock mean a dependency floor drift
    only ever surfaced as red checks on unrelated Dependabot PRs (pydantic-ai >=2.61 broke
    `rejected: true` for three weeks unseen). The full CI must run on `main` on a schedule,
    in its own concurrency group so a push to main cannot cancel it, and the nightly
    provider job must install and replay EVERY provider suite, not a hand-picked subset."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    cron = re.search(r"^  schedule:\n(?:    #[^\n]*\n)*    - cron: \"(\d+) (\d+) \* \* \*\"", ci, re.MULTILINE)
    assert cron, "ci.yml has no daily schedule trigger"
    assert cron.group(1) != "0", "schedule on the hour is queued behind everyone else's"
    group = re.search(r"^  group: (.+)$", ci, re.MULTILINE)
    assert group and "github.event_name" in group.group(1), "scheduled run shares a group with push"

    live = (ROOT / ".github" / "workflows" / "provider-live.yml").read_text()
    assert "permissions:\n  contents: read" in live
    assert re.search(r"pytest -q providers tests/test_quickstart_llm\.py", live), \
        "provider-live replays a subset of the provider suites"
    install = re.search(r"pip install -e \"\.\[[^\]]*\]\"[^\n]*", live)
    assert install, "provider-live install line not found"
    for p in PROVIDERS:
        assert f"-e providers/{p.parent.name}" in install.group(0), p.parent.name
