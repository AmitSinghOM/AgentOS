"""The built-in `tool` executor (core install, no extras) imports `dagentos.providerkit.prompt`,
so importing the kit package must not drag in `jsonschema`. CI's bare-install chaos job caught
the worker failing to start when `providerkit/__init__` re-exported `schema` eagerly; this pins
it in the ordinary suite by running a fresh interpreter with `jsonschema` made unimportable."""
from __future__ import annotations

import subprocess
import sys
import textwrap

PROBE = textwrap.dedent("""
    import sys
    class _Block:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("jsonschema", "referencing"):
                raise ImportError(f"blocked for test: {name}")
            return None
    sys.meta_path.insert(0, _Block())

    import dagentos.providerkit                      # must import without jsonschema
    from dagentos.providerkit import render_prompt   # what dagentos.agents.tool needs
    import dagentos.agents.tool                      # the worker's composition root imports this
    try:
        dagentos.providerkit.OutputSchema            # lazy: only NOW does it need jsonschema
    except ImportError as exc:
        print("lazy-ok:", exc)
    else:
        raise SystemExit("OutputSchema resolved without jsonschema?!")
    print("OK")
""")


def test_importing_the_kit_and_the_tool_executor_needs_no_jsonschema():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                         timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    assert "lazy-ok: blocked for test: jsonschema" in out.stdout and out.stdout.strip().endswith("OK")
