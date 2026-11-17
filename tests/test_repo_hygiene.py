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


# ------------------------------------------------------------------ community files (audit I-36)

ISSUE_FORMS = [".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml"]


def test_contributing_describes_the_ci_gate():
    """CONTRIBUTING.md must set up and gate exactly as ci.yml does, or a first PR learns the
    rules from a red check instead of the page."""
    text = (ROOT / "CONTRIBUTING.md").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    install = re.search(r"pip install -e \"\.\[dev,observability,providerkit\]\"[^\n]*", ci).group(0)
    assert install in text, install                       # the same install line as the test job
    for needle in ("ruff check .", "lint-imports", "pytest -q --ignore=tests/chaos", "-p no:asyncio",
                   "importorskip", "docs/FAIL_MODES.md", "docs/RELEASING.md", "SECURITY.md",
                   "Node", "22", "3.11", "mypy-baseline.txt", "BLE001"):
        assert needle in text, needle


@pytest.mark.parametrize("path", ISSUE_FORMS)
def test_issue_forms_exist_and_ask_for_the_facts_a_report_needs(path):
    text = (ROOT / path).read_text()
    assert text.startswith("name:") and "\nbody:\n" in text   # a GitHub issue FORM, not a .md
    if "bug" in path:
        for needle in ("pip show dagentos", "AGENTOS_STORE", "AGENTOS_AUTH", "agentos doctor --json",
                       "redact"):
            assert needle in text, needle


def test_issue_config_disables_blank_issues_and_links_security():
    text = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()
    assert "blank_issues_enabled: false" in text
    assert "SECURITY.md" in text


def test_pull_request_template_carries_the_house_checklist():
    text = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    for needle in ("- [ ]", "red", "FAIL_MODES", "secret"):
        assert needle in text, needle


def test_readme_links_contributing_and_the_issue_forms():
    readme = (ROOT / "README.md").read_text()
    assert "CONTRIBUTING.md" in readme
    assert "ISSUE_TEMPLATE" in readme
    for path in ["CONTRIBUTING.md", *ISSUE_FORMS]:
        assert (ROOT / path).exists(), path
