"""Property (d): `WorkflowDefinition.topological_order` — the single definition of DAG
validity (dagentos/core/models.py) — over random graphs.

Strategies: `dags()` draws 1–8 nodes where `n_i` depends on a random subset of
`n_0..n_{i-1}` (acyclic by construction) and then shuffles the node list, so the order the
author wrote the nodes in is random too. `with_cycle()` takes such a DAG and adds back-edges
along a random cyclic path of 1–4 nodes (length 1 is a self-loop). `with_dangling()` points
one node at an id that does not exist.

Properties:
  * the order is a permutation of the node ids and respects every edge;
  * it does not depend on the order the nodes were listed in (replays are reproducible);
  * `validate_dag()` agrees with it (same function, pinned so the alias cannot drift);
  * a cycle is rejected with `ValueError` naming a cycle; a dangling dependency with
    `ValueError` naming the unknown node — never a partial order.
"""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dagentos.core.models import WorkflowDefinition, WorkflowNode

from .conftest import PROPERTY


@st.composite
def dags(draw) -> list[WorkflowNode]:
    n = draw(st.integers(min_value=1, max_value=8))
    nodes = []
    for i in range(n):
        deps = (draw(st.lists(st.sampled_from([f"n{j}" for j in range(i)]), unique=True))
                if i else [])
        nodes.append(WorkflowNode(id=f"n{i}", agent="a", depends_on=deps))
    return draw(st.permutations(nodes))


@st.composite
def with_cycle(draw) -> tuple[list[WorkflowNode], list[str]]:
    nodes = draw(dags())
    ids = [n.id for n in nodes]
    ring = draw(st.lists(st.sampled_from(ids), min_size=1, max_size=4, unique=True))
    by_id = {n.id: n for n in nodes}
    for a, b in zip(ring, ring[1:] + ring[:1], strict=True):     # a -> b -> ... -> a
        if b not in by_id[a].depends_on:
            by_id[a].depends_on.append(b)
    return nodes, ring


@st.composite
def with_dangling(draw) -> tuple[list[WorkflowNode], str]:
    nodes = draw(dags())
    victim = draw(st.sampled_from(nodes))
    victim.depends_on.append("ghost")
    return nodes, victim.id


def _wf(nodes: list[WorkflowNode]) -> WorkflowDefinition:
    return WorkflowDefinition(name="w", nodes=nodes)


@PROPERTY
@given(nodes=dags())
def test_order_is_a_permutation_that_respects_every_edge_and_ignores_listing_order(nodes):
    order = _wf(nodes).topological_order()
    assert sorted(order) == sorted(n.id for n in nodes)
    position = {nid: i for i, nid in enumerate(order)}
    for node in nodes:
        for dep in node.depends_on:
            assert position[dep] < position[node.id], (dep, node.id, order)
    # The same DAG listed in any other order yields the same schedule.
    assert _wf(sorted(nodes, key=lambda n: n.id)).topological_order() == order
    assert _wf(list(reversed(nodes))).topological_order() == order
    _wf(nodes).validate_dag()                            # the alias accepts what the order accepts


@PROPERTY
@given(case=with_cycle())
def test_a_cycle_anywhere_is_rejected_never_a_partial_order(case):
    nodes, ring = case
    with pytest.raises(ValueError, match="has a cycle at") as exc:
        _wf(nodes).topological_order()
    # The named node is really stuck: it is on a cycle or downstream of one.
    named = str(exc.value).rsplit(" at ", 1)[1].strip("'")
    assert named in {n.id for n in nodes}
    with pytest.raises(ValueError, match="has a cycle"):
        _wf(nodes).validate_dag()


@PROPERTY
@given(case=with_dangling())
def test_a_dangling_dependency_is_rejected_by_name(case):
    nodes, victim = case
    with pytest.raises(ValueError, match=f"node '{victim}' depends on unknown node 'ghost'"):
        _wf(nodes).topological_order()
