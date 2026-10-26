"""Necessary finite root conflicts over original local and reference witnesses."""

from collections.abc import Iterator
from dataclasses import dataclass

from rdflib import URIRef
from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.shacl.parameters.reading import ParameterReader

from .local import ShapeState
from .types import LocalConflict, RootConflict, SignedDependency


@dataclass(frozen=True)
class _Proof:
    """A bounded shared derivation, referring to original local facts and edges."""

    local: frozenset[Node]
    edges: frozenset[int] = frozenset()


def root_conflicts(
    roots: tuple[Node, ...],
    states: dict[Node, ShapeState],
    conflicts: list[LocalConflict],
    edges: list[SignedDependency],
    parameters: ParameterReader,
) -> Iterator[RootConflict]:
    """Propagate only finite necessary invocations; alternatives are never fused."""
    budget = parameters.budget
    proofs: dict[Node, _Proof] = {}
    by_shape: dict[Node, dict[URIRef, list[int]]] = {}
    for conflict in conflicts:
        budget.consume("work")
        if states[conflict.shape].valid:
            budget.consume("states")
            proofs[conflict.shape] = _Proof(frozenset((conflict.shape,)))
    for index, edge in enumerate(edges):
        budget.consume("work")
        source = states[edge.dependency.shape]
        if source.active and source.valid:
            budget.consume("states", 2)
            by_shape.setdefault(edge.dependency.shape, {}).setdefault(
                edge.dependency.parameter, []
            ).append(index)
    changed = True
    while changed:
        changed = False
        for node, state in states.items():
            budget.consume("work")
            if (
                node in proofs
                or not state.active
                or not state.valid
                or not state.presence
            ):
                continue
            needed: list[int] = []
            for parameter, indexes in by_shape.get(node, {}).items():
                budget.consume("work", len(indexes))
                proven = [
                    index
                    for index in indexes
                    if edges[index].active
                    and edges[index].dependency.dependency in proofs
                ]
                if parameter in (SH.property, SH.node, SH["and"]) and proven:
                    needed = proven[:1]
                    break
                if parameter in (SH["or"], SH.xone) and len(proven) == len(indexes):
                    heads = parameters.values(node, parameter, single=True)
                    if heads:
                        needed = proven
                        break
            if not needed:
                continue
            local: set[Node] = set()
            dependencies: set[int] = set()
            for index in needed:
                child = proofs[edges[index].dependency.dependency]
                budget.consume("work", len(child.local) + len(child.edges))
                budget.consume("states", 1 + len(child.local) + len(child.edges))
                local.update(child.local)
                dependencies.update(child.edges)
                dependencies.add(index)
            proofs[node] = _Proof(frozenset(local), frozenset(dependencies))
            changed = True
    for root in roots:
        budget.consume("work")
        if root in proofs:
            proof = proofs[root]
            budget.consume("states", 1 + len(proof.local) + len(proof.edges))
            root_conflict = RootConflict(
                root,
                tuple(proof.local),
                tuple(edges[i].dependency for i in proof.edges),
            )
            yield root_conflict
