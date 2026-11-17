"""The mypy baseline gate (audit I-24): CI fails on a type error that is not in the
committed baseline, never on the error count, and never because a line moved."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "typecheck_gate.py"
sys.path.insert(0, str(GATE.parent))

import typecheck_gate

BASELINE = """\
dagentos/a.py:10: error: Incompatible return value type (got "str", expected "int")  [return-value]
dagentos/b.py:5: error: Need type annotation for "x"  [var-annotated]
dagentos/b.py:9: error: Need type annotation for "x"  [var-annotated]
"""


def _run(baseline: str, current: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    b = tmp_path / "baseline.txt"
    c = tmp_path / "current.txt"
    b.write_text(baseline)
    c.write_text(current)
    return subprocess.run(
        [sys.executable, str(GATE), str(b), str(c)], capture_output=True, text=True, check=False
    )


def test_identical_output_passes(tmp_path: Path) -> None:
    assert _run(BASELINE, BASELINE, tmp_path).returncode == 0


def test_fewer_errors_passes(tmp_path: Path) -> None:
    current = "\n".join(BASELINE.splitlines()[1:]) + "\n"
    assert _run(BASELINE, current, tmp_path).returncode == 0


def test_moved_line_numbers_pass(tmp_path: Path) -> None:
    current = BASELINE.replace(":10:", ":42:").replace(":5:", ":6:").replace(":9:", ":11:")
    assert _run(BASELINE, current, tmp_path).returncode == 0


def test_new_error_fails_and_is_named(tmp_path: Path) -> None:
    new = 'dagentos/c.py:3: error: Argument 1 has incompatible type "str"  [arg-type]'
    result = _run(BASELINE, BASELINE + new + "\n", tmp_path)
    assert result.returncode == 1
    assert new in result.stdout
    assert "dagentos/a.py" not in result.stdout, "baselined errors are not reported as new"


def test_same_message_appearing_once_more_fails(tmp_path: Path) -> None:
    extra = 'dagentos/b.py:30: error: Need type annotation for "x"  [var-annotated]'
    assert _run(BASELINE, BASELINE + extra + "\n", tmp_path).returncode == 1


def test_notes_and_summary_lines_are_ignored(tmp_path: Path) -> None:
    current = (
        BASELINE
        + 'dagentos/a.py:10: note: Possible overload variants:\n'
        + "Found 3 errors in 2 files (checked 53 source files)\n"
    )
    assert _run(BASELINE, current, tmp_path).returncode == 0


def test_unreadable_output_is_a_usage_error(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(GATE), str(tmp_path / "missing.txt"), str(tmp_path / "also.txt")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('dagentos/x.py:7: error: Bad  [misc]', 'dagentos/x.py: error: Bad  [misc]'),
        ('dagentos/x.py:7:12: error: Bad  [misc]', 'dagentos/x.py: error: Bad  [misc]'),
        ('dagentos/x.py:7: note: hint', None),
        ("Found 1 error in 1 file", None),
    ],
)
def test_normalise(line: str, expected: str | None) -> None:
    assert typecheck_gate.normalise(line) == expected


def test_committed_baseline_is_sorted_error_lines() -> None:
    """The file CI compares against is what `mypy --no-error-summary | sort` writes."""
    lines = (ROOT / "mypy-baseline.txt").read_text().splitlines()
    assert lines == sorted(lines)
    assert all(typecheck_gate.normalise(line) for line in lines)
