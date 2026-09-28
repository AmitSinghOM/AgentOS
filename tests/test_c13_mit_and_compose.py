"""C13 (#13): stay MIT; `docker compose up` is the whole product; no hosted tier assumed.
Landscape con: mastra `ee/` dual licence, Inngest SSPL, Restate BSL, durability or
observability gated behind a vendor's cloud. Acceptance: the compose CI job passes (CI),
and nothing a user must read requires an external account (this file)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_FACING = [ROOT / "README.md", ROOT / "docs" / "quickstart-llm.md", ROOT / "docs" / "tutorial.md",
               ROOT / "providers" / "openai-compat" / "README.md",
               ROOT / "providers" / "anthropic" / "README.md"]
# Phrases that would mean "you need an account somewhere to follow this page".
HOSTED_TIER = re.compile(r"\b(sign ?up|create an account|free tier|paid plan|upgrade to|"
                         r"enterprise (edition|licen[cs]e)|hosted (tier|version)|"
                         r"cloud account required|api key required)\b", re.IGNORECASE)
OSS_IMAGES = {"postgres", "jaegertracing/jaeger", "prom/prometheus", "grafana/grafana"}


def test_license_is_mit_everywhere():
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    for py in (ROOT / "pyproject.toml", ROOT / "providers" / "openai-compat" / "pyproject.toml",
               ROOT / "providers" / "anthropic" / "pyproject.toml"):
        assert 'license = { text = "MIT" }' in py.read_text(), py
    tracked = [p for p in ROOT.glob("*/LICENSE-*") if ".venv" not in p.parts]
    assert not (ROOT / "ee").exists() and not tracked                 # no dual licence


def test_no_user_facing_page_requires_an_external_account():
    for page in USER_FACING:
        text = page.read_text()
        hits = [m.group(0) for m in HOSTED_TIER.finditer(text)]
        assert not hits, f"{page.relative_to(ROOT)} mentions {hits}"
    # Paid providers are optional and say so; the default path is a local model.
    q = (ROOT / "docs" / "quickstart-llm.md").read_text()
    assert "no API key" in q and "Ollama" in q


def test_compose_is_only_open_source_images():
    compose = (ROOT / "docker-compose.yml").read_text()
    images = {m.group(1).split(":")[0] for m in re.finditer(r"image:\s*(\S+)", compose)}
    assert images <= OSS_IMAGES, images - OSS_IMAGES


# Every quoted `host:container` (or `ip:host:container`) port mapping in the compose file.
# Volume mounts and healthcheck argv never match `\d+:\d+`, so this is exactly the publishes.
PORT_MAPPING = re.compile(r'"((?:[\d.]+:)?\d+:\d+)"')


def test_compose_publishes_every_port_on_loopback_only():
    """A bare `HOST:CONTAINER` publish binds 0.0.0.0, so `docker compose up` on a laptop on
    a shared network exposes Postgres (a fixed dev password), the OTLP intake and three
    unauthenticated UIs to the whole segment. Local dev is the only audience: every publish
    carries the `127.0.0.1:` host prefix. Someone on another machine gets an SSH tunnel."""
    compose = (ROOT / "docker-compose.yml").read_text()
    mappings = PORT_MAPPING.findall(compose)
    assert len(mappings) == 5, mappings           # guard: the parse found the real entries
    exposed = [m for m in mappings if not m.startswith("127.0.0.1:")]
    assert not exposed, f"published beyond loopback: {exposed}"


# `${VAR:?message}` references in the compose file: compose fails fast at `up` if VAR is unset.
REQUIRED_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")


def _env_example() -> dict[str, str]:
    """Uncommented KEY=VALUE lines of .env.example — what `cp .env.example .env` gives compose."""
    out: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_compose_carries_no_literal_credential():
    """The delivered compose file names the variable, never the value: a literal
    POSTGRES_PASSWORD in a committed file is a credential in the repo even for a dev stack."""
    compose = (ROOT / "docker-compose.yml").read_text()
    m = re.search(r"POSTGRES_PASSWORD:\s*(\S+)", compose)
    assert m, "postgres service must still set POSTGRES_PASSWORD"
    assert m.group(1).startswith("${POSTGRES_PASSWORD:?"), m.group(1)


def test_every_required_compose_var_is_set_by_env_example():
    """`cp .env.example .env && docker compose up` (README, CI compose job) must work, so every
    fail-fast `${VAR:?}` the compose file demands is an uncommented line in .env.example."""
    compose = (ROOT / "docker-compose.yml").read_text()
    required = set(REQUIRED_VAR.findall(compose))
    assert required, "expected at least one fail-fast ${VAR:?} reference"
    env = _env_example()
    missing = sorted(v for v in required if not env.get(v))
    assert not missing, f"compose requires {missing} but .env.example does not set them"


def test_env_example_dsn_matches_its_own_postgres_password():
    """The commented AGENTOS_PG_DSN in .env.example (and README) must use the same dev password
    the compose stack is started with, or the documented quick start cannot connect."""
    text = (ROOT / ".env.example").read_text()
    password = _env_example()["POSTGRES_PASSWORD"]
    dsn = re.search(r"AGENTOS_PG_DSN=postgresql://agentos:([^@]+)@", text)
    assert dsn and dsn.group(1) == password, (dsn and dsn.group(1), password)
    readme = (ROOT / "README.md").read_text()
    assert f"postgresql://agentos:{password}@" in readme


def test_grafana_anonymous_role_is_not_admin():
    """Anonymous Grafana is fine on loopback for viewing the provisioned dashboard; anonymous
    *Admin* lets any local process rewrite datasources and dashboards. Viewer is enough:
    the dashboards are provisioned read-only from files."""
    compose = (ROOT / "docker-compose.yml").read_text()
    m = re.search(r"GF_AUTH_ANONYMOUS_ORG_ROLE:\s*(\S+)", compose)
    assert m and m.group(1) == "Viewer", m and m.group(1)


def test_docs_do_not_advertise_grafana_admin_login():
    """The stack runs Grafana anonymous with the login form disabled, so a doc telling the
    reader to log in as admin/admin describes a screen that does not exist."""
    for page in (ROOT / "docs" / "quickstart-llm.md", ROOT / "README.md", ROOT / "SECURITY.md"):
        assert "admin/admin" not in page.read_text(), page.relative_to(ROOT)
