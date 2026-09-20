"""Repository hygiene (production pass 2, A3): the controls a public package with a published
image is expected to carry, pinned so they cannot quietly disappear.

  * SECURITY.md — how to report a vulnerability privately, what is supported, what is in
    scope (and that `AGENTOS_AUTH=asserted` is documented as unverified, not a bug);
  * .github/dependabot.yml — pip for the core and every provider, npm for ui/, and the
    workflow actions themselves;
  * a CI job that fails on a known-vulnerable dependency (pip-audit --strict, npm audit).

These are doc/config drift guards in the house style: they read the files, not the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = sorted(p.name for p in (ROOT / "providers").iterdir() if (p / "pyproject.toml").exists())


def test_security_policy_exists_and_says_how_to_report_privately():
    text = (ROOT / "SECURITY.md").read_text()
    for needle in ("## Reporting", "Security Advisor", "## Supported versions",
                   "## Scope", "AGENTOS_AUTH=asserted", "90 days"):
        assert needle in text, needle


def test_dependabot_covers_every_python_distribution_the_ui_and_the_actions():
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    directories = re.findall(r'directory:\s*"?([^"\n]+)"?', text)
    ecosystems = re.findall(r'package-ecosystem:\s*"?([a-z-]+)"?', text)
    assert "/" in directories                                   # the core
    for name in PROVIDERS:
        assert f"/providers/{name}" in directories, name
    assert "/ui" in directories
    assert set(ecosystems) >= {"pip", "npm", "github-actions"}


def test_ci_has_an_audit_job_that_fails_on_known_vulnerabilities():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "\n  audit:\n" in ci, "no `audit` job in ci.yml"
    body = ci[ci.index("\n  audit:\n"):]
    assert "pip-audit" in body and "--strict" in body
    assert "npm audit" in body and "--audit-level=high" in body


@pytest.mark.parametrize("path", ["SECURITY.md", ".github/dependabot.yml"])
def test_readme_points_at_the_security_policy(path):
    assert (ROOT / path).exists(), path
    readme = (ROOT / "README.md").read_text()
    assert "SECURITY.md" in readme
