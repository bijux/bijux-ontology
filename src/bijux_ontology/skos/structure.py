"""Finite SKOS relationship implications with asserted explanation paths."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget

_RELATIONS = frozenset(
    (
        SKOS.semanticRelation,
        SKOS.mappingRelation,
        SKOS.broader,
        SKOS.narrower,
        SKOS.broaderTransitive,
        SKOS.narrowerTransitive,
        SKOS.related,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
        SKOS.closeMatch,
        SKOS.exactMatch,
    )
)


@dataclass(frozen=True)
class SkosStructure:
    """Selected asserted facts and only the advertised finite SKOS implications."""

    roles: Mapping[Node, frozenset[URIRef]]
    schemes: Mapping[Node, frozenset[Node]]
    members: Mapping[Node, frozenset[Node]]
    broader: Mapping[Node, frozenset[Node]]
    transitive: Mapping[Node, frozenset[Node]]
    exact: Mapping[Node, frozenset[Node]]
    related: frozenset[tuple[Node, Node]]
    broad_mappings: frozenset[tuple[Node, Node]]
    related_mappings: frozenset[tuple[Node, Node]]
    mapping_edges: tuple[tuple[Node, URIRef, Node], ...]
    invalid_targets: tuple[tuple[Node, URIRef, Node], ...]


def structure(graph: Graph, budget: Budget) -> SkosStructure:
    """Build explicit role/inverse/subproperty implications without mutating RDF."""
    roles: dict[Node, set[URIRef]] = {}
    schemes: dict[Node, set[Node]] = {}
    members: dict[Node, set[Node]] = {}
    broader: dict[Node, set[Node]] = {}
    transitive: dict[Node, set[Node]] = {}
    exact: dict[Node, set[Node]] = {}
    related: set[tuple[Node, Node]] = set()
    broad_mappings: set[tuple[Node, Node]] = set()
    related_mappings: set[tuple[Node, Node]] = set()
    mappings: list[tuple[Node, URIRef, Node]] = []
    invalid: list[tuple[Node, URIRef, Node]] = []
    for subject, predicate, value in graph:
        budget.consume("work")
        if predicate == RDF.type and value in (
            SKOS.Concept,
            SKOS.ConceptScheme,
            SKOS.Collection,
            SKOS.OrderedCollection,
        ):
            roles.setdefault(subject, set()).add(URIRef(str(value)))
            if value == SKOS.OrderedCollection:
                roles[subject].add(SKOS.Collection)
        # Retain malformed members for the opt-in collection policy; their
        # rejection as relation endpoints must not erase the authored evidence.
        if predicate == SKOS.member:
            roles.setdefault(subject, set()).add(SKOS.Collection)
            members.setdefault(subject, set()).add(value)
        if predicate in _RELATIONS or predicate in (
            SKOS.hasTopConcept,
            SKOS.topConceptOf,
            SKOS.inScheme,
            SKOS.member,
            SKOS.memberList,
        ):
            if not isinstance(value, URIRef | BNode):
                invalid.append((subject, URIRef(str(predicate)), value))
                if predicate in _RELATIONS:
                    roles.setdefault(subject, set()).add(SKOS.Concept)
                continue
        if predicate in _RELATIONS:
            roles.setdefault(subject, set()).add(SKOS.Concept)
            roles.setdefault(value, set()).add(SKOS.Concept)
        if predicate in (SKOS.inScheme, SKOS.topConceptOf):
            schemes.setdefault(subject, set()).add(value)
            roles.setdefault(value, set()).add(SKOS.ConceptScheme)
            if predicate == SKOS.topConceptOf:
                roles.setdefault(subject, set()).add(SKOS.Concept)
        elif predicate == SKOS.hasTopConcept:
            schemes.setdefault(value, set()).add(subject)
            roles.setdefault(subject, set()).add(SKOS.ConceptScheme)
            roles.setdefault(value, set()).add(SKOS.Concept)
        elif predicate == SKOS.memberList:
            roles.setdefault(subject, set()).update(
                (SKOS.OrderedCollection, SKOS.Collection)
            )
        if predicate in (SKOS.broader, SKOS.broadMatch):
            broader.setdefault(subject, set()).add(value)
        elif predicate in (SKOS.narrower, SKOS.narrowMatch):
            broader.setdefault(value, set()).add(subject)
        if predicate == SKOS.broaderTransitive:
            transitive.setdefault(subject, set()).add(value)
        elif predicate == SKOS.narrowerTransitive:
            transitive.setdefault(value, set()).add(subject)
        if predicate in (SKOS.related, SKOS.relatedMatch):
            related.update(((subject, value), (value, subject)))
        if predicate == SKOS.exactMatch:
            exact.setdefault(subject, set()).add(value)
            exact.setdefault(value, set()).add(subject)
        if predicate in (SKOS.broadMatch, SKOS.narrowMatch):
            broad_mappings.add(
                (subject, value) if predicate == SKOS.broadMatch else (value, subject)
            )
        if predicate == SKOS.relatedMatch:
            related_mappings.update(((subject, value), (value, subject)))
        if predicate in (
            SKOS.broadMatch,
            SKOS.narrowMatch,
            SKOS.relatedMatch,
            SKOS.closeMatch,
            SKOS.exactMatch,
        ):
            mappings.append((subject, URIRef(str(predicate)), value))
    for subject, values in broader.items():
        transitive.setdefault(subject, set()).update(values)
    return SkosStructure(
        {key: frozenset(values) for key, values in roles.items()},
        {key: frozenset(values) for key, values in schemes.items()},
        {key: frozenset(values) for key, values in members.items()},
        {key: frozenset(values) for key, values in broader.items()},
        {key: frozenset(values) for key, values in transitive.items()},
        {key: frozenset(values) for key, values in exact.items()},
        frozenset(related),
        frozenset(broad_mappings),
        frozenset(related_mappings),
        tuple(mappings),
        tuple(invalid),
    )


def reachability(
    adjacency: Mapping[Node, frozenset[Node]], budget: Budget
) -> dict[Node, dict[Node, tuple[Node, ...]]]:
    """Retain shortest finite asserted paths, including genuine cyclic self paths."""
    result: dict[Node, dict[Node, tuple[Node, ...]]] = {}
    for source in sorted(adjacency, key=lambda node: node.n3()):
        paths: dict[Node, tuple[Node, ...]] = {}
        queue: deque[tuple[Node, tuple[Node, ...]]] = deque([(source, (source,))])
        expanded = {source}
        while queue:
            current, path = queue.popleft()
            for target in sorted(
                adjacency.get(current, ()), key=lambda node: node.n3()
            ):
                budget.consume("work")
                if target not in paths:
                    budget.consume("states")
                    paths[target] = (*path, target)
                if target not in expanded:
                    expanded.add(target)
                    queue.append((target, (*path, target)))
        result[source] = paths
    return result
