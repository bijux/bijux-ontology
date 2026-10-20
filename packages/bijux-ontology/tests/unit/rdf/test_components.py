"""Bounded directed traversal preserves qualified identities without recursion."""

import pytest
from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.structure import strong_components


def test_components_keep_long_chains_cycles_and_original_anonymous_nodes() -> None:
    nodes: list[Node] = [URIRef(f"urn:chain:{i}") for i in range(1500)]
    anonymous = BNode()
    nodes.extend((anonymous, URIRef("urn:cycle"), URIRef("urn:self")))
    forward: dict[Node, list[Node]] = {n: [] for n in nodes}
    for before, after in zip(nodes[:1499], nodes[1:1500], strict=True):
        forward[before].append(after)
    forward[anonymous].append(nodes[-2])
    forward[nodes[-2]].append(anonymous)
    forward[nodes[-1]].append(nodes[-1])
    backward: dict[Node, list[Node]] = {n: [] for n in nodes}
    for node, children in forward.items():
        for child in children:
            backward[child].append(node)
    groups = list(
        strong_components(nodes, forward, backward, budget=Budget(), max_depth=None)
    )
    assert len(groups) == 1502 and sum(map(len, groups)) == len(nodes)
    cycle = next(g for g in groups if anonymous in g)
    assert set(cycle) == {anonymous, nodes[-2]}
    assert any(n is anonymous for n in cycle)
    assert [nodes[-1]] in groups
    for limits, depth in (
        (Limits(max_states=1), None),
        (Limits(max_total_bytes=128), None),
        (Limits(), 1),
    ):
        with pytest.raises(LimitExceeded):
            list(
                strong_components(
                    nodes, forward, backward, budget=Budget(limits), max_depth=depth
                )
            )


def test_completed_component_remains_available_when_later_work_exhausts() -> None:
    nodes = (URIRef("urn:a"), URIRef("urn:b"))
    adjacency: dict[URIRef, tuple[URIRef, ...]] = dict.fromkeys(nodes, ())
    budget = Budget()
    groups = strong_components(
        nodes, adjacency, adjacency, budget=budget, max_depth=None
    )
    first = next(groups)
    assert len(first) == 1 and first[0] in nodes
    budget.consume("work", budget.remaining("work"))
    with pytest.raises(LimitExceeded):
        next(groups)
    assert len(first) == 1
