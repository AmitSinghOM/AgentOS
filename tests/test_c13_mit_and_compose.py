"""C13 (#13): stay MIT; `docker compose up` is the whole product; no hosted tier assumed.
Landscape con: mastra `ee/` dual licence, Inngest SSPL, Restate BSL, durability or
observability gated behind a vendor's cloud. Acceptance: the compose CI job passes (CI),
and nothing a user must read requires an external account (this file)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_FACING = [ROOT / "README.md", ROOT / "docs" / "quickstart-llm.md",
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
