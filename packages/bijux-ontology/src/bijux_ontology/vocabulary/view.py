"""Owned vocabulary roles and shared immutable scoped inspection indexes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import cast

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import _read_graph, bind_namespaces, copy_graph

from .results import Edge, TermInfo


@dataclass(frozen=True, slots=True)
class _LexicalCandidate:
    """Reuse owned RDF terms until a matched page needs plain text evidence."""

    term: TermInfo
    field: str
    value: str
    language: str | None
    scope: str


def _ordered_terms(index: "_VocabularyIndex", budget: Budget) -> tuple[TermInfo, ...]:
    """Prepare a shared native order, charging vector and sort allowances first."""
    count = len(index.kinds)
    budget.consume("work", len(index.terms) + count * max(1, count.bit_length()))
    budget.consume("states", count)
    # References and sort workspace only: TermInfo and its strings stay shared.
    budget.consume("bytes", 128 + 64 * count)
    result = tuple(
        sorted(
            (term for term in index.terms.values() if term.status == "declared"),
            key=lambda term: term.iri,
        )
    )
    budget.consume("work", 0)
    return result


def _lexical_candidates(
    index: "_VocabularyIndex", terms: tuple[TermInfo, ...], budget: Budget
) -> tuple[_LexicalCandidate, ...]:
    """Prepare query-independent lexical positions without retained text copies."""
    values: list[_LexicalCandidate] = []
    for term in terms:
        budget.consume("work")
        for scope in term.scopes:
            budget.consume("work")
            budget.consume("states")
            budget.consume("bytes", 256)
            values.append(_LexicalCandidate(term, "iri", term.iri, None, scope))
        for edge in index.outgoing.get(URIRef(term.iri), ()):
            budget.consume("work")
            if isinstance(edge.object, Literal):
                budget.consume("states")
                # Slotted record/vector/sort-key allowance, including temporary
                # Unicode sort keys. Retained values reuse immutable RDF terms.
                budget.consume(
                    "bytes", 256 + 4 * (len(edge.predicate) + len(edge.object))
                )
                values.append(
                    _LexicalCandidate(
                        term,
                        edge.predicate,
                        edge.object,
                        edge.object.language,
                        edge.scope,
                    )
                )
    count = len(values)
    budget.consume("work", count * max(1, count.bit_length()))
    values.sort(
        key=lambda value: (
            value.term.iri,
            str(value.field),
            value.language or "",
            str(value.value),
            value.scope,
        )
    )
    budget.consume("work", 0)
    return tuple(values)


@dataclass(frozen=True)
class _VocabularyIndex:
    """One immutable role selection shared by compatible internal consumers."""

    kinds: Mapping[str, frozenset[str]]
    edges: tuple[Edge, ...]
    outgoing: Mapping[Node, tuple[Edge, ...]]
    incoming: Mapping[Node, tuple[Edge, ...]]
    terms: Mapping[str, TermInfo]


def _index(
    scopes: tuple[tuple[str, Graph], ...], budget: Budget | None
) -> _VocabularyIndex:
    """Build bounded term and edge indexes once, preserving source provenance."""
    if budget is not None:
        budget.consume("work", sum(len(graph) for _, graph in scopes))
    edges = tuple(
        sorted(
            (
                Edge(subject, cast(URIRef, predicate), value, scope)
                for scope, graph in scopes
                for subject, predicate, value in graph
            ),
            key=lambda edge: (
                str(edge.subject),
                str(edge.predicate),
                edge.object.n3(),
                edge.scope,
            ),
        )
    )
    outgoing: dict[Node, list[Edge]] = {}
    incoming: dict[Node, list[Edge]] = {}
    kinds: dict[str, set[str]] = {}
    locations: dict[str, set[str]] = {}
    declared_locations: dict[str, set[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.subject, []).append(edge)
        incoming.setdefault(edge.object, []).append(edge)
        for node in (edge.subject, edge.predicate, edge.object):
            if isinstance(node, URIRef):
                locations.setdefault(str(node), set()).add(edge.scope)
            if isinstance(node, Literal) and node.datatype is not None:
                locations.setdefault(str(node.datatype), set()).add(edge.scope)
        if (
            edge.predicate == RDF.type
            and isinstance(edge.subject, URIRef)
            and isinstance(edge.object, URIRef)
        ):
            kinds.setdefault(str(edge.subject), set()).add(str(edge.object))
            declared_locations.setdefault(str(edge.subject), set()).add(edge.scope)
    return _VocabularyIndex(
        MappingProxyType({iri: frozenset(values) for iri, values in kinds.items()}),
        edges,
        MappingProxyType({node: tuple(values) for node, values in outgoing.items()}),
        MappingProxyType({node: tuple(values) for node, values in incoming.items()}),
        MappingProxyType(
            {
                iri: TermInfo(
                    iri,
                    "declared" if iri in kinds else "referenced",
                    tuple(sorted(kinds.get(iri, ()))),
                    tuple(sorted(declared_locations.get(iri, locations[iri]))),
                )
                for iri in locations
            }
        ),
    )


@dataclass(frozen=True)
class _VocabularyRead:
    """Read-only source roles and their set-valued union without inspection work."""

    scopes: tuple[tuple[str, Graph], ...]
    graph: Graph


class Vocabulary:
    """Own separate source roles; public graph exports remain defensive copies.

    Internal readers share immutable snapshots and role-specific indexes. There
    is no invalidation protocol: changing a resource creates a new vocabulary.
    """

    def __init__(
        self,
        local: Graph,
        imports: Sequence[Graph] = (),
        lookup: Sequence[Graph] = (),
        *,
        limits: Limits | None = None,
    ) -> None:
        selected = limits if limits is not None else Limits()
        budget = Budget(selected)

        def own(graph: Graph) -> Graph:
            """Separate each independent input's blank scope exactly once."""
            return prepare_rdf(graph, limits=selected, budget=budget)._read()

        self._initialize(
            own(local), tuple(map(own, imports)), tuple(map(own, lookup)), selected
        )

    def _initialize(
        self,
        local: Graph,
        imports: tuple[Graph, ...],
        lookup: tuple[Graph, ...],
        limits: Limits,
    ) -> None:
        """Retain only owned immutable graph handles before publishing readers."""
        self._limits = limits
        self._sources = (
            ("local", local),
            *((f"import[{index}]", graph) for index, graph in enumerate(imports)),
            *((f"lookup[{index}]", graph) for index, graph in enumerate(lookup)),
        )
        self._reads: dict[tuple[str, ...], _VocabularyRead] = {}
        self._indexes: dict[tuple[str, ...], _VocabularyIndex] = {}
        self._kinds: dict[tuple[str, ...], Mapping[str, frozenset[str]]] = {}
        self._term_pages: dict[tuple[str, ...], tuple[TermInfo, ...]] = {}
        self._lexical_pages: dict[tuple[str, ...], tuple[_LexicalCandidate, ...]] = {}
        self._read_lock = Lock()

    @classmethod
    def _from_owned(
        cls,
        local: Graph,
        imports: tuple[Graph, ...],
        lookup: tuple[Graph, ...],
        limits: Limits,
    ) -> "Vocabulary":
        """Adopt internal readers prepared within the caller's shared budget."""
        result = cls.__new__(cls)
        result._initialize(local, imports, lookup, limits)
        return result

    def _read(
        self,
        scopes: Sequence[str] = ("local", "imports", "lookup"),
        *,
        budget: Budget | None = None,
        limits: Limits | None = None,
    ) -> _VocabularyRead:
        """Select owned roles without preparing unrelated inspection indexes."""
        if (
            isinstance(scopes, (str, bytes))
            or not scopes
            or set(scopes) - {"local", "imports", "lookup"}
        ):
            raise InputError("Select local/imports/lookup source roles explicitly")
        key = tuple(role for role in ("local", "imports", "lookup") if role in scopes)
        selected = tuple(
            (scope, graph)
            for scope, graph in self._sources
            if (
                "imports"
                if scope.startswith("import[")
                else "lookup"
                if scope.startswith("lookup[")
                else scope
            )
            in key
        )
        key = tuple(scope for scope, _ in selected)
        allowance = Budget(
            limits or (budget.limits if budget is not None else self.limits)
        )
        allowance.consume("resources", len(selected))
        allowance.consume("triples", sum(len(graph) for _, graph in selected))
        with self._read_lock:
            if key not in self._reads:
                if len(selected) == 1:
                    graph = selected[0][1]
                else:
                    union = Graph(bind_namespaces="none")
                    for _, source in selected:
                        for triple in source:
                            if budget is not None:
                                budget.consume("work")
                            union.add(triple)
                    bind_namespaces(union, *(source for _, source in selected))
                    graph = _read_graph(union)
                self._reads[key] = _VocabularyRead(selected, graph)
            return self._reads[key]

    def _inspect(
        self, reader: _VocabularyRead, budget: Budget | None = None
    ) -> _VocabularyIndex:
        """Share fully built immutable term/edge indexes for the same source roles."""
        key = tuple(scope for scope, _ in reader.scopes)
        with self._read_lock:
            if key not in self._indexes:
                index = _index(reader.scopes, budget)
                self._indexes[key] = index
                self._kinds[key] = index.kinds
            return self._indexes[key]

    def _browse_terms(
        self, reader: _VocabularyRead, index: _VocabularyIndex, budget: Budget
    ) -> tuple[TermInfo, ...]:
        """Publish one complete ordered derivative per owned role selection."""
        key = tuple(scope for scope, _ in reader.scopes)
        with self._read_lock:
            if key not in self._term_pages:
                self._term_pages[key] = _ordered_terms(index, budget)
            return self._term_pages[key]

    def _browse_lexical(
        self, reader: _VocabularyRead, index: _VocabularyIndex, budget: Budget
    ) -> tuple[_LexicalCandidate, ...]:
        """Share lexical preparation only when lexical browsing is requested."""
        key = tuple(scope for scope, _ in reader.scopes)
        with self._read_lock:
            if key not in self._term_pages:
                self._term_pages[key] = _ordered_terms(index, budget)
            if key not in self._lexical_pages:
                self._lexical_pages[key] = _lexical_candidates(
                    index, self._term_pages[key], budget
                )
            return self._lexical_pages[key]

    def _declared(self, iri: str, scopes: Sequence[str]) -> frozenset[str]:
        """Reuse exact targeted type lookups without scanning unrelated triples."""
        reader = self._read(scopes)
        roles = tuple(scope for scope, _ in reader.scopes)
        with self._read_lock:
            if roles in self._kinds:
                return self._kinds[roles].get(iri, frozenset())
        return frozenset(
            str(kind)
            for kind in reader.graph.objects(URIRef(iri), RDF.type)
            if isinstance(kind, URIRef)
        )

    def _kind_index(
        self, scopes: Sequence[str], budget: Budget
    ) -> Mapping[str, frozenset[str]]:
        """Index explicit declarations only when complete coverage needs them."""
        reader = self._read(scopes, budget=budget)
        key = tuple(scope for scope, _ in reader.scopes)
        with self._read_lock:
            if key not in self._kinds:
                kinds: dict[str, set[str]] = {}
                for subject, kind in reader.graph.subject_objects(RDF.type):
                    budget.consume("work")
                    if isinstance(subject, URIRef) and isinstance(kind, URIRef):
                        kinds.setdefault(str(subject), set()).add(str(kind))
                self._kinds[key] = MappingProxyType(
                    {iri: frozenset(values) for iri, values in kinds.items()}
                )
            return self._kinds[key]

    @property
    def limits(self) -> Limits:
        """Return the immutable bounds used to prepare this vocabulary."""
        return self._limits

    @property
    def local(self) -> Graph:
        """Export local declarations without permitting mutation of this view."""
        return copy_graph(self._sources[0][1], standardize_apart=False)

    @property
    def imports(self) -> tuple[Graph, ...]:
        """Export independent required-import graphs with their owned blank IDs."""
        return tuple(
            copy_graph(graph, standardize_apart=False)
            for scope, graph in self._sources
            if scope.startswith("import[")
        )

    @property
    def lookup(self) -> tuple[Graph, ...]:
        """Export descriptive sources without changing import ownership."""
        return tuple(
            copy_graph(graph, standardize_apart=False)
            for scope, graph in self._sources
            if scope.startswith("lookup[")
        )

    def graph(self) -> Graph:
        """Export an independent union of the vocabulary's supplied source roles."""
        return copy_graph(self._read().graph, standardize_apart=False)
