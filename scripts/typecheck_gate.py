"""mypy baseline gate (audit I-24).

    python scripts/typecheck_gate.py mypy-baseline.txt <current mypy output>

Exit 0 when every error in the current output is already in the baseline, 1 when a NEW
error appears (it is printed), 2 on a usage error. The count going down is fine. Lines are
compared without their line/column numbers, so an edit above a known error does not turn it
into a "new" one; the same message appearing one more time in the same file does.

Refresh the baseline only in a PR that fixes types:
    mypy --no-error-summary | sort > mypy-baseline.txt

Stdlib only; tests/test_typecheck_gate.py feeds it fake baseline/current files.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

_ERROR = re.compile(r"^(?P<path>[^:\n]+):\d+(?::\d+)?: error: (?P<msg>.*)$")


def normalise(line: str) -> str | None:
    """`path:LINE[:COL]: error: msg` -> `path: error: msg`; anything else -> None."""
    m = _ERROR.match(line.rstrip("\n"))
    if m is None:
        return None
    return f"{m.group('path')}: error: {m.group('msg')}"


def new_errors(baseline: list[str], current: list[str]) -> list[str]:
    """Current error lines (verbatim) whose normalised form exceeds the baseline's count."""
    known = Counter(n for n in map(normalise, baseline) if n)
    out: list[str] = []
    for line in current:
        key = normalise(line)
        if key is None:
            continue
        if known[key] > 0:
            known[key] -= 1
        else:
            out.append(line.rstrip("\n"))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        baseline = Path(argv[0]).read_text().splitlines()
        current = Path(argv[1]).read_text().splitlines()
    except OSError as exc:
        print(f"typecheck_gate: {exc}", file=sys.stderr)
        return 2
    fresh = new_errors(baseline, current)
    if fresh:
        print(f"typecheck_gate: {len(fresh)} type error(s) not in {argv[0]}:")
        for line in fresh:
            print(line)
        return 1
    print(f"typecheck_gate: no new type errors ({len(current)} line(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
