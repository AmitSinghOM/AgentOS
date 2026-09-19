"""docs/FAIL_MODES.md is a table of guarantees, each pinned to a named test. This test keeps
the table honest: every cited `path::test_name` must exist as a test function in this
tree, and every row must declare a direction. A doc of guarantees that outlives the code
is worse than no doc (Phase 8 #11)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "FAIL_MODES.md"
CITATION = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")
DIRECTIONS = ("**closed", "**open", "**not a boundary**", "**reported**", "**warn**",
              "**not detectable in-log**")


def _rows() -> list[str]:
    return [line for line in DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and not line.startswith("| Chokepoint")
            and not line.startswith("| ---")]


def _citations() -> list[tuple[str, str]]:
    return CITATION.findall(DOC.read_text(encoding="utf-8"))


def test_the_table_cites_at_least_forty_pinned_tests():
    assert len(_citations()) >= 40


@pytest.mark.parametrize("path, name", sorted(set(_citations())))
def test_every_cited_test_exists(path: str, name: str):
    src = (ROOT / path)
    assert src.exists(), f"{path} cited in FAIL_MODES.md does not exist"
    assert re.search(rf"^def {re.escape(name)}\(", src.read_text(encoding="utf-8"), re.MULTILINE), \
        f"{path} has no top-level test named {name}"


def test_every_row_declares_a_direction_and_a_pinning_test():
    for row in _rows():
        assert any(d in row for d in DIRECTIONS), f"row without a direction: {row[:80]}"
        assert CITATION.search(row), f"row without a pinning test: {row[:80]}"


UNPINNED = {"`_fail` under conflict"}


def test_unpinned_rows_are_named_here():
    """A row may cite THIS test only when its chokepoint is listed in UNPINNED — the gap is
    then a declared TODO, not a forgotten one. Closing the gap means writing the real test
    and removing the name from this set."""
    rows = [r for r in _rows() if "test_fail_modes.py::test_unpinned_rows_are_named_here" in r]
    named = {r.split("|")[1].strip() for r in rows}
    assert named == UNPINNED, f"rows citing the placeholder: {named}; declared: {UNPINNED}"
    for r in rows:
        assert "**Unpinned**" in r, f"placeholder row must say Unpinned: {r[:80]}"
