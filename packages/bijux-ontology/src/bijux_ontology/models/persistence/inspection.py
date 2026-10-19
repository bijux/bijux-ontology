"""Typed native vocabulary inspection records, without rebuilding their indexes."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, cast

from rdflib import URIRef
from rdflib.term import Node

from bijux_ontology._json import array_value, string_value, string_values
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import validate_triple
from bijux_ontology.vocabulary.browsing.results import Edge, TermInfo
from bijux_ontology.vocabulary.sources.types import OwnedSource
from bijux_ontology.vocabulary.sources.view import _VocabularyIndex

from .sources import TermTable, record, reference


def inspection_data(
    key: tuple[str, ...], index: _VocabularyIndex, terms: TermTable, budget: Budget
) -> dict[str, object]:
    """Capture actual immutable edge order and reference-based native indexes."""
    budget.consume("states", len(index.edges))
    budget.consume("bytes", 64 * len(index.edges))
    positions = {id(edge): position for position, edge in enumerate(index.edges)}

    def adjacency(values: Mapping[Node, tuple[Edge, ...]]) -> list[object]:
        """Keep existing adjacency order rather than deriving a second index."""
        result: list[object] = []
        for node, edges in values.items():
            budget.consume("work", len(edges) + 1)
            budget.consume("states")
            result.append([terms.add(node), [positions[id(edge)] for edge in edges]])
        return result

    edges: list[object] = []
    for edge in index.edges:
        budget.consume("work")
        edges.append(
            [
                terms.add(edge.subject),
                terms.add(edge.predicate),
                terms.add(edge.object),
                edge.scope,
            ]
        )
    return {
        "scopes": list(key),
        "edges": edges,
        "kinds": [[iri, sorted(kinds)] for iri, kinds in index.kinds.items()],
        "terms": [
            [term.iri, term.status, list(term.kinds), list(term.scopes)]
            for term in index.terms.values()
        ],
        "outgoing": adjacency(index.outgoing),
        "incoming": adjacency(index.incoming),
    }


def _adjacency(
    value: object,
    edges: tuple[Edge, ...],
    terms: tuple[Node, ...],
    *,
    incoming: bool,
    budget: Budget,
) -> Mapping[Node, tuple[Edge, ...]]:
    """Validate complete typed adjacency references without running _index."""
    result: dict[Node, tuple[Edge, ...]] = {}
    covered: set[int] = set()
    for item in array_value(value):
        row = array_value(item)
        if len(row) != 2:
            raise InputError("Preparation adjacency needs a term and edge references")
        node = terms[reference(row[0], len(terms))]
        if node in result:
            raise InputError("Preparation repeats an adjacency key")
        refs = array_value(row[1])
        budget.consume("states", len(refs) + 1)
        budget.consume("bytes", 64 + 24 * len(refs))
        selected: list[Edge] = []
        previous = -1
        for raw in refs:
            budget.consume("work")
            position = reference(raw, len(edges))
            edge = edges[position]
            if (
                position <= previous
                or position in covered
                or (edge.object if incoming else edge.subject) != node
            ):
                raise InputError("Preparation adjacency is inconsistent")
            previous = position
            covered.add(position)
            selected.append(edge)
        if not selected:
            raise InputError("Preparation adjacency has an empty key")
        result[node] = tuple(selected)
    if len(covered) != len(edges):
        raise InputError("Preparation adjacency omits asserted edges")
    return MappingProxyType(result)


def read_inspection(
    value: object,
    terms: tuple[Node, ...],
    sources: Mapping[str, OwnedSource],
    budget: Budget,
) -> tuple[tuple[str, ...], _VocabularyIndex]:
    """Qualify exact source membership and complete native reference inventories."""
    raw = record(value, {"scopes", "edges", "kinds", "terms", "outgoing", "incoming"})
    key = string_values(raw["scopes"])
    if len(key) != len(set(key)) or any(scope not in sources for scope in key):
        raise InputError("Preparation inspection references unknown source roles")
    edges: list[Edge] = []
    seen: set[tuple[Node, Node, Node, str]] = set()
    for item in array_value(raw["edges"]):
        budget.consume("work")
        budget.consume("states")
        budget.consume("bytes", 160)
        row = array_value(item)
        if len(row) != 4:
            raise InputError("Preparation edge needs three terms and source scope")
        subject, predicate, obj = (terms[reference(ref, len(terms))] for ref in row[:3])
        validate_triple(subject, predicate, obj)
        scope = string_value(row[3])
        identity = subject, predicate, obj, scope
        if (
            scope not in key
            or identity in seen
            or (subject, predicate, obj) not in sources[scope].selected
        ):
            raise InputError("Preparation edge does not match its owned source")
        seen.add(identity)
        edges.append(Edge(subject, cast(URIRef, predicate), obj, scope))
    if len(edges) != sum(len(sources[scope].selected) for scope in key):
        raise InputError("Preparation inspection omits source assertions")
    kinds: dict[str, frozenset[str]] = {}
    for item in array_value(raw["kinds"]):
        row = array_value(item)
        if len(row) != 2:
            raise InputError("Preparation kind record needs an IRI and kinds")
        iri = str(absolute_iri(string_value(row[0])))
        values = string_values(row[1])
        budget.consume("states", len(values) + 1)
        budget.consume("work", len(values) + 1)
        for kind in values:
            absolute_iri(kind)
        if iri in kinds or len(values) != len(set(values)):
            raise InputError("Preparation repeats a declared kind")
        kinds[iri] = frozenset(values)
    infos: dict[str, TermInfo] = {}
    for item in array_value(raw["terms"]):
        budget.consume("states")
        budget.consume("work")
        row = array_value(item)
        if len(row) != 4:
            raise InputError("Preparation term information is incomplete")
        iri = str(absolute_iri(string_value(row[0])))
        status = string_value(row[1])
        term_kinds, scopes = string_values(row[2]), string_values(row[3])
        if (
            iri in infos
            or status not in ("declared", "referenced")
            or (set(term_kinds) != kinds.get(iri, frozenset()))
            or any(scope not in key for scope in scopes)
        ):
            raise InputError("Preparation term information is inconsistent")
        infos[iri] = TermInfo(
            iri, cast(Literal["declared", "referenced"], status), term_kinds, scopes
        )
    frozen_edges = tuple(edges)
    return key, _VocabularyIndex(
        MappingProxyType(kinds),
        frozen_edges,
        _adjacency(raw["outgoing"], frozen_edges, terms, incoming=False, budget=budget),
        _adjacency(raw["incoming"], frozen_edges, terms, incoming=True, budget=budget),
        MappingProxyType(infos),
    )
