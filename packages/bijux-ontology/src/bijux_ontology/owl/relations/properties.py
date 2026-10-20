"""Effective endpoint constraints through named property relations."""

from dataclasses import dataclass

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.execution.limits import Budget


@dataclass(frozen=True)
class PropertyContract:
    """Conjunctive constraints on the parent and value of one field occurrence."""

    parents: tuple[Node, ...]
    values: tuple[Node, ...]
    object_property: bool = False
    literal_value: bool = False
    literal_parent: bool = False
    unsupported: bool = False
    bottom: bool = False


def property_contract(
    graph: Graph,
    predicate: str,
    *,
    reverse: bool = False,
    annotation_contracts: bool = True,
    budget: Budget | None = None,
) -> PropertyContract:
    """Follow finite subproperty, equivalence and inverse paths without reasoning.

    Every state records endpoint parity, so cycles and double inversions neither
    duplicate constraints nor swap the caller's parent and child accidentally.
    """
    budget = budget if budget is not None else Budget()
    pending = [(URIRef(predicate), reverse)]
    visited: set[tuple[URIRef, bool]] = set()
    parents: set[Node] = set()
    values: set[Node] = set()
    object_property = literal_value = literal_parent = unsupported = bottom = False
    while pending:
        budget.consume("work")
        node, swapped = pending.pop()
        if (node, swapped) in visited:
            continue
        visited.add((node, swapped))
        kinds = set(graph.objects(node, RDF.type))
        annotation = OWL.AnnotationProperty in kinds
        if annotation_contracts or not annotation:
            parents.update(graph.objects(node, RDFS.range if swapped else RDFS.domain))
            values.update(graph.objects(node, RDFS.domain if swapped else RDFS.range))
        object_property |= OWL.ObjectProperty in kinds
        if OWL.DatatypeProperty in kinds:
            literal_parent |= swapped
            literal_value |= not swapped
        bottom |= node in (OWL.bottomObjectProperty, OWL.bottomDataProperty)
        related = (
            *((item, swapped) for item in graph.objects(node, RDFS.subPropertyOf)),
            *((item, swapped) for item in graph.objects(node, OWL.equivalentProperty)),
            *((item, swapped) for item in graph.subjects(OWL.equivalentProperty, node)),
            *((item, not swapped) for item in graph.objects(node, OWL.inverseOf)),
            *((item, not swapped) for item in graph.subjects(OWL.inverseOf, node)),
        )
        for target, parity in related:
            if isinstance(target, URIRef):
                pending.append((target, parity))
            else:
                unsupported = True
        if next(graph.objects(node, OWL.propertyChainAxiom), None) is not None:
            unsupported = True
    return PropertyContract(
        tuple(sorted(parents, key=str)),
        tuple(sorted(values, key=str)),
        object_property,
        literal_value,
        literal_parent,
        unsupported,
        bottom,
    )
