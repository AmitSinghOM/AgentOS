"""Fresh-venv install smoke for the publish pipeline (Phase 8 #10).

What a stranger's `pip install` sees. Run INSIDE a venv that has only the built wheels from
`dist/` installed (see .github/workflows/publish.yml and docs/RELEASING.md):

    python scripts/release_smoke.py <expected-version>

Asserts: the installed distribution is `agentos-durable` at the expected version, the import
package `agentos` and the CLI import, all four provider executors are registered through the
`agentos.executors` entry-point group, and `agentos doctor` runs against a memory store and
lists the built-in and provider executors. Exit 1 with the mismatch on any failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata

EXPECTED_EXECUTORS = {"openai-compat", "anthropic", "openai-agents", "pydantic-ai"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_smoke.py <expected-version>", file=sys.stderr)
        return 2
    expected = argv[1]
    problems: list[str] = []

    version = metadata.version("agentos-durable")
    if version != expected:
        problems.append(f"agentos-durable is {version}, expected {expected}")

    eps = {e.name for e in metadata.entry_points(group="agentos.executors")}
    if eps != EXPECTED_EXECUTORS:
        problems.append(f"executor entry points {sorted(eps)} != {sorted(EXPECTED_EXECUTORS)}")

    import agentos
    import agentos.cli  # noqa: F401
    from agentos.api.ui import PACKAGED_DIST
    if not (PACKAGED_DIST / "index.html").is_file():
        problems.append(f"operator UI bundle missing from the wheel ({PACKAGED_DIST}); run "
                        "`npm run build` in ui/ and scripts/bundle_ui.py before python -m build")
    else:
        stamp = PACKAGED_DIST / "VERSION"
        ui_version = stamp.read_text().strip() if stamp.is_file() else "?"
        if ui_version != expected:
            problems.append(f"packaged UI is {ui_version}, expected {expected}")

    env = dict(os.environ, AGENTOS_STORE="memory")
    env.pop("AGENTOS_POLICY", None)
    env.pop("AGENTOS_SIGNING_KEYS", None)
    proc = subprocess.run([sys.executable, "-m", "agentos.cli", "--json", "doctor"],
                          capture_output=True, text=True, env=env, check=False,
                          cwd=os.path.dirname(sys.executable))
    if proc.returncode != 0:
        problems.append(f"agentos doctor exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    else:
        names = {c["name"] for c in json.loads(proc.stdout)["checks"]}
        want = {"store", "executor echo", "executor tool"} | {f"executor {e}" for e in EXPECTED_EXECUTORS}
        if not want <= names:
            problems.append(f"doctor did not list {sorted(want - names)}")

    if problems:
        print("release smoke FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"release smoke ok: agentos-durable {version}, executors {sorted(eps)}, ui bundled")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
