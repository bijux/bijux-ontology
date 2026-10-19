"""Owned vocabulary roles and shared immutable scoped inspection indexes."""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import _read_graph, bind_namespaces, copy_graph
from bijux_ontology.vocabulary.browsing.results import Edge, TermInfo
from bijux_ontology.vocabulary.sources.types import GraphKind, OwnedSource, source_role


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


@dataclass(frozen=True)
class _VocabularyPreparation:
    """Closed source receipts and already qualified native inspection records.

    This internal handoff carries preparation, never validation verdicts. An
    external decoder owns its trust pin, input identity and typed record checks.
    """

    sources: tuple[tuple[str, OwnedSource, SnapshotMetadata], ...]
    inspections: tuple[tuple[tuple[str, ...], _VocabularyIndex], ...]
    required_imports_qualified: bool


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

        def own(graph: Graph) -> OwnedSource:
            """Separate each independent input's blank scope exactly once."""
            prepared = prepare_rdf(graph, limits=selected, budget=budget)
            return OwnedSource(prepared, prepared._read())

        self._initialize(
            own(local), tuple(map(own, imports)), tuple(map(own, lookup)), selected
        )

    def _initialize(
        self,
        local: OwnedSource,
        imports: tuple[OwnedSource, ...],
        lookup: tuple[OwnedSource, ...],
        limits: Limits,
        *,
        required_imports_qualified: bool = False,
    ) -> None:
        """Retain only owned immutable graph handles before publishing readers."""
        self._limits = limits
        self._required_imports_qualified = required_imports_qualified
        self._source_handles = (
            ("local", local),
            *((f"import[{index}]", source) for index, source in enumerate(imports)),
            *((f"lookup[{index}]", source) for index, source in enumerate(lookup)),
        )
        self._sources = tuple(
            (scope, source.selected) for scope, source in self._source_handles
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
        local: OwnedSource,
        imports: tuple[OwnedSource, ...],
        lookup: tuple[OwnedSource, ...],
        limits: Limits,
        *,
        required_imports_qualified: bool = False,
    ) -> "Vocabulary":
        """Adopt internal readers prepared within the caller's shared budget."""
        result = cls.__new__(cls)
        result._initialize(
            local,
            imports,
            lookup,
            limits,
            required_imports_qualified=required_imports_qualified,
        )
        return result

    def _capture_preparation(self, *, budget: Budget) -> _VocabularyPreparation:
        """Capture existing immutable preparation; do not build missing indexes."""
        if not isinstance(budget, Budget):
            raise InputError("Vocabulary preparation requires a Budget")
        with self._read_lock:
            count = len(self._source_handles) + len(self._indexes)
            budget.consume("work", count)
            budget.consume("states", 3 * count)
            budget.consume("bytes", 128 * count)
            sources = tuple(
                (scope, source, SnapshotMetadata.capture(source.prepared, budget))
                for scope, source in self._source_handles
            )
            inspections = tuple(self._indexes.items())
            return _VocabularyPreparation(
                sources, inspections, self._required_imports_qualified
            )

    @classmethod
    def _from_preparation(
        cls, preparation: _VocabularyPreparation, *, budget: Budget
    ) -> "Vocabulary":
        """Publish a new owner after a trusted decoder qualifies native records.

        Role keys and source receipts must match this owner exactly. The decoder
        has already checked complete index references against externally pinned
        artifact data; adoption freezes those records without rederiving them.
        """
        if not isinstance(preparation, _VocabularyPreparation) or not isinstance(
            budget, Budget
        ):
            raise InputError("Expected typed vocabulary preparation and Budget")
        if (
            type(preparation.sources) is not tuple
            or not preparation.sources
            or type(preparation.inspections) is not tuple
            or type(preparation.required_imports_qualified) is not bool
        ):
            raise InputError("Vocabulary preparation requires complete inventories")
        budget.consume(
            "states", len(preparation.sources) + len(preparation.inspections)
        )
        sources: list[OwnedSource] = []
        imports: list[OwnedSource] = []
        lookup: list[OwnedSource] = []
        scopes: list[str] = []
        for position, (scope, source, receipt) in enumerate(preparation.sources):
            budget.consume("work")
            if not isinstance(source, OwnedSource) or not isinstance(
                receipt, SnapshotMetadata
            ):
                raise InputError(
                    "Vocabulary preparation requires owned source receipts"
                )
            expected = (
                "local"
                if not position
                else f"import[{len(imports)}]"
                if scope.startswith("import[") and not lookup
                else f"lookup[{len(lookup)}]"
            )
            if scope != expected:
                raise InputError(
                    "Vocabulary preparation source roles are not canonical"
                )
            if not receipt.same_scope(
                SnapshotMetadata.capture(source.prepared, budget)
            ):
                raise InputError("Vocabulary preparation source metadata changed")
            if source.selected is not source.prepared._read(
                source.graph_name, union=source.union, budget=budget
            ):
                raise InputError("Vocabulary preparation selected graph is not owned")
            budget.consume("resources")
            budget.consume("triples", len(source.selected))
            scopes.append(scope)
            sources.append(source)
            if position:
                (imports if scope.startswith("import[") else lookup).append(source)
        result = cls._from_owned(
            sources[0],
            tuple(imports),
            tuple(lookup),
            budget.limits,
            required_imports_qualified=preparation.required_imports_qualified,
        )
        for key, index in preparation.inspections:
            budget.consume("work")
            if (
                type(key) is not tuple
                or len(key) > len(scopes)
                or not isinstance(index, _VocabularyIndex)
            ):
                raise InputError("Vocabulary preparation inspection scope is invalid")
            budget.consume("work", (len(scopes) + 1) * (len(key) + 1))
            budget.consume("states", len(key))
            for scope in key:
                if not isinstance(scope, str):
                    raise InputError("Vocabulary preparation role keys require text")
                budget.consume("work", len(scope) + 1)
            roles = {source_role(scope) for scope in key}
            if (
                tuple(scope for scope in scopes if source_role(scope) in roles) != key
                or key in result._indexes
            ):
                raise InputError("Vocabulary preparation inspection scope is invalid")
            count = (
                len(index.kinds)
                + len(index.edges)
                + len(index.outgoing)
                + len(index.incoming)
                + len(index.terms)
                + sum(map(len, index.kinds.values()))
                + sum(map(len, index.outgoing.values()))
                + sum(map(len, index.incoming.values()))
            )
            budget.consume("work", count)
            budget.consume("states", count)
            budget.consume("bytes", 128 + 64 * count)
            if any(edge.scope not in key for edge in index.edges):
                raise InputError("Vocabulary preparation edge escapes inspection scope")
            frozen = _VocabularyIndex(
                MappingProxyType(
                    {iri: frozenset(kinds) for iri, kinds in index.kinds.items()}
                ),
                tuple(index.edges),
                MappingProxyType(
                    {node: tuple(edges) for node, edges in index.outgoing.items()}
                ),
                MappingProxyType(
                    {node: tuple(edges) for node, edges in index.incoming.items()}
                ),
                MappingProxyType(dict(index.terms)),
            )
            result._indexes[key] = frozen
            result._kinds[key] = frozen.kinds
        return result

    @property
    def required_imports_qualified(self) -> bool:
        """Whether loading validated the local resource and required import closure.

        Raw graph construction supplies ownership without import qualification.
        Lookup resources are excluded. This records provenance, not consistency.
        """
        return self._required_imports_qualified

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

    def _fact_sources(
        self, scopes: Sequence[str], budget: Budget
    ) -> Iterator[tuple[str, GraphKind, URIRef | BNode | None, Graph]]:
        """Read exact selected graph contexts only when fact ownership is requested."""
        if (
            isinstance(scopes, (str, bytes))
            or not scopes
            or set(scopes) - {"local", "imports", "lookup"}
        ):
            raise InputError("Select local/imports/lookup source roles explicitly")
        allowance = Budget(budget.limits)
        for scope, source in self._source_handles:
            if source_role(scope) in scopes:
                allowance.consume("resources")
                allowance.consume("triples", len(source.selected))
                budget.consume("resources")
                for kind, name, graph in source.contexts(budget):
                    yield scope, kind, name, graph

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
