"""Recognise local OWL constructs outside finite named-edge comparisons."""

from rdflib import BNode, Graph
from rdflib.namespace import OWL, RDFS
from rdflib.term import Node

_UNSUPPORTED = frozenset(
    (
        OWL.unionOf,
        OWL.intersectionOf,
        OWL.complementOf,
        OWL.oneOf,
        OWL.disjointUnionOf,
        OWL.datatypeComplementOf,
        OWL.onDatatype,
        OWL.withRestrictions,
        OWL.onProperty,
        OWL.someValuesFrom,
        OWL.allValuesFrom,
        OWL.hasValue,
        OWL.hasSelf,
        OWL.minCardinality,
        OWL.maxCardinality,
        OWL.cardinality,
        OWL.minQualifiedCardinality,
        OWL.maxQualifiedCardinality,
        OWL.qualifiedCardinality,
        OWL.onClass,
        OWL.onDataRange,
        OWL.propertyChainAxiom,
        OWL.hasKey,
    )
)


def unsupported_constructs(graph: Graph, node: Node) -> tuple[str, ...]:
    """Name relevant OWL expressions outside finite named-edge comparisons.

    Recognition is local to the requested node. Unsupported expressions elsewhere
    do not invalidate an independent completed comparison. Recognising an OWL
    predicate never establishes that its encoding is well formed.
    """
    predicates = {
        str(predicate)
        for predicate in graph.predicates(node)
        if predicate in _UNSUPPORTED
    }
    for predicate in (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith):
        if any(isinstance(value, BNode) for value in graph.objects(node, predicate)):
            predicates.add(str(predicate))
    for predicate in (OWL.equivalentClass, OWL.disjointWith):
        if any(isinstance(value, BNode) for value in graph.subjects(predicate, node)):
            predicates.add(str(predicate))
    if (
        isinstance(node, BNode)
        and next(graph.objects(node, OWL.inverseOf), None) is not None
    ):
        predicates.add(str(OWL.inverseOf))
    return tuple(sorted(predicates))
